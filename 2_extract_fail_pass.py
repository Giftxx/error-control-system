# ==============================
# Production Code: Wafer Analysis Pipeline
# ==============================

import cv2
import numpy as np
import os
from datetime import datetime
from pathlib import Path
import glob

# === Configuration ===
DATA_PATH = r'C:\Users\chama\Desktop\Detect_Na\roi_results_20260218_145534\good_full_circle'  # Input path

# ROI Detection Parameters
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

# Red Analysis Parameters
RED_H_RANGES = [(0, 10), (160, 179)]
RED_S_MIN, RED_V_MIN = 100, 80
MIN_CLUSTER_AREA = 30          # ลดลงเพื่อจับจุดเล็กได้
MAX_SCATTERED_RATIO = 0.3
CLUSTER_MERGE_DIST = 25        # เพิ่มเพื่อรวมจุดใกล้กันได้ดีขึ้น
EDGE_ZONE_RATIO = 0.20         # ขอบ = 20% นอกสุดของรัศมี
ZONE_RED_THRESHOLD = 0.02      # ถ้าโซนใดมีสีแดง >= 2% ถือว่ามี cluster
ZONE_HIGH_THRESHOLD = 0.05     # >= 5% ถือว่ากระจุกตัวหนัก
ZONE_SEVERE_THRESHOLD = 0.10   # >= 10% ถือว่ารุนแรง

