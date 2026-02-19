# ======================================================================
# Error Detection & ROI Drawing System for Wafer Testing Screenshots
# ======================================================================
#
# หลักการ ROI Detection:
#
#   เราตรวจจับ 3 ชนิด popup/dialog เฉพาะทาง โดยใช้ detector ที่ tune
#   สำหรับแต่ละชนิด  ไม่ใช้ "หาสี่เหลี่ยมทุกอัน" (ซึ่งจะไปจับปุ่ม/panel)
#
#   1. Yellow Popup  → ตรวจจาก "พื้นที่สีเหลืองขนาดใหญ่"
#      - Yield Monitoring / Needle alignment temp stop
#      - วิธี: color mask → morphology → contours
#
#   2. Dark Dialog   → ตรวจจาก "สี่เหลี่ยมขอบชัดที่ข้างในมืดมาก + มีตัวอักษร"
#      - Remote operation mode → Code / Message / Operation guide
#      - วิธี: edge contours → ตรวจ mean<80, body_dark>60%, bright text>1%
#
#   3. Win Dialog    → ตรวจจาก "สี่เหลี่ยมเล็ก body สีเทาอ่อนสม่ำเสมอ + title bar สี"
#      - Error [Alarm Code: xxxx] / Calibration failed
#      - วิธี: edge contours → body_mean>170, body_std<40, title contrast
#      - ตรวจซ้ำด้วย OCR: ต้องมีคำ error/alarm/fail จึงจะ keep
#
#   หลังจาก detect ทั้ง 3 แบบ → NMS ลบ overlap → OCR validate win_dialog
# ======================================================================

import cv2
import numpy as np
import os
import re
import glob
import json
from pathlib import Path
from datetime import datetime
import pandas as pd
import pytesseract

# === Tesseract Path ===
pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

# === Paths ===
DATA_PATH = r"C:\Users\chama\Desktop\Detect_Na\data_error"
OUTPUT_BASE = r"C:\Users\chama\Desktop\Detect_Na\error_analysis"

# === ROI Drawing Style ===
COLORS = {
    "high":    (0, 0, 255),      # Red
    "medium":  (0, 140, 255),    # Orange
    "low":     (0, 220, 255),    # Yellow
    "unknown": (180, 180, 180),  # Gray
}
ROI_THICK = 3
FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 0.65
FONT_THICK = 2

# === Post-OCR Validation Keywords ===
# win_dialog ROI ต้องมี keyword พวกนี้อย่างน้อย 1 ตัว จึงจะถือว่าเป็น error จริง
# ถ้าไม่มี → เป็น UI panel ธรรมดา → ลบทิ้ง
ERROR_KEYWORDS = [
    r"error", r"alarm", r"fail", r"warning", r"retry", r"cancel",
    r"cassette", r"calibr", r"vacuum", r"gpib", r"wafer.*load",
    r"needle", r"violation", r"site.*fail", r"locking",
    r"network", r"not.*found", r"not.*read", r"execution",
    r"unload", r"test.*head", r"prober",
]


# =====================================================================
# ERROR DATABASE  (from doc_error.md)
# =====================================================================

