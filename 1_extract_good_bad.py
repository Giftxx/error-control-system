# ==============================
# 1) Import Libraries
# ==============================

import cv2
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
import glob
import os
from datetime import datetime

plt.rcParams['figure.figsize'] = (14, 6)
plt.rcParams['figure.dpi'] = 100


# ==============================
# 2) Show Image Function
# ==============================

def show(img, title='', cmap=None):
    """
    แสดงภาพ:
    - ถ้าเป็นภาพจาก OpenCV (BGR) จะ convert เป็น RGB ก่อน
    - ถ้าเป็นภาพขาวดำ จะแสดงแบบ gray
    """
    
    if img is None:
        print("⚠️ Image is None")
        return

    if len(img.shape) == 3:
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        plt.imshow(img_rgb)
    else:
        plt.imshow(img, cmap=cmap or 'gray')

    plt.title(title)
    plt.axis('off')
    plt.show()


print("✅ Import and setup complete")

# === แก้ตรงนี้ ===
DATA_PATH = r'C:\Users\chama\Desktop\Detect_Na\data_merged'  #
MACHINE = ''                     # ว่าง = เอาทั้งหมด, หรือระบุ: wftb33-01, wftb33-02, wftb33-13, wftb33-14

# โหลดรูป - รองรับทั้งแบบมี subfolder และไม่มี
if MACHINE:
    # ถ้าระบุเครื่อง
    imgs_with_folder = sorted(glob.glob(f'{DATA_PATH}/{MACHINE}/*.png'))
    imgs_no_folder = sorted(glob.glob(f'{DATA_PATH}/*{MACHINE}*.png'))
    imgs = imgs_with_folder if imgs_with_folder else imgs_no_folder
    print(f"พบ {len(imgs)} รูปของ {MACHINE} ใน {DATA_PATH}\n")
else:
    # ถ้าไม่ระบุเครื่อง = เอาทั้งหมด
    # Try multiple file extensions and patterns
    patterns = [
        f'{DATA_PATH}/*.png',
        f'{DATA_PATH}/*.jpg',
        f'{DATA_PATH}/*.jpeg',
        f'{DATA_PATH}/**/*.png',
        f'{DATA_PATH}/**/*.jpg',
        f'{DATA_PATH}/**/*.jpeg'
    ]
    
    imgs = []
    for pattern in patterns:
        found_imgs = sorted(glob.glob(pattern, recursive=True))
        imgs.extend(found_imgs)
    
    # Remove duplicates while preserving order
    imgs = list(dict.fromkeys(imgs))
    print(f"พบ {len(imgs)} รูปทั้งหมดใน {DATA_PATH}\n")

if len(imgs) == 0:
    print(f"⚠️ ไม่พบรูปภาพ! ตรวจสอบ DATA_PATH และ MACHINE")
    print(f"ตรวจสอบ path: {DATA_PATH}")
    print(f"ตรวจสอบว่ามีไฟล์ .png .jpg .jpeg ในโฟลเดอร์หรือไม่")
else:
    print("พบรูปภาพ กำลังประมวลผล...")

# --- Parameters สำหรับ ROI Detection ---
GREEN_H_LOW, GREEN_H_HIGH = 35, 85    # แค่ pure green ไม่รวม cyan (80-105)
GREEN_S_LOW, GREEN_V_LOW = 50, 50

MIN_CIRCULARITY = 0.65   # ลดลงเพื่อรองรับวงกลมที่มีเส้นติดอยู่ (เดิม 0.78)
MIN_AREA_RATIO  = 0.005   # ลดลงเพื่อให้เจอง่ายขึ้น (เดิม 0.01)
MIN_ARC_COVERAGE = 0.75   # contour ต้องครอบคลุมอย่างน้อย 75% ของ 360° (ไม่เอาครึ่งวงกลม)


# ============================================================
#  Least-Squares Circle Fit  (Kasa method + centering)
# ============================================================
def least_squares_circle_fit(points):
    """
    Fit วงกลมจากจุด 2D ด้วย algebraic least-squares (Kasa).
    Input : Nx2 array (x, y)
    Return: (cx, cy, r)
    """
    x = points[:, 0].astype(np.float64)
    y = points[:, 1].astype(np.float64)

    # Center data เพื่อลด numerical error
    mx, my = np.mean(x), np.mean(y)
    u, v = x - mx, y - my

    A = np.column_stack([u, v, np.ones_like(u)])
    b = u**2 + v**2

    result, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    uc = result[0] / 2
    vc = result[1] / 2
    r  = np.sqrt(max(result[2] + uc**2 + vc**2, 0))

    return float(uc + mx), float(vc + my), float(r)


