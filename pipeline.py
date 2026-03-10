"""  
Unified Wafer Analysis Pipeline
Chains Step 1 (ROI Detection + Good/Bad) → Step 2 (Red Cluster Analysis) → Step 3 (Error Detection)
Results saved as JSON for dashboard consumption.
"""

import cv2
import numpy as np
import os
import glob
import json
import shutil
from datetime import datetime
from pathlib import Path
import threading

# ─────────────────────────────────────────
# Shared Parameters
# ─────────────────────────────────────────

# ROI Detection
GREEN_H_LOW, GREEN_H_HIGH = 35, 85
GREEN_S_LOW, GREEN_V_LOW = 50, 50
MIN_CIRCULARITY = 0.65
MIN_AREA_RATIO = 0.005
MIN_ARC_COVERAGE = 0.75
MIN_BORDER_EDGE_RATIO = 0.08
MIN_BORDER_ARC_COVERAGE = 0.65
MIN_BORDER_CONTRAST = 18
BORDER_THICKNESS = 3
CANNY_LOW, CANNY_HIGH = 60, 180

# Red Analysis
RED_H_RANGES = [(0, 10), (160, 179)]
RED_S_MIN, RED_V_MIN = 100, 80
MIN_CLUSTER_AREA = 30
CLUSTER_MERGE_DIST = 25
EDGE_ZONE_RATIO = 0.20

# Pipeline state (for progress tracking)
_pipeline_state = {
    "running": False,
    "progress": 0,
    "total": 0,
    "current_file": "",
    "step": "",
    "last_run": None,
    "last_result": None,
}
_pipeline_lock = threading.Lock()


def get_pipeline_state():
    with _pipeline_lock:
        return dict(_pipeline_state)


# ─────────────────────────────────────────
# Core Functions (from scripts 1 & 2)
# ─────────────────────────────────────────

def least_squares_circle_fit(points):
    x = points[:, 0].astype(np.float64)
    y = points[:, 1].astype(np.float64)
    mx, my = np.mean(x), np.mean(y)
    u, v = x - mx, y - my
    A = np.column_stack([u, v, np.ones_like(u)])
    b = u**2 + v**2
    result, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    uc = result[0] / 2
    vc = result[1] / 2
    r = np.sqrt(max(result[2] + uc**2 + vc**2, 0))
    return float(uc + mx), float(vc + my), float(r)


def compute_arc_coverage(pts, cx, cy, n_bins=36):
    angles = np.arctan2(pts[:, 1] - cy, pts[:, 0] - cx)
    angles = np.mod(angles, 2 * np.pi)
    bins = (angles / (2 * np.pi) * n_bins).astype(int)
    bins = np.clip(bins, 0, n_bins - 1)
    covered = len(np.unique(bins))
    return covered / n_bins