ERROR_DB = {
    # ── Yield / Site ──
    "YIELD_VIOLATION": {
        "cat": "Yield_Site",
        "desc": "Yield limit violation / Site FAILED",
        "sev": "high", "handler": "Eng",
        "kw": [
            "yield monitoring", "limit violation", "site.*failed",
            "sitewaf_pass", "waf_tested", "stop.?test", "occurrence.?count",
            "site.*fail",
        ],
    },
    # ── Probe / Alignment ──
    "E1181": {
        "cat": "Probe_Alignment",
        "desc": "Needle alignment error - position not found",
        "sev": "high", "handler": "Eng / Maintenance",
        "kw": ["needle.*position", "needle.*alignment.*error", "e.?1181"],
    },
    "NEEDLE_TEMP_STOP": {
        "cat": "Probe_Alignment",
        "desc": "Needle alignment temporary stop - check TEST HEAD",
        "sev": "medium", "handler": "Operator",
        "kw": [
            "needle.*alignment.*temporary", "manual.*unload.*test.*head",
            "check.*test.*head", "alignment.*temporary.*stop",
        ],
    },
    "TEST_HEAD_CONFIRM": {
        "cat": "Probe_Alignment",
        "desc": "Please confirm state of TEST HEAD",
        "sev": "medium", "handler": "Operator",
        "kw": [
            "confirm.*state.*test.*head", "confirm.*test.*head",
            "state.*test.*head", "please.*confirm",
        ],
    },
    "E5393": {
        "cat": "Probe_Alignment",
        "desc": "Card thickness / needle length mismatch",
        "sev": "high", "handler": "Eng",
        "kw": [
            "card.*thickness", "needle.*length.*differ",
            "device.*data.*card.*data", "e.?5393",
        ],
    },
    # ── Cassette / Loader ──
    "ALARM10197": {
        "cat": "Cassette_Loader",
        "desc": "Locking the Cassette on the Prober failed",
        "sev": "medium", "handler": "Eng",
        "kw": [
            "locking.*cassette", "cassette.*prober.*fail",
            "alarm.*10197", "10197",
        ],
    },
    "O352": {
        "cat": "Cassette_Loader",
        "desc": "Cassette not ready",
        "sev": "low", "handler": "Operator",
        "kw": ["cassette.*not.*ready"],
    },
    "O8508": {
        "cat": "Cassette_Loader",
        "desc": "Close and Lock Inspection Tray",
        "sev": "low", "handler": "Operator",
        "kw": ["close.*lock.*inspection", "inspection.*tray"],
    },
    "ALARM3117": {
        "cat": "Cassette_Loader",
        "desc": "No Wafer in Cassette Position",
        "sev": "medium", "handler": "Eng",
        "kw": ["no.*wafer.*cassette"],
    },
    # ── Vacuum / Wafer Handling ──
    "E225": {
        "cat": "Vacuum_Wafer",
        "desc": "Wafer Load Error - vacuum failed",
        "sev": "high", "handler": "Eng",
        "kw": ["wafer.*load.*error", "load.*error.*vacuum"],
    },
    # ── Communication / Network ──
    "O691": {
        "cat": "Communication",
        "desc": "GPIB Command Execution Error",
        "sev": "medium", "handler": "Eng",
        "kw": [
            "gpib.*command", "gpib.*execution", "gpib.*error",
            "command.*execution.*error",
        ],
    },
    "E1989": {
        "cat": "Communication",
        "desc": "Network process error - server/host unreachable",
        "sev": "high", "handler": "Eng",
        "kw": [
            "network.*process", "error.*occurs.*network",
            "check.*network", "e.?1989",
        ],
    },
    "E7036": {
        "cat": "Communication",
        "desc": "GEM host directions error",
        "sev": "medium", "handler": "Eng",
        "kw": ["gem.*host.*direction"],
    },
    # ── File / Software / Setup ──
    "ALARM301": {
        "cat": "File_Software",
        "desc": "File TEC_STD.env not found",
        "sev": "medium", "handler": "Eng",
        "kw": ["file.*not.*found", "tec_std"],
    },
    "ALARM10031": {
        "cat": "File_Software",
        "desc": "Calibration failed (module)",
        "sev": "medium", "handler": "Eng",
        "kw": ["calibration.*failed", "calibrat.*fail", "alarm.*10031"],
    },
    "ALARM10019": {
        "cat": "File_Software",
        "desc": "Calibration of tester failed",
        "sev": "medium", "handler": "Eng",
        "kw": [
            "calibration.*tester", "tester.*fail",
            "alarm.*10019", "10019",
        ],
    },
    "ALARM10230": {
        "cat": "File_Software",
        "desc": "Site mask missing",
        "sev": "medium", "handler": "Eng",
        "kw": ["site.*mask.*missing"],
    },
    "LOT_START_ERROR": {
        "cat": "File_Software",
        "desc": "Lot cannot be started - config mismatch",
        "sev": "high", "handler": "Eng",
        "kw": ["lot.*cannot.*start", "cannot.*be.*start"],
    },
    # ── Wafer ID ──
    "E460": {
        "cat": "Wafer_ID",
        "desc": "Wafer ID not read - No ID characters",
        "sev": "medium", "handler": "Operator",
        "kw": ["wafer.*id.*not.*read", "no.*id.*character"],
    },
    "ID_INVALID": {
        "cat": "Wafer_ID",
        "desc": "Wafer ID is invalid",
        "sev": "medium", "handler": "Operator",
        "kw": ["value.*id.*invalid", "id.*invalid", "associated.*id"],
    },
}

# Code extraction regex (E/O/Alarm numbers)
CODE_RE = [
    (r"[Ee]\s*(\d{3,5})", "E"),
    (r"[Oo]\s*(\d{3,5})", "O"),
    (r"[Aa]larm\s*[Cc]?o?d?e?\s*:?\s*(\d{3,6})", "ALARM"),
]


# =====================================================================
# UTILITY
# =====================================================================

def make_output(base):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = f"{base}_{ts}"
    Path(root).mkdir(parents=True, exist_ok=True)
    cats = sorted({v["cat"] for v in ERROR_DB.values()}) + ["Unknown"]
    for c in cats:
        Path(root, c).mkdir(exist_ok=True)
        Path(root, c, "roi_crops").mkdir(exist_ok=True)
    return root