# === Core Functions ===
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
    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    
    gmask = cv2.inRange(hsv, np.array([GREEN_H_LOW, GREEN_S_LOW, GREEN_V_LOW]),
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

        inlier_pts = pts
        for _ in range(3):
            dists = np.sqrt((inlier_pts[:, 0] - cx)**2 + (inlier_pts[:, 1] - cy)**2)
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

        dists_final = np.sqrt((inlier_pts[:, 0] - cx)**2 + (inlier_pts[:, 1] - cy)**2)
        rmse = float(np.sqrt(np.mean((dists_final - r)**2)))

        candidates.append({
            'cx': int(round(cx)), 'cy': int(round(cy)), 'r': int(round(r)),
            'area': area, 'circularity': circ, 'green_fill': green_fill, 
            'rmse': rmse, 'arc_coverage': arc_cov, 'contour': cnt,
        })

    if not candidates:
        return None, clean
    
    max_r = max(c['r'] for c in candidates)
    for c in candidates:
        dist_to_center = np.sqrt((c['cx'] - w/2)**2 + (c['cy'] - h/2)**2)
        max_dist = np.sqrt((w/2)**2 + (h/2)**2)
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
    inner_r = max(int(r - thickness), 1)
    outer_r = int(r + thickness)
    cv2.circle(inner, (int(cx), int(cy)), inner_r, 255, thickness=thickness)
    cv2.circle(outer, (int(cx), int(cy)), outer_r, 255, thickness=thickness)

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
    ok = (edge_ratio >= MIN_BORDER_EDGE_RATIO) and (arc_cov >= MIN_BORDER_ARC_COVERAGE) and (contrast >= MIN_BORDER_CONTRAST)
    return ok, edge_ratio, arc_cov, contrast

def detect_red_in_roi(image, roi_info):
    if not roi_info:
        return None
    
    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    
    cx, cy, r = roi_info['cx'], roi_info['cy'], roi_info['r']
    
    # ใช้ full radius เพื่อจับสีแดงที่ขอบวงกลมได้
    roi_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(roi_mask, (cx, cy), int(r), 255, -1)
    
    # --- สร้าง red mask ---
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
    
    # --- สร้าง zone masks (8 โซน: edge/inner x top/bottom/left/right) ---
    inner_r = int(r * (1.0 - EDGE_ZONE_RATIO))  # รัศมีเขตด้านใน
    
    # สร้าง coordinate grid
    yy, xx = np.mgrid[0:h, 0:w]
    dx = xx - cx
    dy = yy - cy
    dist_from_center = np.sqrt(dx**2 + dy**2)
    angle = np.arctan2(dy, dx)  # -π to π
    
    # Edge zone: inner_r <= dist <= r
    edge_mask = (dist_from_center >= inner_r) & (dist_from_center <= r)
    # Inner zone: dist < inner_r
    inner_mask = dist_from_center < inner_r
    
    # Direction masks (แบ่ง 4 ทิศ)
    top_mask    = (angle >= -3*np.pi/4) & (angle < -np.pi/4)     # บน
    right_mask  = (angle >= -np.pi/4) & (angle < np.pi/4)        # ขวา
    bottom_mask = (angle >= np.pi/4) & (angle < 3*np.pi/4)       # ล่าง
    left_mask   = (angle >= 3*np.pi/4) | (angle < -3*np.pi/4)    # ซ้าย
    
    direction_masks = {
        'top': top_mask, 'bottom': bottom_mask,
        'left': left_mask, 'right': right_mask
    }
    
    # --- คำนวณสีแดงในแต่ละโซน ---
    zone_stats = {}
    red_binary = (red_clean > 0)
    
    for zone_type, zone_mask in [('edge', edge_mask), ('inner', inner_mask)]:
        for direction, dir_mask in direction_masks.items():
            zone_key = f"{zone_type}_{direction}"
            combined = zone_mask & dir_mask & (roi_mask > 0)
            zone_total = np.sum(combined)
            zone_red = np.sum(red_binary & combined)
            zone_density = zone_red / zone_total if zone_total > 0 else 0
            
            # จัดระดับความรุนแรงของแต่ละโซน
            if zone_density >= ZONE_SEVERE_THRESHOLD:
                zone_level = 'severe'
            elif zone_density >= ZONE_HIGH_THRESHOLD:
                zone_level = 'high'
            elif zone_density >= ZONE_RED_THRESHOLD:
                zone_level = 'moderate'
            else:
                zone_level = 'normal'
            
            zone_stats[zone_key] = {
                'total_pixels': int(zone_total),
                'red_pixels': int(zone_red),
                'density': float(zone_density),
                'level': zone_level
            }
    
    # --- สรุปกลุ่มขอบและกลุ่มด้านใน ---
    edge_groups = {}
    inner_groups = {}
    for direction in ['top', 'bottom', 'left', 'right']:
        e_key = f"edge_{direction}"
        i_key = f"inner_{direction}"
        edge_groups[direction] = zone_stats[e_key]
        inner_groups[direction] = zone_stats[i_key]
    
    # --- หา clusters (contours) สำหรับ visualization ---
    contours, _ = cv2.findContours(red_clean, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    valid_clusters = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area >= MIN_CLUSTER_AREA:
            M = cv2.moments(cnt)
            if M['m00'] > 0:
                centroid_x = int(M['m10'] / M['m00'])
                centroid_y = int(M['m01'] / M['m00'])
                # ตรวจว่า cluster นี้อยู่ edge หรือ inner
                cdist = np.sqrt((centroid_x - cx)**2 + (centroid_y - cy)**2)
                cangle = np.arctan2(centroid_y - cy, centroid_x - cx)
                is_edge = cdist >= inner_r
                
                if -3*np.pi/4 <= cangle < -np.pi/4:
                    direction = 'top'
                elif -np.pi/4 <= cangle < np.pi/4:
                    direction = 'right'
                elif np.pi/4 <= cangle < 3*np.pi/4:
                    direction = 'bottom'
                else:
                    direction = 'left'
                
                zone_type = 'edge' if is_edge else 'inner'
                valid_clusters.append({
                    'contour': cnt, 'area': area,
                    'centroid': (centroid_x, centroid_y),
                    'zone': zone_type, 'direction': direction
                })
    
    valid_clusters.sort(key=lambda x: x['area'], reverse=True)
    
    # --- Merge nearby clusters ---
    merged_clusters = []
    used = set()
    for i, cluster in enumerate(valid_clusters):
        if i in used:
            continue
        cluster_group = [cluster]
        cx1, cy1 = cluster['centroid']
        for j, other in enumerate(valid_clusters[i+1:], i+1):
            if j in used:
                continue
            cx2, cy2 = other['centroid']
            dist = np.sqrt((cx1 - cx2)**2 + (cy1 - cy2)**2)
            if dist <= CLUSTER_MERGE_DIST:
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
    
    # --- ประเมินระดับความรุนแรงรวม ---
    # นับจำนวนโซนที่มีปัญหา
    severe_zones = [k for k, v in zone_stats.items() if v['level'] == 'severe']
    high_zones   = [k for k, v in zone_stats.items() if v['level'] == 'high']
    moderate_zones = [k for k, v in zone_stats.items() if v['level'] == 'moderate']
    
    if len(severe_zones) >= 1 or red_ratio > MAX_SCATTERED_RATIO:
        status = "SEVERE"
        concern_level = 3
    elif len(high_zones) >= 2 or largest_cluster_ratio > 0.035:
        status = "HIGH"
        concern_level = 2
    elif len(high_zones) >= 1 or len(moderate_zones) >= 3 or red_ratio > 0.05:
        status = "MODERATE" 
        concern_level = 1
    elif len(moderate_zones) >= 1:
        status = "LOW"
        concern_level = 1
    elif red_ratio <= 0.01:
        status = "NORMAL"
        concern_level = 0
    else:
        status = "NORMAL"
        concern_level = 0
    
    # สร้าง zone summary string
    zone_summary = []
    for k, v in zone_stats.items():
        if v['level'] != 'normal':
            zone_summary.append(f"{k}={v['density']:.1%}({v['level']})")
    
    return {
        'red_mask': red_clean, 'red_ratio': red_ratio, 'roi_area': roi_area,
        'clusters': merged_clusters, 'largest_cluster_area': largest_cluster_area,
        'largest_cluster_ratio': largest_cluster_ratio, 'status': status,
        'concern_level': concern_level, 'total_clusters': len(merged_clusters),
        'edge_groups': edge_groups, 'inner_groups': inner_groups,
        'zone_stats': zone_stats, 'zone_summary': zone_summary,
        'severe_zones': severe_zones, 'high_zones': high_zones,
        'moderate_zones': moderate_zones,
        'inner_r': inner_r
    }

# === Main Processing Pipeline ===
def main():
    output_base = f"wafer_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    # Create output folders (Only red analysis - input already has good ROI)
    red_normal = os.path.join(output_base, "red_normal")
    red_moderate = os.path.join(output_base, "red_moderate_concern")
    red_high = os.path.join(output_base, "red_high_concern")
    red_severe = os.path.join(output_base, "red_severe_concern")
    
    for folder in [red_normal, red_moderate, red_high, red_severe]:
        os.makedirs(folder, exist_ok=True)
    
    # Load images (already filtered good ROI from previous step)
    patterns = [f'{DATA_PATH}/*.png', f'{DATA_PATH}/*.jpg', f'{DATA_PATH}/*.jpeg']
    imgs = []
    for pattern in patterns:
        imgs.extend(sorted(glob.glob(pattern)))
    imgs = list(dict.fromkeys(imgs))
    
    # Counters
    red_counts = [0, 0, 0, 0]  # normal, moderate, high, severe
    
    print(f"Processing {len(imgs)} images from: {DATA_PATH}")
    print("=" * 60)
    
    for img_path in imgs:
        img_name = Path(img_path).name
        image = cv2.imread(img_path)
        if image is None:
            print(f"❌ ไม่สามารถโหลดรูป: {img_name}")
            continue
        
        # ROI Detection
        roi_info, _ = detect_wafer_roi(image)
        
        if roi_info:
            # Red Analysis
            red_result = detect_red_in_roi(image, roi_info)
            if red_result:
                vis_red = image.copy()
                cx, cy, r = roi_info['cx'], roi_info['cy'], roi_info['r']
                inner_r = red_result.get('inner_r', int(r * 0.80))
                
                # ไม่วาดวงกลม ROI สีฟ้า
                cv2.circle(vis_red, (cx, cy), 5, (0, 0, 255), -1)
                
                # # วาดวงแบ่ง edge/inner zone (วงกลมด้านใน)
                # cv2.circle(vis_red, (cx, cy), inner_r, (255, 255, 0), 1)
                
                # วาดเส้นแบ่ง 4 ทิศ (เส้นทแยง 45°)
                line_len = int(r * 1.05)
                for ang in [-3*np.pi/4, -np.pi/4, np.pi/4, 3*np.pi/4]:
                    x2 = int(cx + line_len * np.cos(ang))
                    y2 = int(cy + line_len * np.sin(ang))
                    cv2.line(vis_red, (cx, cy), (x2, y2), (255, 255, 0), 1)
                
                # วาด cluster contours แต่ละอันด้วยสีตามระดับ
                zone_colors = {'normal': (0, 255, 0), 'moderate': (0, 255, 255),
                               'high': (0, 165, 255), 'severe': (0, 0, 255)}
                for cl in red_result['clusters']:
                    for c in cl['clusters']:
                        zone_key = f"{c['zone']}_{c['direction']}"
                        zs = red_result['zone_stats'].get(zone_key, {})
                        color = zone_colors.get(zs.get('level', 'normal'), (0, 255, 0))
                        cv2.drawContours(vis_red, [c['contour']], -1, color, 2)
                
                concern = red_result['concern_level']
                colors = [(0, 255, 0), (0, 255, 255), (0, 165, 255), (0, 0, 255)]
                labels = ["NORMAL", "MODERATE", "HIGH", "SEVERE"]
                
                # แสดงสถานะรวม
                cv2.putText(vis_red, f"Red: {labels[concern]} ({red_result['status']})", (10, 30), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, colors[concern], 2)
                cv2.putText(vis_red, f"Ratio:{red_result['red_ratio']:.1%} Clusters:{red_result['total_clusters']}", 
                           (10, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colors[concern], 2)
                
                # แสดงโซนที่มีปัญหา
                y_pos = 80
                for zone_key in sorted(red_result['zone_stats'].keys()):
                    zs = red_result['zone_stats'][zone_key]
                    if zs['level'] != 'normal':
                        zcolor = zone_colors[zs['level']]
                        cv2.putText(vis_red, f"{zone_key}: {zs['density']:.1%} [{zs['level']}]",
                                   (10, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.45, zcolor, 1)
                        y_pos += 20
                
                # Save to appropriate folder
                folders = [red_normal, red_moderate, red_high, red_severe]
                cv2.imwrite(os.path.join(folders[concern], img_name), vis_red)
                red_counts[concern] += 1
                
                # Print detail
                zone_info = ", ".join(red_result['zone_summary']) if red_result['zone_summary'] else "all normal"
                print(f"{labels[concern]}: {img_name} (red={red_result['red_ratio']:.1%}) [{zone_info}]")
        else:
            print(f"⚠️ ไม่เจอ ROI: {img_name}")
    
    # Summary
    total = sum(red_counts)
    print("\n" + "=" * 60)
    print("✅ Processing Complete!")
    print(f"Results saved to: {output_base}")
    print(f"\n🔴 Red Analysis Results:")
    print(f"  ✅ Normal:    {red_counts[0]} files")
    print(f"  ⚠️ Moderate:  {red_counts[1]} files")
    print(f"  🔶 High:      {red_counts[2]} files") 
    print(f"  ❌ Severe:    {red_counts[3]} files")
    
    if total > 0:
        print(f"\n  Health score: {red_counts[0]/total*100:.1f}% normal")
    
    # Save summary report
    summary_file = os.path.join(output_base, "analysis_summary.txt")
    with open(summary_file, 'w', encoding='utf-8') as f:
        f.write(f"Wafer Analysis Summary - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Input: {DATA_PATH}\n")
        f.write(f"Total images processed: {len(imgs)}\n")
        f.write(f"Red Normal: {red_counts[0]}, Moderate: {red_counts[1]}, High: {red_counts[2]}, Severe: {red_counts[3]}\n")
        f.write(f"\nZone Detection:\n")
        f.write(f"  Edge zone = outer {int(EDGE_ZONE_RATIO*100)}% of radius\n")
        f.write(f"  Inner zone = inner {int((1-EDGE_ZONE_RATIO)*100)}% of radius\n")
        f.write(f"  Directions: top / bottom / left / right (divided at 45 deg)\n")
        f.write(f"  Thresholds: moderate>={ZONE_RED_THRESHOLD:.0%}, high>={ZONE_HIGH_THRESHOLD:.0%}, severe>={ZONE_SEVERE_THRESHOLD:.0%}\n")
        if total > 0:
            f.write(f"\nHealth score: {red_counts[0]/total*100:.1f}% normal\n")
    
    print(f"\n📄 Summary saved: {summary_file}")

# Run the analysis
if __name__ == "__main__":
    main()