def detect_wafer_roi(image):
    """Detect circular wafer ROI in image."""
    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    gmask = cv2.inRange(hsv,
                        np.array([GREEN_H_LOW, GREEN_S_LOW, GREEN_V_LOW]),
                        np.array([GREEN_H_HIGH, 255, 255]))

    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    clean = cv2.morphologyEx(gmask, cv2.MORPH_CLOSE, kern, iterations=2)
    clean = cv2.morphologyEx(clean, cv2.MORPH_OPEN, kern, iterations=1)

    contours, _ = cv2.findContours(clean, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    min_area = h * w * MIN_AREA_RATIO
    candidates = []

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue
        peri = cv2.arcLength(cnt, True)
        if peri == 0:
            continue
        circ = 4 * np.pi * area / (peri ** 2)
        if circ < MIN_CIRCULARITY:
            continue
        pts = cnt.reshape(-1, 2).astype(np.float64)
        if len(pts) < 30:
            continue

        cx, cy, r = least_squares_circle_fit(pts)
        if r < 50 or r > min(h, w) // 2:
            continue

        # RANSAC refinement
        inlier_pts = pts
        for _ in range(3):
            dists = np.sqrt((inlier_pts[:, 0] - cx) ** 2 + (inlier_pts[:, 1] - cy) ** 2)
            residuals = np.abs(dists - r)
            thr = max(r * 0.10, 5)
            mask = residuals < thr
            new_pts = inlier_pts[mask]
            if len(new_pts) < 30:
                break
            cx_new, cy_new, r_new = least_squares_circle_fit(new_pts)
            if abs(cx_new - cx) < 1 and abs(cy_new - cy) < 1 and abs(r_new - r) < 1:
                cx, cy, r = cx_new, cy_new, r_new
                inlier_pts = new_pts
                break
            cx, cy, r = cx_new, cy_new, r_new
            inlier_pts = new_pts

        if r < 50 or r > min(h, w) // 2:
            continue
        if not (0 <= cx < w and 0 <= cy < h):
            continue

        arc_cov = compute_arc_coverage(pts, cx, cy)
        if arc_cov < MIN_ARC_COVERAGE:
            continue

        cmask = np.zeros((h, w), dtype=np.uint8)
        cv2.circle(cmask, (int(round(cx)), int(round(cy))), int(round(r)), 255, -1)
        green_in = np.sum((gmask > 0) & (cmask > 0))
        total_in = np.sum(cmask > 0)
        green_fill = green_in / total_in if total_in > 0 else 0

        dists_final = np.sqrt((inlier_pts[:, 0] - cx) ** 2 + (inlier_pts[:, 1] - cy) ** 2)
        rmse = float(np.sqrt(np.mean((dists_final - r) ** 2)))

        candidates.append({
            'cx': int(round(cx)), 'cy': int(round(cy)), 'r': int(round(r)),
            'area': area, 'circularity': circ, 'green_fill': green_fill,
            'rmse': rmse, 'arc_coverage': arc_cov, 'contour': cnt,
        })

    if not candidates:
        return None, clean

    max_r = max(c['r'] for c in candidates)
    for c in candidates:
        dist_to_center = np.sqrt((c['cx'] - w / 2) ** 2 + (c['cy'] - h / 2) ** 2)
        max_dist = np.sqrt((w / 2) ** 2 + (h / 2) ** 2)
        center_score = 1.0 - (dist_to_center / max_dist)
        c['score'] = (c['green_fill'] * 0.35 + c['circularity'] * 0.25 +
                      (c['r'] / max(max_r, 1)) * 0.25 + center_score * 0.15)

    best = max(candidates, key=lambda x: x['score'])
    return best, clean


def _border_edge_stats(image, roi_info, thickness=BORDER_THICKNESS):
    if not roi_info:
        return 0.0, 0.0, 0.0
    h, w = image.shape[:2]
    cx, cy, r = roi_info['cx'], roi_info['cy'], roi_info['r']
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2]
    v_blur = cv2.GaussianBlur(v, (5, 5), 0)
    edges = cv2.Canny(v_blur, CANNY_LOW, CANNY_HIGH)

    ring = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(ring, (int(cx), int(cy)), int(r), 255, thickness=thickness)

    inner = np.zeros((h, w), dtype=np.uint8)
    outer = np.zeros((h, w), dtype=np.uint8)
    inner_r_val = max(int(r - thickness), 1)
    outer_r_val = int(r + thickness)
    cv2.circle(inner, (int(cx), int(cy)), inner_r_val, 255, thickness=thickness)
    cv2.circle(outer, (int(cx), int(cy)), outer_r_val, 255, thickness=thickness)

    ring_pixels = np.sum(ring > 0)
    if ring_pixels == 0:
        return 0.0, 0.0, 0.0

    edge_mask = (edges > 0) & (ring > 0)
    edge_on_ring = np.sum(edge_mask)
    edge_ratio = edge_on_ring / ring_pixels

    if edge_on_ring > 0:
        ys, xs = np.where(edge_mask)
        pts = np.column_stack([xs, ys]).astype(np.float64)
        arc_cov = compute_arc_coverage(pts, cx, cy, n_bins=36)
    else:
        arc_cov = 0.0

    inner_vals = v_blur[inner > 0]
    outer_vals = v_blur[outer > 0]
    if inner_vals.size == 0 or outer_vals.size == 0:
        contrast = 0.0
    else:
        contrast = float(abs(inner_vals.mean() - outer_vals.mean()))

    return edge_ratio, arc_cov, contrast