# ============================================================
#  Arc Coverage — ตรวจว่า contour ครอบคลุมวงกลมกี่ %
# ============================================================
def compute_arc_coverage(pts, cx, cy, n_bins=36):
    """
    คำนวณว่า contour points ครอบคลุมกี่ % ของวงกลม 360°
    แบ่งวงกลมเป็น n_bins ส่วน (default 36 = ส่วนละ 10°)
    Return: coverage ratio (0.0 - 1.0)
    """
    angles = np.arctan2(pts[:, 1] - cy, pts[:, 0] - cx)  # -π to π
    angles = np.mod(angles, 2 * np.pi)  # 0 to 2π
    bins = (angles / (2 * np.pi) * n_bins).astype(int)
    bins = np.clip(bins, 0, n_bins - 1)
    covered = len(np.unique(bins))
    return covered / n_bins


# ============================================================
#  Detect Wafer ROI — contour + LS circle fit + RANSAC refine
# ============================================================
def detect_wafer_roi(image, debug=False):
    """
    Pipeline:
      1. Green mask -> morphological cleanup
      2. findContours -> filter by area & circularity
      3. Least-squares circle fit on contour edge points
      4. RANSAC-style outlier rejection -> refit
      5. Arc coverage check — reject ครึ่งวงกลม
      6. Score by green_fill x circularity x size
    Returns: (best_dict, clean_mask) or (None, clean_mask)
    """
    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    
    if debug:
        print(f"  Image size: {w}x{h}  |  min_area={h*w*MIN_AREA_RATIO:.0f}px  |  min_circ={MIN_CIRCULARITY}  |  min_arc={MIN_ARC_COVERAGE:.0%}")

    # --- Green mask ---
    gmask = cv2.inRange(hsv,
        np.array([GREEN_H_LOW, GREEN_S_LOW, GREEN_V_LOW]),
        np.array([GREEN_H_HIGH, 255, 255]))

    # --- Morphological cleanup (ลด iterations เพื่อไม่ให้เชื่อม noise มากเกินไป) ---
    kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    clean = cv2.morphologyEx(gmask, cv2.MORPH_CLOSE, kern, iterations=2)
    clean = cv2.morphologyEx(clean, cv2.MORPH_OPEN, kern, iterations=1)

    # --- Find contours ---
    contours, _ = cv2.findContours(clean, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    
    if debug:
        print(f"  Found {len(contours)} contours total")

    min_area = h * w * MIN_AREA_RATIO
    candidates = []

    for idx, cnt in enumerate(contours):
        area = cv2.contourArea(cnt)
        if area < min_area:
            if debug and area > 100:
                print(f"    #{idx}: area={area:.0f} < {min_area:.0f} ❌ too small")
            continue

        peri = cv2.arcLength(cnt, True)
        if peri == 0:
            continue

        circ = 4 * np.pi * area / (peri ** 2)
        if circ < MIN_CIRCULARITY:
            if debug:
                print(f"    #{idx}: area={area:.0f} circ={circ:.3f} < {MIN_CIRCULARITY} ❌ not circular")
            continue

        pts = cnt.reshape(-1, 2).astype(np.float64)
        if len(pts) < 30:
            continue

        # --- Initial least-squares fit ---
        cx, cy, r = least_squares_circle_fit(pts)

        if r < 50 or r > min(h, w) // 2:
            continue

        # --- RANSAC-style: reject outlier points -> refit (ปรับ threshold ให้หละหลวมกว่า) ---
        inlier_pts = pts
        for _ in range(3):
            dists = np.sqrt((inlier_pts[:, 0] - cx)**2 + (inlier_pts[:, 1] - cy)**2)
            residuals = np.abs(dists - r)
            thr = max(r * 0.10, 5)       # เพิ่มเป็น 10% และ 5px (เดิม 6% และ 4px)
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

        # --- Validate ---
        if r < 50 or r > min(h, w) // 2:
            continue
        if not (0 <= cx < w and 0 <= cy < h):
            continue

        # --- Arc Coverage Check: ต้องเป็นวงกลมเต็มวง ---
        arc_cov = compute_arc_coverage(pts, cx, cy)
        if arc_cov < MIN_ARC_COVERAGE:
            if debug:
                print(f"    #{idx}: area={area:.0f} circ={circ:.3f} arc={arc_cov:.0%} < {MIN_ARC_COVERAGE:.0%} ❌ ไม่เต็มวง (ครึ่งวง/บางส่วน)")
            continue

        # --- Green fill ratio ---
        cmask = np.zeros((h, w), dtype=np.uint8)
        cv2.circle(cmask, (int(round(cx)), int(round(cy))), int(round(r)), 255, -1)
        green_in = np.sum((gmask > 0) & (cmask > 0))
        total_in = np.sum(cmask > 0)
        green_fill = green_in / total_in if total_in > 0 else 0

        # --- Fit RMSE (low = accurate) ---
        dists_final = np.sqrt((inlier_pts[:, 0] - cx)**2 + (inlier_pts[:, 1] - cy)**2)
        rmse = float(np.sqrt(np.mean((dists_final - r)**2)))

        candidates.append({
            'cx': int(round(cx)), 'cy': int(round(cy)), 'r': int(round(r)),
            'area': area, 'circularity': circ,
            'green_fill': green_fill, 'rmse': rmse,
            'arc_coverage': arc_cov,
            'n_inliers': len(inlier_pts), 'n_total': len(pts),
            'contour': cnt,
        })
        
        if debug:
            print(f"    #{idx}: area={area:.0f} circ={circ:.3f} r={int(round(r))} gfill={green_fill:.1%} arc={arc_cov:.0%} rmse={rmse:.1f} ✅ PASS")

    if not candidates:
        # --- Fallback: ถ้าไม่เจอด้วยเงื่อนไขเข้มงวด ลองหาแบบหละหลวมกว่า ---
        print("  [Fallback] ไม่เจอด้วยเงื่อนไขปกติ ลองใช้เงื่อนไขหละหลวมกว่า...")
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < h * w * 0.003:  # ต้อง > 0.3% ของภาพ
                continue
            
            pts = cnt.reshape(-1, 2).astype(np.float64)
            if len(pts) < 20:
                continue
            
            cx, cy, r = least_squares_circle_fit(pts)
            if r < 30 or r > min(h, w) // 2:
                continue
            if not (0 <= cx < w and 0 <= cy < h):
                continue
            
            # --- Arc Coverage Check (fallback ก็ต้องเต็มวง) ---
            arc_cov = compute_arc_coverage(pts, cx, cy)
            if arc_cov < MIN_ARC_COVERAGE:
                if debug:
                    print(f"    [FB] arc={arc_cov:.0%} < {MIN_ARC_COVERAGE:.0%} ❌ ไม่เต็มวง")
                continue
            
            # ตรวจสอบว่าใกล้กลางภาพ (ไม่ชิดขอบเกินไป)
            dist_to_center = np.sqrt((cx - w/2)**2 + (cy - h/2)**2)
            max_dist = min(w, h) * 0.4
            if dist_to_center > max_dist:
                continue
            
            # ตรวจสอบ green fill
            cmask = np.zeros((h, w), dtype=np.uint8)
            cv2.circle(cmask, (int(round(cx)), int(round(cy))), int(round(r)), 255, -1)
            green_in = np.sum((gmask > 0) & (cmask > 0))
            total_in = np.sum(cmask > 0)
            green_fill = green_in / total_in if total_in > 0 else 0
            
            if green_fill < 0.3:  # ต้องมีสีเขียวอย่างน้อย 30%
                continue
            
            peri = cv2.arcLength(cnt, True)
            circ = 4 * np.pi * area / (peri ** 2) if peri > 0 else 0
            
            candidates.append({
                'cx': int(round(cx)), 'cy': int(round(cy)), 'r': int(round(r)),
                'area': area, 'circularity': circ,
                'green_fill': green_fill, 'rmse': 0,
                'arc_coverage': arc_cov,
                'n_inliers': len(pts), 'n_total': len(pts),
                'contour': cnt,
            })
        
        if not candidates:
            return None, clean
    
    # --- Score: green fill + circularity + relative size + position (ยิ่งใกล้กลางยิ่งดี) ---
    max_r = max(c['r'] for c in candidates)
    for c in candidates:
        # ระยะจากกลางภาพ (normalized)
        dist_to_center = np.sqrt((c['cx'] - w/2)**2 + (c['cy'] - h/2)**2)
        max_dist = np.sqrt((w/2)**2 + (h/2)**2)
        center_score = 1.0 - (dist_to_center / max_dist)  # 1=กลางสนิท, 0=มุม
        
        c['score'] = (c['green_fill']  * 0.35 +
                      c['circularity'] * 0.25 +
                      (c['r'] / max(max_r, 1)) * 0.25 +
                      center_score * 0.15)  # เพิ่มคะแนนถ้าอยู่ใกล้กลางภาพ

    best = max(candidates, key=lambda x: x['score'])
    return best, clean


# ==============================
# 7) Save ROI Results to Folders
# ==============================

# สร้าง output folders
output_base = f"roi_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
good_folder = os.path.join(output_base, "good_full_circle")
bad_folder = os.path.join(output_base, "bad_partial_or_none")

os.makedirs(good_folder, exist_ok=True)
os.makedirs(bad_folder, exist_ok=True)

# สร้าง summary report
summary_file = os.path.join(output_base, "roi_detection_summary.txt")

print("กำลังประมวลผลและแยกไฟล์...")
print(f"Output folder: {output_base}")
print("=" * 80)

good_count = 0
bad_count = 0
summary_lines = []

summary_lines.append(f"ROI Detection Summary - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
summary_lines.append(f"Total images processed: {len(imgs)}")
summary_lines.append(f"Criteria: Arc coverage ≥ {MIN_ARC_COVERAGE:.0%} (full circle)")
summary_lines.append("Criteria: Border edge must exist and be continuous")
summary_lines.append("=" * 80)

MIN_BORDER_EDGE_RATIO = 0.08
MIN_BORDER_ARC_COVERAGE = 0.65
MIN_BORDER_CONTRAST = 18
BORDER_THICKNESS = 3
CANNY_LOW = 60
CANNY_HIGH = 180

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

for idx, img_path in enumerate(imgs):
    img_name = Path(img_path).name
    _img = cv2.imread(img_path)

    if _img is None:
        print(f"❌ ไม่สามารถโหลดรูป: {img_name}")
        continue

    # ตรวจจับ ROI
    roi_info, _ = detect_wafer_roi(_img, debug=False)
    border_ok, edge_ratio, border_arc, border_contrast = has_border_edge(_img, roi_info) if roi_info else (False, 0.0, 0.0, 0.0)

    # สร้างภาพ mark
    vis = _img.copy()
    if roi_info:
        cx, cy, r = roi_info['cx'], roi_info['cy'], roi_info['r']
        arc_coverage = roi_info.get('arc_coverage', 0)
        # ขยายกรอบ contour วงกลมให้ใหญ่ขึ้นเล็กน้อย
        expanded_r = int(round(r * 1.03))  # ขยายรัศมีขึ้น 3%
        cv2.circle(vis, (cx, cy), expanded_r, (0, 255, 0), 3)
        cv2.circle(vis, (cx, cy), 5, (0, 0, 255), -1)

        arc_color = (0, 255, 0) if arc_coverage >= MIN_ARC_COVERAGE else (0, 0, 255)
        border_color = (0, 255, 0) if border_ok else (0, 0, 255)

        cv2.putText(vis, f"arc={arc_coverage:.0%}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, arc_color, 2, cv2.LINE_AA)
        cv2.putText(vis, f"border={border_arc:.0%} edge={edge_ratio:.1%} dV={border_contrast:.0f}", (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, border_color, 2, cv2.LINE_AA)
    else:
        cv2.putText(vis, "NO ROI", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2, cv2.LINE_AA)

    if roi_info and roi_info.get('arc_coverage', 0) >= MIN_ARC_COVERAGE and border_ok:
        # Good: เต็มวงกลม + มีเส้นขอบ
        dest_path = os.path.join(good_folder, img_name)
        if not cv2.imwrite(dest_path, vis):
            print(f"⚠️ บันทึกไฟล์ไม่สำเร็จ: {dest_path}")

        green_fill = roi_info['green_fill']
        circularity = roi_info['circularity']
        arc_coverage = roi_info['arc_coverage']

        status_line = f"✅ GOOD: {img_name}"
        detail_line = f"    ROI: center=({cx},{cy}) radius={r}px green_fill={green_fill:.1%} circularity={circularity:.3f} arc_coverage={arc_coverage:.0%}"

        print(status_line)
        print(detail_line)

        summary_lines.append(status_line)
        summary_lines.append(detail_line)
        good_count += 1
        

    else:
        # Bad: ครึ่งวง หรือไม่เจอ หรือไม่มีขอบ
        dest_path = os.path.join(bad_folder, img_name)
        if not cv2.imwrite(dest_path, vis):
            print(f"⚠️ บันทึกไฟล์ไม่สำเร็จ: {dest_path}")

        if roi_info and not border_ok:
            status_line = f"❌ BAD: {img_name} (border weak/partial)"
        elif roi_info:
            arc_coverage = roi_info.get('arc_coverage', 0)
            status_line = f"❌ BAD: {img_name} (arc_coverage={arc_coverage:.0%} < {MIN_ARC_COVERAGE:.0%})"
        else:
            status_line = f"❌ BAD: {img_name} (ไม่เจอ ROI)"

        print(status_line)
        summary_lines.append(status_line)
        bad_count += 1

print("\n" + "=" * 80)
print("✅ เสร็จสิ้น!")
print(f"Good (full circle): {good_count} files → {good_folder}")
print(f"Bad (partial/none): {bad_count} files → {bad_folder}")
print(f"Total processed: {good_count + bad_count} files")

# เขียน summary report
summary_lines.append("")
summary_lines.append("FINAL SUMMARY:")
summary_lines.append(f"Good (full circle): {good_count} files")
summary_lines.append(f"Bad (partial/none): {bad_count} files")
summary_lines.append(f"Success rate: {good_count/(good_count+bad_count)*100:.1f}%")

with open(summary_file, 'w', encoding='utf-8') as f:
    f.write('\n'.join(summary_lines))

print(f"\n📄 Summary report saved: {summary_file}")