def np2py(obj):
    if isinstance(obj, dict):
        return {k: np2py(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [np2py(i) for i in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


# =====================================================================
# 1. ROI DETECTION
# =====================================================================
#
# หลักการ:
#   แต่ละ detector ถูกออกแบบเฉพาะสำหรับ popup ชนิดใดชนิดหนึ่งเท่านั้น
#   ไม่ใช้ "หาสี่เหลี่ยมทุกอัน" เพราะ UI ของ prober มีปุ่ม/panel มากมาย
#
#   A) Yellow popup  → color mask (เหลือง HSV)
#   B) Dark dialog   → edge contour + ตรวจ interior มืด + มีตัวอักษร
#   C) Win dialog    → edge contour แบบ STRICT + ตรวจ body สีเทาสม่ำเสมอ
#                       + validate ด้วย OCR ภายหลัง
#
# =====================================================================

def detect_rois(img):
    """
    Return list of ROI dicts [{x, y, w, h, type, conf}, ...]
    for error popup regions only.
    """
    H, W = img.shape[:2]
    tot = H * W
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    rois = []

    # A ── Yellow popup (Yield Monitoring, Needle alignment)
    rois += _detect_yellow_popup(hsv, H, W, tot)

    # B ── Dark dialog (Code/Message/Operation guide)
    rois += _detect_dark_dialog(gray, img, H, W, tot)

    # C ── Windows error dialog (Error [Alarm Code:])
    #      จะถูก validate ด้วย OCR อีกครั้งใน process_image()
    rois += _detect_win_dialog(gray, img, H, W, tot, rois)

    return _nms(rois, 0.35)


# ── A: Yellow Popup ──────────────────────────────────────────
# ตรวจจากพื้นที่สีเหลืองสด (Yield violation, Needle alignment temp stop)
# หลักการ: สร้าง mask สีเหลือง → morphological close เพื่อเชื่อมช่องว่าง
#          → หา contour ขนาดใหญ่พอ

def _detect_yellow_popup(hsv, H, W, tot):
    mask = cv2.inRange(hsv, (18, 80, 150), (38, 255, 255))
    if np.sum(mask > 0) / tot < 0.008:
        return []

    k = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    mask = cv2.dilate(mask, k, iterations=1)

    rois = []
    for c in cv2.findContours(mask, cv2.RETR_EXTERNAL,
                               cv2.CHAIN_APPROX_SIMPLE)[0]:
        x, y, w, h = cv2.boundingRect(c)
        if (w * h) / tot > 0.005 and 0.3 < w / max(h, 1) < 6:
            pad = 15
            x, y = max(x - pad, 0), max(y - pad, 0)
            w, h = min(w + 2 * pad, W - x), min(h + 2 * pad, H - y)
            rois.append(dict(x=x, y=y, w=w, h=h,
                             type="yellow_popup", conf=0.90))
    return rois


# ── B: Dark Dialog ───────────────────────────────────────────
# ตรวจจาก edge contours ที่สร้างสี่เหลี่ยม + ภายในมืด + มีตัวอักษร
#
# หลักการ:
#   1) Canny edge → morphological close → หา contours
#   2) กรอง: 4-10 vertices, area 2-55%, aspect 0.4-4.5
#   3) ตรวจภายใน: mean brightness < 80 (มืดจริง)
#   4) body (ล่าง 85%) ต้องมี >55% pixels ที่มืดมาก (<50)
#   5) ต้องมี bright pixels >1% (ตัวอักษรสีขาว/แดง/เขียว)
#   6) ไม่ชิดขอบจอ 3 ด้าน (ไม่ใช่ background)

def _detect_dark_dialog(gray, img, H, W, tot):
    edges = cv2.Canny(gray, 25, 90)
    k = np.ones((7, 7), np.uint8)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k)
    cnts, _ = cv2.findContours(edges, cv2.RETR_TREE,
                                cv2.CHAIN_APPROX_SIMPLE)
    rois = []
    for c in cnts:
        peri = cv2.arcLength(c, True)
        apx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if not (4 <= len(apx) <= 10):
            continue

        x, y, w, h = cv2.boundingRect(c)
        ar = (w * h) / tot
        if ar < 0.02 or ar > 0.55:
            continue
        asp = w / max(h, 1)
        if asp < 0.4 or asp > 4.5:
            continue

        roi_g = gray[y:y+h, x:x+w]
        mb = float(np.mean(roi_g))

        # ภายในต้องมืดจริง (mean < 80)
        if mb > 80:
            continue

        # body (ล่าง 85%) ต้องมืด >55%
        body_y = y + int(h * 0.15)
        if body_y < y + h:
            body = gray[body_y:y+h, x:x+w]
            body_dark = np.sum(body < 50) / max(body.size, 1)
        else:
            body_dark = 0
        if body_dark < 0.55:
            continue

        # ต้องมี bright pixels (ตัวอักษร)
        bright_r = np.sum(roi_g > 100) / roi_g.size
        if bright_r < 0.01:
            continue

        # ไม่ชิดขอบจอ ≥3 ด้าน
        mg = 10
        n_edges = sum([x < mg, y < mg, x+w > W-mg, y+h > H-mg])
        if n_edges >= 3:
            continue

        rois.append(dict(x=x, y=y, w=w, h=h, type="dark_dialog",
                         conf=round(min(body_dark + bright_r * 3, 1.0), 3)))
    return rois


# ── C: Windows Error Dialog ──────────────────────────────────
# ตรวจจาก edge contours แบบ STRICT มาก
#
# หลักการ (ทำไมเข้มงวด):
#   UI ของ prober software มีปุ่ม, panel, section header มากมาย
#   ถ้าใช้ filter หลวม จะจับ UI elements ธรรมดาเป็น ROI
#
#   Filters ที่ใช้ (ทุกข้อต้องผ่าน):
#   1) vertices 4-6 (สี่เหลี่ยมชัดเจน)
#   2) area 0.3-6% (dialog เล็ก ไม่ใช่ panel ใหญ่)
#   3) aspect 1.2-3.5 (กว้างกว่าสูง ลักษณะ dialog)
#   4) ไม่ชิดขอบจอเลย (dialog ลอยอยู่)
#   5) body brightness 170-240, std < 40 (สีเทาอ่อน สม่ำเสมอมาก)
#   6) title bar ต้อง contrast กับ body (สีต่างหรือความสว่างต่าง)
#   7) edge density < 0.08 (dialog ง่ายๆ ไม่มี sub-element เยอะ)
#   8) ไม่อยู่ภายใน ROI ที่ตรวจพบแล้ว
#
#   หลัง detect → validate ด้วย OCR (ต้องมีคำ error/alarm/fail)

def _detect_win_dialog(gray, img, H, W, tot, existing):
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(blur, 30, 100)
    k = np.ones((5, 5), np.uint8)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k)
    cnts, _ = cv2.findContours(edges, cv2.RETR_TREE,
                                cv2.CHAIN_APPROX_SIMPLE)
    rois = []
    for c in cnts:
        peri = cv2.arcLength(c, True)
        apx = cv2.approxPolyDP(c, 0.02 * peri, True)
        # (1) ต้องเป็นสี่เหลี่ยม 4-6 มุม
        if not (4 <= len(apx) <= 6):
            continue

        x, y, w, h = cv2.boundingRect(c)
        area_r = (w * h) / tot

        # (2) ขนาดเล็ก-กลาง 0.3%-6%
        if area_r < 0.003 or area_r > 0.06:
            continue

        # (3) กว้างกว่าสูง (dialog shape)
        asp = w / max(h, 1)
        if asp < 1.2 or asp > 3.5:
            continue

        # (4) ไม่ชิดขอบจอเลย (floating dialog)
        mg = 15
        if x < mg or y < mg or x + w > W - mg or y + h > H - mg:
            continue

        # (5) body: สีเทาอ่อน สม่ำเสมอ
        title_h = max(int(h * 0.22), 15)
        body = gray[y + title_h:y + h, x:x + w]
        title = gray[y:y + title_h, x:x + w]
        if body.size < 100 or title.size == 0:
            continue

        body_mean = float(np.mean(body))
        body_std = float(np.std(body))
        if body_mean < 170 or body_mean > 240 or body_std > 40:
            continue

        # (6) title bar ต้อง contrast กับ body
        title_mean = float(np.mean(title))
        diff = abs(title_mean - body_mean)
        title_hsv = cv2.cvtColor(
            img[y:y + title_h, x:x + w], cv2.COLOR_BGR2HSV)
        title_sat = float(np.mean(title_hsv[:, :, 1]))
        if title_sat < 40 and diff < 40:
            continue

        # (7) edge density ต่ำ (simple dialog)
        roi_edges = cv2.Canny(gray[y:y+h, x:x+w], 30, 100)
        edge_density = np.sum(roi_edges > 0) / max(roi_edges.size, 1)
        if edge_density > 0.08:
            continue

        # (8) ไม่อยู่ภายใน ROI เดิม (เช่น ปุ่มข้างใน dark dialog)
        contained = False
        cand = dict(x=x, y=y, w=w, h=h)
        for e in existing + rois:
            ox1 = max(x, e["x"])
            oy1 = max(y, e["y"])
            ox2 = min(x + w, e["x"] + e["w"])
            oy2 = min(y + h, e["y"] + e["h"])
            if ox2 > ox1 and oy2 > oy1:
                inter = (ox2 - ox1) * (oy2 - oy1)
                if inter / max(w * h, 1) > 0.5:
                    contained = True
                    break
        if contained:
            continue

        rois.append(dict(x=x, y=y, w=w, h=h, type="win_dialog",
                         conf=round(0.50 + min(diff / 120 +
                                               title_sat / 200, 0.45), 3)))
    return rois


# ── NMS & helpers ────────────────────────────────────────────

def _iou(a, b):
    x1 = max(a["x"], b["x"])
    y1 = max(a["y"], b["y"])
    x2 = min(a["x"] + a["w"], b["x"] + b["w"])
    y2 = min(a["y"] + a["h"], b["y"] + b["h"])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / max(union, 1)


def _nms(rois, thresh):
    if len(rois) <= 1:
        return rois
    rois.sort(key=lambda r: r["conf"], reverse=True)
    keep = []
    for r in rois:
        if all(_iou(r, k) < thresh for k in keep):
            keep.append(r)
    return keep


# =====================================================================
# 2. POST-OCR ROI VALIDATION
# =====================================================================
# win_dialog ROIs ที่ OCR ออกมาแล้วไม่มี error keyword → ลบทิ้ง
# ป้องกัน UI panel ธรรมดาที่หลุดจาก visual filter

def validate_win_dialog_rois(rois, roi_texts):
    """
    ตรวจสอบ win_dialog ROIs ว่า OCR text มี error keyword หรือไม่
    ถ้าไม่มี → ลบ (เป็น UI panel ไม่ใช่ error dialog)
    yellow_popup / dark_dialog → keep เสมอ (เชื่อถือได้จาก visual)
    """
    validated = []
    for roi, text in zip(rois, roi_texts):
        if roi["type"] == "win_dialog":
            tl = text.lower()
            has_error = any(re.search(kw, tl) for kw in ERROR_KEYWORDS)
            if not has_error:
                continue  # ไม่มี error keyword → false positive → ลบ
        validated.append(roi)
    return validated


# =====================================================================
# 3. ROI DRAWING
# =====================================================================

def draw_rois(img, rois, classification, error_id, severity):
    """
    วาด ROI bounding box + corner accents + label + top banner
    """
    ann = img.copy()
    color = COLORS.get(severity, COLORS["unknown"])

    for i, roi in enumerate(rois):
        x, y, w, h = roi["x"], roi["y"], roi["w"], roi["h"]

        # ── thick bounding box ──
        cv2.rectangle(ann, (x, y), (x + w, y + h), color, ROI_THICK)

        # ── corner accents ──
        cl = min(30, w // 4, h // 4)
        ct = ROI_THICK + 2
        for (cx, cy, dx, dy) in [
            (x, y, 1, 1), (x + w, y, -1, 1),
            (x, y + h, 1, -1), (x + w, y + h, -1, -1),
        ]:
            cv2.line(ann, (cx, cy), (cx + cl * dx, cy), color, ct)
            cv2.line(ann, (cx, cy), (cx, cy + cl * dy), color, ct)

        # ── ROI label ──
        label = f"ROI-{i} [{roi['type']}]"
        (tw, th_t), _ = cv2.getTextSize(label, FONT, FONT_SCALE * 0.8, 1)
        lx, ly = x, max(y - 8, th_t + 4)
        cv2.rectangle(ann, (lx - 2, ly - th_t - 4),
                       (lx + tw + 4, ly + 4), color, -1)
        cv2.putText(ann, label, (lx, ly), FONT, FONT_SCALE * 0.8,
                    (255, 255, 255), 1, cv2.LINE_AA)

    # ── Top banner ──
    if classification:
        db = classification
        banner = (f"{error_id} | {db['cat']} | {db['sev'].upper()} "
                  f"| {db['desc']}")
    else:
        banner = f"UNCLASSIFIED | {error_id}"

    (bw, bh), _ = cv2.getTextSize(banner, FONT, FONT_SCALE, FONT_THICK)
    cv2.rectangle(ann, (0, 0), (bw + 20, bh + 20), color, -1)
    cv2.putText(ann, banner, (10, bh + 10), FONT, FONT_SCALE,
                (255, 255, 255), FONT_THICK, cv2.LINE_AA)

    return ann


# =====================================================================
# 4. OCR
# =====================================================================

def ocr_roi(img, roi, pad=12):
    """OCR a single ROI crop, multi-attempt by popup type."""
    H, W = img.shape[:2]
    x = max(roi["x"] - pad, 0)
    y = max(roi["y"] - pad, 0)
    w = min(roi["w"] + 2 * pad, W - x)
    h = min(roi["h"] + 2 * pad, H - y)
    crop = img[y:y+h, x:x+w]
    if crop.size == 0:
        return ""

    rtype = roi.get("type", "")
    texts = []

    if rtype == "dark_dialog":
        # Attempt 1: invert + high contrast
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        g = cv2.bitwise_not(g)
        g = cv2.convertScaleAbs(g, alpha=1.8, beta=10)
        _, b = cv2.threshold(g, 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        texts.append(_tess(b))

        # Attempt 2: red-channel text
        hc = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        r1 = cv2.inRange(hc, (0, 50, 80), (10, 255, 255))
        r2 = cv2.inRange(hc, (170, 50, 80), (180, 255, 255))
        rm = cv2.bitwise_or(r1, r2)
        rm = cv2.dilate(rm, np.ones((2, 2), np.uint8))
        texts.append(_tess(rm))

        # Attempt 3: green-channel text
        gm = cv2.inRange(hc, (35, 50, 80), (85, 255, 255))
        gm = cv2.dilate(gm, np.ones((2, 2), np.uint8))
        texts.append(_tess(gm))

    elif rtype == "yellow_popup":
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        g = cv2.convertScaleAbs(g, alpha=1.5, beta=0)
        _, b = cv2.threshold(g, 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        texts.append(_tess(b))

        # red text extraction
        hc = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        r1 = cv2.inRange(hc, (0, 80, 80), (10, 255, 255))
        r2 = cv2.inRange(hc, (170, 80, 80), (180, 255, 255))
        rm = cv2.bitwise_or(r1, r2)
        rm = cv2.dilate(rm, np.ones((3, 3), np.uint8))
        texts.append(_tess(rm))

    else:  # win_dialog, etc.
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        _, b = cv2.threshold(g, 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        texts.append(_tess(b))
        ad = cv2.adaptiveThreshold(g, 255,
                                    cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 15, 8)
        texts.append(_tess(ad))

    return "\n".join(t for t in texts if t).strip()


def ocr_full(img):
    """Full-image OCR (normal + inverted for dark screens)."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    texts = []
    # normal
    gn = cv2.convertScaleAbs(g, alpha=1.3, beta=10)
    _, bn = cv2.threshold(gn, 0, 255,
                           cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    texts.append(_tess(bn))
    # inverted
    gi = cv2.bitwise_not(g)
    gi = cv2.convertScaleAbs(gi, alpha=1.5, beta=10)
    _, bi = cv2.threshold(gi, 0, 255,
                           cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    texts.append(_tess(bi))
    return "\n".join(t for t in texts if t).strip()


def _tess(bw):
    """Run Tesseract; upscale tiny images."""
    if bw.size == 0:
        return ""
    if bw.shape[0] < 80 or bw.shape[1] < 160:
        sc = max(2, 160 // max(bw.shape[1], 1))
        bw = cv2.resize(bw, None, fx=sc, fy=sc,
                         interpolation=cv2.INTER_CUBIC)
    try:
        return pytesseract.image_to_string(
            bw, config="--oem 3 --psm 6"
        ).strip()
    except Exception:
        return ""


# =====================================================================
# 5. CODE EXTRACTION & CLASSIFICATION
# =====================================================================

def extract_codes(text):
    """Extract E/O/Alarm codes → [{raw, norm, type}]"""
    out, seen = [], set()
    for pat, ctype in CODE_RE:
        for m in re.finditer(pat, text, re.I):
            n = f"{ctype}{m.group(1)}"
            if n not in seen:
                seen.add(n)
                out.append(dict(raw=m.group(0).strip(), norm=n, type=ctype))
    return out


def classify(codes, text):
    """
    Match by exact code first, then keyword fallback.
    Returns db entry dict or None.
    """
    lo = text.lower()

    # ── exact code match ──
    for c in codes:
        n = c["norm"]
        if n in ERROR_DB:
            return dict(ERROR_DB[n], error_id=n, method="code_exact")

    # ── keyword match (best score) ──
    best, bscore = None, 0
    for eid, db in ERROR_DB.items():
        sc = sum(1 for kw in db["kw"] if re.search(kw, lo))
        if sc > bscore:
            bscore, best = sc, (eid, db)
    if best and bscore >= 1:
        eid, db = best
        return dict(db, error_id=eid, method=f"keyword(score={bscore})")

    return None


# =====================================================================
# 6. VISUAL ANALYSIS
# =====================================================================

def visual_cues(img):
    H, W = img.shape[:2]
    tot = H * W
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    rm1 = cv2.inRange(hsv, (0, 70, 70), (10, 255, 255))
    rm2 = cv2.inRange(hsv, (170, 70, 70), (180, 255, 255))
    red_pct = np.sum(cv2.bitwise_or(rm1, rm2) > 0) / tot * 100
    ym = cv2.inRange(hsv, (18, 80, 150), (38, 255, 255))
    yel_pct = np.sum(ym > 0) / tot * 100
    dark_pct = np.sum(g < 30) / tot * 100
    return dict(
        red_pct=round(red_pct, 2), yellow_pct=round(yel_pct, 2),
        dark_pct=round(dark_pct, 2), brightness=round(float(np.mean(g)), 1),
    )


# =====================================================================
# 7. PROCESS SINGLE IMAGE
# =====================================================================

def process_image(path, out_root):
    fname = os.path.basename(path)
    img = cv2.imread(path)
    if img is None:
        return None
    H, W = img.shape[:2]
    vis = visual_cues(img)

    # ── detect ROIs ──
    rois = detect_rois(img)

    # ── OCR per ROI ──
    roi_texts = []
    all_codes = []
    for roi in rois:
        txt = ocr_roi(img, roi)
        codes = extract_codes(txt)
        roi_texts.append(txt)
        all_codes += codes

    # ── POST-OCR VALIDATION ──
    # win_dialog ROIs ที่ไม่มี error keyword → ลบ (false positive)
    rois_valid = validate_win_dialog_rois(rois, roi_texts)
    texts_valid = [t for roi, t in zip(rois, roi_texts)
                   if roi in rois_valid]
    rois = rois_valid
    roi_texts = texts_valid

    # ── full-image OCR supplement ──
    ftxt = ocr_full(img)
    fcodes = extract_codes(ftxt)
    seen = {c["norm"] for c in all_codes}
    for fc in fcodes:
        if fc["norm"] not in seen:
            all_codes.append(fc)
            seen.add(fc["norm"])

    all_text = "\n".join(roi_texts) + "\n" + ftxt
    all_text = all_text.strip()

    # ── build roi_info ──
    roi_info = []
    for i, (roi, txt) in enumerate(zip(rois, roi_texts)):
        codes = extract_codes(txt)
        roi_info.append(dict(
            idx=i,
            bbox=dict(x=roi["x"], y=roi["y"], w=roi["w"], h=roi["h"]),
            type=roi["type"], conf=roi["conf"],
            text=txt, codes=codes,
        ))

    if not rois:
        roi_info.append(dict(
            idx=0, bbox=dict(x=0, y=0, w=W, h=H),
            type="full_fallback", conf=0.3, text=ftxt, codes=fcodes,
        ))

    # ── classify ──
    clf = classify(all_codes, all_text)
    if clf:
        cat = clf["cat"]
        eid = clf.get("error_id", "?")
        sev = clf["sev"]
    else:
        cat, eid, sev = "Unknown", "UNKNOWN", "unknown"

    # ── save outputs ──
    cat_dir = os.path.join(out_root, cat)
    roi_dir = os.path.join(cat_dir, "roi_crops")
    os.makedirs(cat_dir, exist_ok=True)
    os.makedirs(roi_dir, exist_ok=True)

    # original copy
    cv2.imwrite(os.path.join(cat_dir, fname), img)

    # annotated (with ROI boxes)
    ann = draw_rois(img, rois, clf, eid, sev)
    ann_path = os.path.join(cat_dir, f"annotated_{fname}")
    cv2.imwrite(ann_path, ann)

    # ROI crops
    crop_paths = []
    for i, roi in enumerate(rois):
        rx, ry, rw, rh = roi["x"], roi["y"], roi["w"], roi["h"]
        c = img[ry:ry+rh, rx:rx+rw]
        cp = os.path.join(roi_dir, f"{Path(fname).stem}_roi{i}.png")
        cv2.imwrite(cp, c)
        crop_paths.append(cp)

    return dict(
        filename=fname, path=path, size=dict(w=W, h=H),
        roi_count=len(rois), rois=np2py(roi_info),
        codes=np2py(all_codes), ocr_text=all_text,
        classification=np2py(clf), category=cat,
        error_id=eid, severity=sev, visual=np2py(vis),
        files=dict(annotated=ann_path, crops=crop_paths),
        ts=datetime.now().isoformat(),
    )


# =====================================================================
# 8. REPORTS
# =====================================================================

def gen_reports(results, out):
    valid = [r for r in results if r]
    total = len(results)
    n_valid = len(valid)
    n_clf = len([r for r in valid if r["category"] != "Unknown"])
    cats, sevs, codes_c = {}, {}, {}
    for r in valid:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
        sevs[r["severity"]] = sevs.get(r["severity"], 0) + 1
        for c in r["codes"]:
            codes_c[c["norm"]] = codes_c.get(c["norm"], 0) + 1

    # JSON
    rpt = dict(
        ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        summary=dict(
            total=total, valid=n_valid, classified=n_clf,
            unclassified=n_valid - n_clf,
            rate=f"{n_clf / max(n_valid, 1) * 100:.1f}%",
            categories=cats, severities=sevs, codes=codes_c,
        ),
        detailed_results=valid,
    )
    jp = os.path.join(out, "error_report.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(rpt, f, indent=2, ensure_ascii=False, default=str)

    # CSV
    rows = []
    for r in valid:
        c = r["classification"]
        rows.append(dict(
            filename=r["filename"], category=r["category"],
            error_id=r["error_id"], severity=r["severity"],
            roi_count=r["roi_count"],
            codes=", ".join(x["norm"] for x in r["codes"]),
            method=c["method"] if c else "",
            description=c["desc"] if c else "",
            handler=c["handler"] if c else "",
            red_pct=r["visual"]["red_pct"],
            yellow_pct=r["visual"]["yellow_pct"],
            dark_pct=r["visual"]["dark_pct"],
            ocr_preview=r["ocr_text"][:120].replace("\n", " "),
        ))
    cp = os.path.join(out, "error_report.csv")
    pd.DataFrame(rows).to_csv(cp, index=False, encoding="utf-8-sig")

    # TXT summary
    sep = "=" * 64
    lines = [
        sep, "  ERROR DETECTION & ROI CLASSIFICATION REPORT", sep,
        f"  Generated: {rpt['ts']}", f"  Source:    {DATA_PATH}", sep, "",
        "  OVERVIEW",
        f"  Total images    : {total}",
        f"  Processed       : {n_valid}",
        f"  Classified      : {n_clf}",
        f"  Unclassified    : {n_valid - n_clf}",
        f"  Detection rate  : {rpt['summary']['rate']}", "",
        "  CATEGORY BREAKDOWN",
    ]
    for c, n in sorted(cats.items()):
        lines.append(f"    {c:<25}: {n}")
    lines += ["", "  SEVERITY BREAKDOWN"]
    for s, n in sorted(sevs.items()):
        lines.append(f"    {s:<25}: {n}")
    if codes_c:
        lines += ["", "  CODES FOUND"]
        for cd, n in sorted(codes_c.items()):
            lines.append(f"    {cd:<25}: {n}")
    lines += ["", "  DETAILS", "  " + "-" * 58]
    for r in valid:
        c = r["classification"]
        lines.append(f"  [{r['severity'].upper():>7}] {r['filename']}")
        lines.append(
            f"            ID:  {r['error_id']}  |  Cat: {r['category']}")
        if c:
            lines.append(f"            Desc: {c['desc']}")
            lines.append(
                f"            Handler: {c['handler']} | Match: {c['method']}")
        codes_str = ", ".join(x["norm"] for x in r["codes"])
        if codes_str:
            lines.append(f"            Codes: {codes_str}")
        lines.append(f"            ROIs: {r['roi_count']}")
        lines.append("")
    lines.append(sep)
    tp = os.path.join(out, "analysis_summary.txt")
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return jp, cp, tp


# =====================================================================
# MAIN
# =====================================================================

def main():
    print("=" * 60)
    print("  WAFER TESTING - ERROR DETECTION & ROI DRAWING")
    print("=" * 60)
    print(f"  Source : {DATA_PATH}")
    print("=" * 60)

    out = make_output(OUTPUT_BASE)
    print(f"  Output : {out}\n")

    exts = ["*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG", "*.bmp"]
    files = sorted({f for e in exts
                    for f in glob.glob(os.path.join(DATA_PATH, e))})
    if not files:
        print("  No images found.")
        return
    print(f"  {len(files)} images\n")

    results = []
    for i, fp in enumerate(files, 1):
        nm = os.path.basename(fp)
        print(f"  [{i:>3}/{len(files)}] {nm}", end="")
        r = process_image(fp, out)
        results.append(r)
        if r:
            print(f"  ->  {r['severity'].upper():>7} | {r['category']}"
                  f" | {r['error_id']} | ROIs={r['roi_count']}")
        else:
            print("  ->  SKIP")

    print("\n  Generating reports ...")
    jp, cpth, tp = gen_reports(results, out)
    print(f"  JSON: {jp}")
    print(f"  CSV : {cpth}")
    print(f"  TXT : {tp}")

    valid = [r for r in results if r]
    clf = [r for r in valid if r["category"] != "Unknown"]
    print(f"\n  SUMMARY: {len(clf)}/{len(valid)} classified")
    if clf:
        cats = {}
        for r in clf:
            cats[r["category"]] = cats.get(r["category"], 0) + 1
        for c, n in sorted(cats.items()):
            print(f"    {c:<25}: {n}")
    print("\n" + "=" * 60 + "\n  Done.\n" + "=" * 60)


if __name__ == "__main__":
    main()