def has_border_edge(image, roi_info):
    edge_ratio, arc_cov, contrast = _border_edge_stats(image, roi_info)
    ok = (edge_ratio >= MIN_BORDER_EDGE_RATIO and
          arc_cov >= MIN_BORDER_ARC_COVERAGE and
          contrast >= MIN_BORDER_CONTRAST)
    return ok, edge_ratio, arc_cov, contrast


def detect_red_in_roi(image, roi_info):
    """Detect red clusters within the wafer ROI."""
    if not roi_info:
        return None

    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    cx, cy, r = roi_info['cx'], roi_info['cy'], roi_info['r']

    roi_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(roi_mask, (cx, cy), int(r), 255, -1)

    red_mask = np.zeros((h, w), dtype=np.uint8)
    for h_low, h_high in RED_H_RANGES:
        mask_part = cv2.inRange(hsv, np.array([h_low, RED_S_MIN, RED_V_MIN]),
                                np.array([h_high, 255, 255]))
        red_mask = cv2.bitwise_or(red_mask, mask_part)

    red_in_roi = cv2.bitwise_and(red_mask, roi_mask)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    red_clean = cv2.morphologyEx(red_in_roi, cv2.MORPH_OPEN, kernel)
    red_clean = cv2.morphologyEx(red_clean, cv2.MORPH_CLOSE, kernel)

    roi_area = np.sum(roi_mask > 0)
    red_pixels = np.sum(red_clean > 0)
    red_ratio = red_pixels / roi_area if roi_area > 0 else 0

    inner_r = int(r * (1.0 - EDGE_ZONE_RATIO))

    yy, xx = np.mgrid[0:h, 0:w]
    dx = xx - cx
    dy = yy - cy
    dist_from_center = np.sqrt(dx ** 2 + dy ** 2)
    angle = np.arctan2(dy, dx)

    edge_mask_zone = (dist_from_center >= inner_r) & (dist_from_center <= r)
    inner_mask_zone = dist_from_center < inner_r

    top_mask = (angle >= -3 * np.pi / 4) & (angle < -np.pi / 4)
    right_mask = (angle >= -np.pi / 4) & (angle < np.pi / 4)
    bottom_mask = (angle >= np.pi / 4) & (angle < 3 * np.pi / 4)
    left_mask = (angle >= 3 * np.pi / 4) | (angle < -3 * np.pi / 4)

    direction_masks = {
        'top': top_mask, 'bottom': bottom_mask,
        'left': left_mask, 'right': right_mask
    }

    zone_stats = {}
    red_binary = (red_clean > 0)

    for zone_type, zone_mask in [('edge', edge_mask_zone), ('inner', inner_mask_zone)]:
        for direction, dir_mask in direction_masks.items():
            zone_key = f"{zone_type}_{direction}"
            combined = zone_mask & dir_mask & (roi_mask > 0)
            zone_total = np.sum(combined)
            zone_red = np.sum(red_binary & combined)
            zone_density = zone_red / zone_total if zone_total > 0 else 0

            if zone_density >= 0.10:
                level = 'severe'
            elif zone_density >= 0.05:
                level = 'high'
            elif zone_density >= 0.02:
                level = 'moderate'
            else:
                level = 'normal'

            zone_stats[zone_key] = {
                'density': zone_density, 'red_pixels': int(zone_red),
                'total_pixels': int(zone_total), 'level': level
            }

    # Clusters
    contours, _ = cv2.findContours(red_clean, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    valid_clusters = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area >= MIN_CLUSTER_AREA:
            M = cv2.moments(cnt)
            if M["m00"] > 0:
                ccx = int(M["m10"] / M["m00"])
                ccy = int(M["m01"] / M["m00"])
            else:
                x_c, y_c, w_c, h_c = cv2.boundingRect(cnt)
                ccx, ccy = x_c + w_c // 2, y_c + h_c // 2

            dist = np.sqrt((ccx - cx) ** 2 + (ccy - cy) ** 2)
            zone = 'edge' if dist >= inner_r else 'inner'
            ang = np.degrees(np.arctan2(ccy - cy, ccx - cx)) % 360
            if 315 <= ang or ang < 45:
                direction = 'right'
            elif 45 <= ang < 135:
                direction = 'bottom'
            elif 135 <= ang < 225:
                direction = 'left'
            else:
                direction = 'top'

            valid_clusters.append({
                'area': area, 'centroid': (ccx, ccy),
                'contour': cnt, 'zone': zone, 'direction': direction
            })

    valid_clusters.sort(key=lambda x: x['area'], reverse=True)

    # Merge nearby clusters
    merged_clusters = []
    used = set()
    for i, cluster in enumerate(valid_clusters):
        if i in used:
            continue
        cluster_group = [cluster]
        cx1, cy1 = cluster['centroid']
        for j, other in enumerate(valid_clusters[i + 1:], i + 1):
            if j in used:
                continue
            cx2, cy2 = other['centroid']
            if np.sqrt((cx1 - cx2) ** 2 + (cy1 - cy2) ** 2) < CLUSTER_MERGE_DIST:
                cluster_group.append(other)
                used.add(j)
        total_area = sum(c['area'] for c in cluster_group)
        merged_clusters.append({
            'clusters': cluster_group, 'total_area': total_area,
            'zone': cluster_group[0]['zone'],
            'direction': cluster_group[0]['direction']
        })
        used.add(i)

    merged_clusters.sort(key=lambda x: x['total_area'], reverse=True)
    largest_cluster_area = merged_clusters[0]['total_area'] if merged_clusters else 0
    largest_cluster_ratio = largest_cluster_area / roi_area if roi_area > 0 else 0

    if largest_cluster_ratio > 0.07:
        status = "SEVERE"
        concern_level = 2
    elif largest_cluster_ratio >= 0.01:
        status = "HIGH"
        concern_level = 1
    else:
        status = "NORMAL"
        concern_level = 0

    # Edge/inner groups
    edge_groups = {}
    inner_groups = {}
    for direction in ['top', 'bottom', 'left', 'right']:
        edge_groups[direction] = zone_stats.get(f"edge_{direction}", {})
        inner_groups[direction] = zone_stats.get(f"inner_{direction}", {})

    return {
        'red_ratio': red_ratio, 'roi_area': roi_area,
        'clusters': merged_clusters, 'largest_cluster_area': largest_cluster_area,
        'largest_cluster_ratio': largest_cluster_ratio, 'status': status,
        'concern_level': concern_level, 'total_clusters': len(merged_clusters),
        'edge_groups': edge_groups, 'inner_groups': inner_groups,
        'zone_stats': zone_stats, 'inner_r': inner_r,
    }


def analyze_cluster_location(red_result, roi_info):
    """Determine main cluster location (center or edge/direction).
    Uses the LARGEST cluster's position as primary indicator.
    Direction mapping uses image coordinates (y increases downward)."""
    if not red_result or not red_result['clusters']:
        return 'center'

    cx, cy, r = roi_info['cx'], roi_info['cy'], roi_info['r']
    inner_r = int(r * 0.66)  # outer 34% = edge zone

    def get_direction(px, py):
        """Get direction from wafer center. Image coords: y increases downward."""
        ang = np.degrees(np.arctan2(py - cy, px - cx)) % 360
        if 315 <= ang or ang < 45:
            return 'right'
        elif 45 <= ang < 135:
            return 'bottom'   # y increases downward = bottom
        elif 135 <= ang < 225:
            return 'left'
        else:
            return 'top'      # y decreases upward = top

    # 1) Check the LARGEST merged cluster's weighted centroid
    largest = red_result['clusters'][0]  # already sorted by total_area
    w_x, w_y, w_total = 0.0, 0.0, 0.0
    for cl in largest['clusters']:
        a = cl['area']
        clx, cly = cl['centroid']
        w_x += clx * a
        w_y += cly * a
        w_total += a

    if w_total > 0:
        main_x = w_x / w_total
        main_y = w_y / w_total
        main_dist = np.sqrt((main_x - cx)**2 + (main_y - cy)**2)

        if main_dist >= inner_r:
            # Largest cluster is at edge → classify as edge
            main_dir = get_direction(main_x, main_y)

            # Check if multiple edges have significant clusters
            edge_dirs = set()
            for mc in red_result['clusters']:
                for cl in mc['clusters']:
                    clx, cly = cl['centroid']
                    d = np.sqrt((clx - cx)**2 + (cly - cy)**2)
                    if d >= inner_r and cl['area'] >= 50:
                        edge_dirs.add(get_direction(clx, cly))

            if len(edge_dirs) >= 3:
                return 'edge/multiple_edges'
            return f'edge/{main_dir}_edge'

    # 2) If largest cluster is center, check if significant edge area exists
    edge_area = 0
    center_area = 0
    edge_dirs = {}
    for mc in red_result['clusters']:
        for cl in mc['clusters']:
            clx, cly = cl['centroid']
            d = np.sqrt((clx - cx)**2 + (cly - cy)**2)
            if d >= inner_r:
                edge_area += cl['area']
                direction = get_direction(clx, cly)
                edge_dirs[direction] = edge_dirs.get(direction, 0) + cl['area']
            else:
                center_area += cl['area']

    total_area = edge_area + center_area
    if total_area > 0 and edge_area / total_area >= 0.3:
        # 30%+ of red area is at edge → classify as edge
        if len(edge_dirs) >= 3:
            return 'edge/multiple_edges'
        if edge_dirs:
            dominant = max(edge_dirs.keys(), key=lambda k: edge_dirs[k])
            return f'edge/{dominant}_edge'

    return 'center'


# ─────────────────────────────────────────
# Step 1: ROI Detection + Good/Bad Split
# ─────────────────────────────────────────

def step1_extract_good_bad(data_path, output_base="roi_results"):
    """
    Step 1: Detect wafer ROI, split into good (full circle) and bad (partial/none).
    Returns: dict with results summary and list of good image paths.
    """
    good_folder = os.path.join(output_base, "good_full_circle")
    bad_folder = os.path.join(output_base, "bad_partial_or_none")
    os.makedirs(good_folder, exist_ok=True)
    os.makedirs(bad_folder, exist_ok=True)

    # Load images
    patterns = [
        f'{data_path}/*.png', f'{data_path}/*.jpg', f'{data_path}/*.jpeg',
        f'{data_path}/**/*.png', f'{data_path}/**/*.jpg', f'{data_path}/**/*.jpeg',
    ]
    imgs = []
    for pattern in patterns:
        imgs.extend(sorted(glob.glob(pattern, recursive=True)))
    imgs = list(dict.fromkeys(imgs))

    good_images = []
    bad_images = []
    results = []

    for idx, img_path in enumerate(imgs):
        img_name = Path(img_path).name

        with _pipeline_lock:
            _pipeline_state["progress"] = idx + 1
            _pipeline_state["total"] = len(imgs)
            _pipeline_state["current_file"] = img_name
            _pipeline_state["step"] = "Step 1: ROI Detection"

        image = cv2.imread(img_path)
        if image is None:
            continue

        roi_info, _ = detect_wafer_roi(image)
        border_ok = False
        if roi_info:
            border_ok, edge_ratio, border_arc, border_contrast = has_border_edge(image, roi_info)

        # Mark image
        vis = image.copy()
        if roi_info:
            cxr, cyr, rr = roi_info['cx'], roi_info['cy'], roi_info['r']
            expanded_r = int(round(rr * 1.03))
            cv2.circle(vis, (cxr, cyr), expanded_r, (0, 255, 0), 3)
            cv2.circle(vis, (cxr, cyr), 5, (0, 0, 255), -1)

        is_good = (roi_info is not None and
                   roi_info.get('arc_coverage', 0) >= MIN_ARC_COVERAGE and
                   border_ok)

        if is_good:
            dest = os.path.join(good_folder, img_name)
            cv2.imwrite(dest, vis)
            good_images.append(dest)
            results.append({
                "filename": img_name, "source": img_path,
                "status": "good", "roi": True,
                "arc_coverage": roi_info['arc_coverage'],
                "green_fill": roi_info['green_fill'],
                "circularity": roi_info['circularity'],
                "radius": roi_info['r'],
            })
        else:
            dest = os.path.join(bad_folder, img_name)
            cv2.imwrite(dest, vis)
            bad_images.append(dest)
            results.append({
                "filename": img_name, "source": img_path,
                "status": "bad",
                "roi": roi_info is not None,
                "reason": "no_roi" if not roi_info else (
                    "no_border" if not border_ok else "arc_incomplete"
                ),
            })

    return {
        "total": len(imgs),
        "good_count": len(good_images),
        "bad_count": len(bad_images),
        "good_images": good_images,
        "bad_images": bad_images,
        "output_base": output_base,
        "details": results,
    }


# ─────────────────────────────────────────
# Step 2: Red Cluster Analysis
# ─────────────────────────────────────────

def step2_red_analysis(good_images, output_base):
    """
    Step 2: Analyze red clusters on good ROI images.
    Returns: dict with classification results.
    """
    # Create output folders
    folders = {
        'red_normal': os.path.join(output_base, "red_normal"),
        'red_high_center': os.path.join(output_base, "red_high_concern", "center"),
        'red_severe_center': os.path.join(output_base, "red_severe_concern", "center"),
    }
    for edge in ['top', 'bottom', 'left', 'right', 'multiple']:
        folders[f'red_high_edge_{edge}'] = os.path.join(output_base, "red_high_concern", "edge", f"{edge}_edge" if edge != 'multiple' else "multiple_edges")
        folders[f'red_severe_edge_{edge}'] = os.path.join(output_base, "red_severe_concern", "edge", f"{edge}_edge" if edge != 'multiple' else "multiple_edges")

    for fp in folders.values():
        os.makedirs(fp, exist_ok=True)

    red_counts = {
        'normal': 0,
        'high_edge_top': 0, 'high_edge_bottom': 0, 'high_edge_left': 0,
        'high_edge_right': 0, 'high_edge_multiple': 0, 'high_center': 0,
        'severe_edge_top': 0, 'severe_edge_bottom': 0, 'severe_edge_left': 0,
        'severe_edge_right': 0, 'severe_edge_multiple': 0, 'severe_center': 0,
    }

    results = []

    for idx, img_path in enumerate(good_images):
        img_name = Path(img_path).name

        with _pipeline_lock:
            _pipeline_state["progress"] = idx + 1
            _pipeline_state["total"] = len(good_images)
            _pipeline_state["current_file"] = img_name
            _pipeline_state["step"] = "Step 2: Red Analysis"

        image = cv2.imread(img_path)
        if image is None:
            continue

        roi_info, _ = detect_wafer_roi(image)
        if not roi_info:
            continue

        red_result = detect_red_in_roi(image, roi_info)
        if not red_result:
            continue

        cluster_location = analyze_cluster_location(red_result, roi_info)
        concern = red_result['concern_level']
        labels = ["NORMAL", "HIGH", "SEVERE"]

        # Determine target folder
        if concern == 0:
            count_key = 'normal'
        elif concern == 1:
            if cluster_location == 'center':
                count_key = 'high_center'
            elif cluster_location == 'edge/multiple_edges':
                count_key = 'high_edge_multiple'
            else:
                edge_type = cluster_location.split('/')[-1].replace('_edge', '')
                count_key = f'high_edge_{edge_type}'
        else:
            if cluster_location == 'center':
                count_key = 'severe_center'
            elif cluster_location == 'edge/multiple_edges':
                count_key = 'severe_edge_multiple'
            else:
                edge_type = cluster_location.split('/')[-1].replace('_edge', '')
                count_key = f'severe_edge_{edge_type}'

        target_folder = folders.get(f'red_{count_key}', folders['red_normal'])
        cv2.imwrite(os.path.join(target_folder, img_name), image)
        red_counts[count_key] = red_counts.get(count_key, 0) + 1

        results.append({
            "filename": img_name,
            "status": labels[concern],
            "concern_level": concern,
            "location": cluster_location,
            "count_key": count_key,
            "red_ratio": round(red_result['red_ratio'], 6),
            "largest_cluster_ratio": round(red_result['largest_cluster_ratio'], 6),
            "total_clusters": red_result['total_clusters'],
        })

    total = sum(red_counts.values())
    total_high = sum(v for k, v in red_counts.items() if 'high_' in k)
    total_severe = sum(v for k, v in red_counts.items() if 'severe_' in k)

    return {
        "total_analyzed": total,
        "normal": red_counts['normal'],
        "high": total_high,
        "severe": total_severe,
        "red_counts": red_counts,
        "health_score": round(red_counts['normal'] / total * 100, 1) if total > 0 else 0,
        "output_base": output_base,
        "details": results,
    }


# ─────────────────────────────────────────
# Full Pipeline: Step 1 → Step 2
# ─────────────────────────────────────────

def run_pipeline(data_path, roi_output="roi_results", wafer_output=None):
    """
    Run the full pipeline:
      data_path → Step 1 (ROI + Good/Bad) → Step 2 (Red Analysis) → Step 3 (Error Detection)
    
    Args:
        data_path:    Input folder with wafer images
        roi_output:   Where to save ROI results (step 1)
        wafer_output: Where to save wafer analysis (step 2). Auto-generated if None.
    
    Returns: Combined results dict (saved as JSON too).
    """
    global _pipeline_state

    with _pipeline_lock:
        if _pipeline_state["running"]:
            return {"error": "Pipeline is already running"}
        _pipeline_state["running"] = True
        _pipeline_state["progress"] = 0
        _pipeline_state["step"] = "Starting..."

    try:
        start_time = datetime.now()

        if wafer_output is None:
            wafer_output = "wafer_analysis"  # Fixed folder name (no timestamp)

        print(f"{'=' * 60}")
        print(f"  Unified Wafer Analysis Pipeline")
        print(f"  Input:  {data_path}")
        print(f"  ROI:    {roi_output}")
        print(f"  Output: {wafer_output}")
        print(f"{'=' * 60}\n")

        # Step 1
        print("▶ Step 1: ROI Detection + Good/Bad Classification...")
        step1_result = step1_extract_good_bad(data_path, roi_output)
        print(f"  ✅ Good: {step1_result['good_count']} | Bad: {step1_result['bad_count']}")

        # Step 2 - use good images from step 1
        good_folder = os.path.join(roi_output, "good_full_circle")
        good_imgs = []
        for ext in ['*.png', '*.jpg', '*.jpeg']:
            good_imgs.extend(glob.glob(os.path.join(good_folder, ext)))
        good_imgs = sorted(list(set(good_imgs)))

        print(f"\n▶ Step 2: Red Cluster Analysis on {len(good_imgs)} good images...")
        step2_result = step2_red_analysis(good_imgs, wafer_output)
        print(f"  ✅ Normal: {step2_result['normal']} | High: {step2_result['high']} | Severe: {step2_result['severe']}")
        print(f"  Health Score: {step2_result['health_score']}%")

        # Step 3 - Error Detection (OCR + classify using PostgreSQL error codes)
        step3_result = None
        try:
            import importlib
            simple = importlib.import_module("3_simple")
            # Reload error codes from PostgreSQL
            simple.ERROR_DB = simple.load_error_db()

            with _pipeline_lock:
                _pipeline_state["step"] = "Step 3: Error Detection"
                _pipeline_state["progress"] = 0

            print(f"\n▶ Step 3: Error Detection (OCR + classify)...")
            data_error_path = simple.DATA_PATH
            if os.path.isdir(data_error_path):
                out = simple.make_output(simple.OUTPUT_BASE)
                machines = simple.discover_machines(data_error_path)
                print(f"  Found {len(machines)} machine(s): {', '.join(m[0] for m in machines)}")

                exts = ["*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG", "*.bmp"]
                all_err_results = []

                for machine_name, machine_path in machines:
                    machine_out = simple.make_machine_dirs(out, machine_name)
                    files = sorted({f for e in exts
                                    for f in glob.glob(os.path.join(machine_path, e))})
                    if not files:
                        continue

                    machine_results = []
                    for idx, fp in enumerate(files, 1):
                        with _pipeline_lock:
                            _pipeline_state["progress"] = idx
                            _pipeline_state["total"] = len(files)
                            _pipeline_state["current_file"] = os.path.basename(fp)
                        r = simple.process_image(fp, machine_out, machine_name=machine_name)
                        machine_results.append(r)

                    simple.gen_reports(machine_results, machine_out, machine_name=machine_name)
                    all_err_results.extend(machine_results)

                # Overall report
                simple.gen_overall_report(all_err_results, out)
                valid = [r for r in all_err_results if r]
                clf = [r for r in valid if r.get("category") != "Unknown"]
                step3_result = {
                    "total_images": len(all_err_results),
                    "detected": len(valid),
                    "classified": len(clf),
                    "machines": len(machines),
                    "output_folder": out,
                }
                print(f"  ✅ Detected: {len(valid)} errors, Classified: {len(clf)}/{len(valid)} across {len(machines)} machine(s)")
            else:
                print(f"  ⚠ data_error folder not found: {data_error_path}, skipping error detection")
        except Exception as e:
            print(f"  ⚠ Error Detection (Step 3) failed: {e}")
            import traceback
            traceback.print_exc()

        elapsed = (datetime.now() - start_time).total_seconds()

        # Combined result
        combined = {
            "timestamp": start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": round(elapsed, 1),
            "data_path": data_path,
            "roi_output": roi_output,
            "wafer_output": wafer_output,
            "step1": {
                "total": step1_result["total"],
                "good": step1_result["good_count"],
                "bad": step1_result["bad_count"],
            },
            "step2": {
                "total_analyzed": step2_result["total_analyzed"],
                "normal": step2_result["normal"],
                "high": step2_result["high"],
                "severe": step2_result["severe"],
                "health_score": step2_result["health_score"],
                "red_counts": step2_result["red_counts"],
            },
            "details": {
                "step1": step1_result["details"],
                "step2": step2_result["details"],
            },
        }

        # Include Step 3 result if available
        if step3_result:
            combined["step3"] = step3_result

        # Save JSON result
        result_json = os.path.join(wafer_output, "pipeline_result.json")
        with open(result_json, 'w', encoding='utf-8') as f:
            json.dump(combined, f, indent=2, ensure_ascii=False, default=str)

        # Also save a "latest" symlink/copy for dashboard
        latest_json = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_pipeline_result.json")
        with open(latest_json, 'w', encoding='utf-8') as f:
            json.dump(combined, f, indent=2, ensure_ascii=False, default=str)

        print(f"\n{'=' * 60}")
        print(f"  ✅ Pipeline Complete! ({elapsed:.1f}s)")
        print(f"  Results: {result_json}")
        print(f"{'=' * 60}")

        with _pipeline_lock:
            _pipeline_state["last_run"] = combined["timestamp"]
            _pipeline_state["last_result"] = combined

        return combined

    finally:
        with _pipeline_lock:
            _pipeline_state["running"] = False
            _pipeline_state["step"] = "Done"


# ─────────────────────────────────────────
# CLI
# ─────────────────────────────────────────

if __name__ == "__main__":
    import sys

    data = sys.argv[1] if len(sys.argv) > 1 else "data_merged"
    
    if not os.path.isdir(data):
        print(f"❌ Data path not found: {data}")
        print(f"Usage: python pipeline.py <data_folder>")
        sys.exit(1)

    result = run_pipeline(data)
    if "error" in result:
        print(f"❌ {result['error']}")
    else:
        print(f"\n📊 Health Score: {result['step2']['health_score']}%")
