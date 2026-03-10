# ======================================================================
# Simple Error Detection (No BBox) - OCR Full Image + Match ERROR_DB
# ======================================================================

import cv2
import numpy as np
import os
import sys
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
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(_SCRIPT_DIR, "data_error")
OUTPUT_BASE = os.path.join(_SCRIPT_DIR, "error_analysis_simple")

# === Severity Colors (for banner only) ===
COLORS = {
    "high":    (0, 0, 255),      # Red
    "medium":  (0, 140, 255),    # Orange
    "low":     (0, 220, 255),    # Yellow
    "unknown": (180, 180, 180),  # Gray
}
FONT = cv2.FONT_HERSHEY_SIMPLEX


# =====================================================================
# ERROR DATABASE  (loaded from PostgreSQL)
# =====================================================================


def load_error_db():
    """Load ERROR_DB from PostgreSQL.
    Returns dict: { error_id: { cat, desc, sev, handler, kw: [patterns] } }
    """
    backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard", "backend")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    import db_helper
    codes = db_helper.get_error_codes_with_keywords()
    db = {}
    for c in codes:
        eid = c["error_id"]
        kw_raw = c.get("keywords", "")
        kw_list = [k.strip() for k in kw_raw.split("|") if k.strip()]
        db[eid] = {
            "cat": c.get("category", ""),
            "desc": c.get("description", ""),
            "sev": c.get("severity", "medium"),
            "handler": c.get("handler", ""),
            "kw": kw_list,
        }
    print(f"[3_simple] Loaded {len(db)} error codes from PostgreSQL")
    return db


ERROR_DB = load_error_db()

# Code extraction regex - ต้องมี context รอบๆ เพื่อลด false positive
CODE_RE = [
    # E codes: E1181, E225, etc. - ต้องมี space/punctuation ก่อนหน้า
    (r"(?:^|[\s\[:\(])([Ee]\s*\d{3,5})(?:[\s\]\):\.]|$)", "E"),
    # O codes: O691, O352 - ต้องอยู่ใน context "Code:" หรือ "O " + number
    (r"[Cc]ode[:\s]*([Oo]\s*\d{3,5})", "O"),
    # Alarm codes: Alarm 3117, Alarm Code: 10197
    (r"[Aa]larm\s*[Cc]?o?d?e?\s*[:\s]*(\d{3,6})", "ALARM"),
]


# =====================================================================
# UTILITY
# =====================================================================

def make_output(base):
    Path(base).mkdir(parents=True, exist_ok=True)
    return base


def make_machine_dirs(root, machine_name):
    """สร้าง subfolder สำหรับแต่ละเครื่อง (ไม่แยก category)"""
    machine_dir = os.path.join(root, machine_name)
    Path(machine_dir).mkdir(parents=True, exist_ok=True)
    return machine_dir


# =====================================================================
# FILTER CONSOLE AREA
# =====================================================================

def mask_console_area(img):
    """สร้าง mask กรอง console log area (ด้านซ้าย ~20% ของภาพ) แต่ไม่กรอง dialog boxes"""
    H, W = img.shape[:2]
    mask = np.ones((H, W), dtype=np.uint8) * 255
    
    # ตรวจจับ console area โดยดูจาก:
    # 1. พื้นที่ด้านซ้ายที่มีพื้นหลังเทาเข้ม/ขาว และมีข้อความสีเขียว หรือ
    # 2. Fixed left 20% area (ลดลงจาก 25%)
    
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
    
    # ตรวจสอบ brightness ด้านซ้าย 30%
    left_w = int(W * 0.30)
    left_area = g[:, :left_w]
    mean_left = np.mean(left_area)
    std_left = np.std(left_area)
    
    # ถ้า left area มี contrast ต่ำ (เป็นพื้นหลังสม่ำเสมอ) = likely console
    # หรือ มี brightness สูง/ต่ำมาก = white/black console background
    is_console_like = (std_left < 60) or (mean_left < 80) or (mean_left > 200)
    
    if is_console_like:
        # Mask ด้านซ้าย 20% ออก (ลดลงเพื่อไม่กรอง dialog ที่อาจอยู่ใกล้ขอบ)
        mask_w = int(W * 0.20)
        mask[:, :mask_w] = 0
    
    return mask


# =====================================================================
# OCR (Full Image with Console Filtering)
# =====================================================================

def ocr_full(img):
    """OCR ทั้งภาพ - กรอง console area ออก + ลองหลายวิธี"""
    # สร้าง mask กรอง console
    console_mask = mask_console_area(img)
    
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    # Apply mask to gray image
    g_filtered = cv2.bitwise_and(g, g, mask=console_mask)
    
    texts = []
    
    # 1) Normal threshold
    gn = cv2.convertScaleAbs(g_filtered, alpha=1.3, beta=10)
    _, bn = cv2.threshold(gn, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bn_masked = cv2.bitwise_and(bn, bn, mask=console_mask)
    texts.append(_tess(bn_masked))
    
    # 2) Inverted (for dark backgrounds)
    gi = cv2.bitwise_not(g_filtered)
    gi = cv2.convertScaleAbs(gi, alpha=1.5, beta=10)
    _, bi = cv2.threshold(gi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bi_masked = cv2.bitwise_and(bi, bi, mask=console_mask)
    texts.append(_tess(bi_masked))
    
    # 3) Adaptive threshold with different parameters
    ad1 = cv2.adaptiveThreshold(g_filtered, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 15, 8)
    ad1_masked = cv2.bitwise_and(ad1, ad1, mask=console_mask)
    texts.append(_tess(ad1_masked))
    
    # 4) Red channel extraction (for red error text)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    r1 = cv2.inRange(hsv, (0, 50, 80), (10, 255, 255))
    r2 = cv2.inRange(hsv, (170, 50, 80), (180, 255, 255))
    rm = cv2.bitwise_or(r1, r2)
    rm = cv2.dilate(rm, np.ones((2, 2), np.uint8))
    rm_masked = cv2.bitwise_and(rm, rm, mask=console_mask)
    texts.append(_tess(rm_masked))
    
    # 5) Yellow popup text (for yellow dialogs)
    y1 = cv2.inRange(hsv, (20, 100, 100), (35, 255, 255))
    y1 = cv2.dilate(y1, np.ones((2, 2), np.uint8))
    y1_masked = cv2.bitwise_and(y1, y1, mask=console_mask)
    texts.append(_tess(y1_masked))
    
    # 6) Green text extraction (for GPIB error dialogs and green text on black)
    # Broader green range to catch various shades
    g1 = cv2.inRange(hsv, (35, 50, 80), (85, 255, 255))  # Standard green
    g2 = cv2.inRange(hsv, (40, 30, 100), (80, 255, 255))  # Brighter green
    gm = cv2.bitwise_or(g1, g2)
    gm = cv2.morphologyEx(gm, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    gm_masked = cv2.bitwise_and(gm, gm, mask=console_mask)
    texts.append(_tess(gm_masked))
    
    # 7) Specific for green-on-black dialogs (high contrast green)
    # Look for bright pixels in green channel
    b, g_ch, r = cv2.split(img)
    bright_green = ((g_ch > 100) & (r < 100) & (b < 100)).astype(np.uint8) * 255
    bright_green = cv2.morphologyEx(bright_green, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    bg_masked = cv2.bitwise_and(bright_green, bright_green, mask=console_mask)
    texts.append(_tess(bg_masked))
    
    # 8) White text on blue background (common Windows dialogs)
    # Extract blue areas and look for white text
    blue_mask = cv2.inRange(hsv, (90, 50, 80), (130, 255, 255))  # Blue background
    # Threshold for bright (white) text
    _, white_text = cv2.threshold(g_filtered, 180, 255, cv2.THRESH_BINARY)
    white_text_masked = cv2.bitwise_and(white_text, white_text, mask=console_mask)
    texts.append(_tess(white_text_masked))
    
    # 9) High contrast text extraction (for any bright text on dark bg)
    # Use adaptive threshold with larger block size for dialog text
    ad2 = cv2.adaptiveThreshold(g_filtered, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                cv2.THRESH_BINARY, 21, 10)
    ad2_masked = cv2.bitwise_and(ad2, ad2, mask=console_mask)
    texts.append(_tess(ad2_masked))
    
    # 10) Teal/Cyan text extraction (for dialog title bars like "Error [Alarm Code: 10019]")
    # Teal/cyan color in HSV: H=80-100, S>50, V>100
    cyan1 = cv2.inRange(hsv, (75, 30, 100), (105, 255, 255))
    cyan1 = cv2.dilate(cyan1, np.ones((2, 2), np.uint8))
    cyan_masked = cv2.bitwise_and(cyan1, cyan1, mask=console_mask)
    texts.append(_tess(cyan_masked))
    
    # 11) White text on teal/cyan background (title bar of error dialogs)
    # Find teal bg area, then extract white text within it
    teal_bg = cv2.inRange(hsv, (75, 50, 80), (105, 255, 255))
    teal_bg = cv2.dilate(teal_bg, np.ones((10, 10), np.uint8))  # expand area
    white_on_teal = cv2.bitwise_and(white_text_masked, white_text_masked, mask=teal_bg)
    texts.append(_tess(white_on_teal))
    
    # 12) Purple/Magenta text extraction (for dialog codes like "E 7036")
    # Purple-magenta in HSV: H=125-170, covers blue-purple-magenta-pink
    p1 = cv2.inRange(hsv, (125, 30, 50), (170, 255, 255))
    p1 = cv2.dilate(p1, np.ones((2, 2), np.uint8))
    p1_masked = cv2.bitwise_and(p1, p1, mask=console_mask)
    texts.append(_tess(p1_masked))
    
    return "\n".join(t for t in texts if t).strip()


def _tess(bw):
    """Run Tesseract with multiple PSM modes for better console text recognition"""
    if bw.size == 0:
        return ""
    
    configs = [
        "--oem 3 --psm 6",  # Default: uniform block of text
        "--oem 3 --psm 8",  # Single word
        "--oem 3 --psm 7",  # Single text line
        "--oem 3 --psm 13", # Raw line, treat as single text line, no hacks
    ]
    
    for config in configs:
        try:
            text = pytesseract.image_to_string(bw, config=config).strip()
            if text and len(text) > 2:  # If we get reasonable text, use it
                return text
        except Exception:
            continue
    return ""


# =====================================================================
# CODE EXTRACTION & CLASSIFICATION
# =====================================================================

def extract_codes(text):
    """Extract E/O/Alarm codes จาก dialog boxes"""
    out, seen = [], set()
    
    # รายชื่อ known codes จาก ERROR_DB
    known_codes = set(ERROR_DB.keys())
    
    # 1) "Code:" context → จาก dialog box จริง (เช่น "Code: E 225", "Code: O 691")
    # ต้อง validate ว่ามีใน ERROR_DB เพื่อกรอง OCR ที่อ่านผิด (E566 → E586, E568)
    for m in re.finditer(r"[Cc]ode[:\s]+([EOeo]\s*\d{3,5})", text, re.I):
        raw = m.group(1)
        clean = re.sub(r'\s+', '', raw).upper()
        code = clean
        if code in known_codes and code not in seen:
            seen.add(code)
            out.append(dict(raw=raw.strip(), norm=code, type=code[0]))
    
    # 2) Standalone E/O codes ที่ตรงกับ ERROR_DB เท่านั้น (ลด false positive)
    # กรอง codes ที่มี leading zeros (E00586, O00691) → console log format ไม่ใช่ dialog จริง
    for m in re.finditer(r"\b([Ee]\s*(\d{3,5}))\b", text):
        num = m.group(2)
        # Skip ถ้าเลขขึ้นต้นด้วย 0 (E00586 → console, E225/E586 → dialog)
        if num.startswith('0'):
            continue
        code = f"E{num}"
        if code in known_codes and code not in seen:
            seen.add(code)
            out.append(dict(raw=m.group(1).strip(), norm=code, type="E"))
    
    for m in re.finditer(r"\b([Oo]\s*(\d{3,5}))\b", text):
        num = m.group(2)
        if num.startswith('0'):
            continue
        code = f"O{num}"
        if code in known_codes and code not in seen:
            seen.add(code)
            out.append(dict(raw=m.group(1).strip(), norm=code, type="O"))
    
    # 3) Alarm codes (มี context "Alarm" ชัดเจน)
    # รวมรูปแบบ title bar: "Error [Alarm Code: 10019]"
    alarm_patterns = [
        r"[Aa]larm\s*[Cc]?o?d?e?\s*[:\s]*(\d{3,6})",
        r"\[Aa]?larm\s*[Cc]ode[:\s]*(\d{3,6})\]",
        r"[Aa]larm[:\s]*(\d{3,6})",
    ]
    for pat in alarm_patterns:
        for m in re.finditer(pat, text, re.I):
            code = f"ALARM{m.group(1)}"
            if code not in seen:
                seen.add(code)
                out.append(dict(raw=m.group(0).strip(), norm=code, type="ALARM"))
    
    return out


def classify(codes, text):
    """Match by exact code first, then keyword fallback (limited)."""
    lo = text.lower()
    matches = []

    # exact code match - ให้ priority สูงสุด
    for c in codes:
        n = c["norm"]
        if n in ERROR_DB:
            matches.append(dict(ERROR_DB[n], error_id=n, method="code_exact"))

    # ถ้ามี exact code match แล้ว → เก็บ descriptions ไว้ป้องกัน keyword ซ้ำ
    exact_descs = set()
    for m in matches:
        # เก็บ base description (ตัด " (Alarm xxxx)" suffix ออก)
        d = re.sub(r'\s*\(alarm.*\)', '', m["desc"].lower()).strip()
        exact_descs.add(d)

    # keyword match - ใช้ทั้ง errors ที่ไม่มี E/O code และ E/O code (fallback)
    # สำหรับ E/O codes: ใช้เฉพาะ keyword ที่เป็น multi-word pattern (มี .*) เพื่อลด false positive
    # สำหรับ non-E/O codes: ใช้ทุก keyword, ต้อง score >= 2
    for eid, db in ERROR_DB.items():
        # skip ถ้า match แล้ว
        if any(m["error_id"] == eid for m in matches):
            continue
        
        is_eo_code = bool(re.match(r'^[EO]\d+$', eid))
        
        if is_eo_code:
            # E/O codes: นับเฉพาะ multi-word keyword patterns (มี .* = contextual)
            # เพื่อป้องกัน false positive จาก single-word keywords เช่น "error", "wafer"
            specific_kw = [kw for kw in db["kw"] if '.*' in kw and re.search(kw, lo)]
            sc = len(specific_kw)
            min_score = 1  # multi-word pattern 1 ตัวก็พอ (เช่น gem.*host.*direction)
        else:
            # Non-E/O codes: ใช้ทุก keyword
            matched_kw = [kw for kw in db["kw"] if re.search(kw, lo)]
            sc = len(matched_kw)
            min_score = 2  # ต้อง >= 2 เพื่อลด false positive
        
        if sc >= min_score:
            # ถ้า description ซ้ำกับ exact code match → ข้าม
            kw_desc = db["desc"].lower().strip()
            if kw_desc in exact_descs:
                continue
            matches.append(dict(db, error_id=eid, method=f"keyword(score={sc})"))

    return matches if matches else None


# =====================================================================
# DRAW BANNER (No BBox)
# =====================================================================

def draw_banner(img, clf_list, error_ids, severity):
    """วาด banner สำหรับ multiple errors"""
    ann = img.copy()
    color = COLORS.get(severity, COLORS["unknown"])
    
    if clf_list:
        # แสดง error IDs ทั้งหมด
        ids_str = ", ".join(error_ids)
        cats = list(set(c['cat'] for c in clf_list))
        banner = f"{ids_str} | {', '.join(cats)} | {severity.upper()}"
    else:
        banner = f"UNCLASSIFIED"
    
    (bw, bh), _ = cv2.getTextSize(banner, FONT, 0.65, 2)
    cv2.rectangle(ann, (0, 0), (bw + 20, bh + 20), color, -1)
    cv2.putText(ann, banner, (10, bh + 10), FONT, 0.65,
                (255, 255, 255), 2, cv2.LINE_AA)
    
    return ann


# =====================================================================
# PROCESS SINGLE IMAGE
# =====================================================================

def process_image(path, out_root, machine_name="Unknown"):
    fname = os.path.basename(path)
    img = cv2.imread(path)
    if img is None:
        return None
    H, W = img.shape[:2]

    # OCR full image
    text = ocr_full(img)
    codes = extract_codes(text)
    
    # Classify - returns list of all matches
    clf_list = classify(codes, text)
    if clf_list:
        # ใช้ severity สูงสุด
        sev_order = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
        clf_list.sort(key=lambda x: sev_order.get(x["sev"], 0), reverse=True)
        sev = clf_list[0]["sev"]
        error_ids = [c["error_id"] for c in clf_list]
        cats = list(set(c["cat"] for c in clf_list))
        cat = cats[0] if len(cats) == 1 else "Multiple"
    else:
        cat, error_ids, sev = "Unknown", ["UNKNOWN"], "unknown"
        clf_list = None

    # Save outputs (flat in machine folder, no category subfolder)
    cv2.imwrite(os.path.join(out_root, fname), img)

    # Annotated (banner only)
    ann = draw_banner(img, clf_list, error_ids, sev)
    ann_path = os.path.join(out_root, f"annotated_{fname}")
    cv2.imwrite(ann_path, ann)

    return dict(
        filename=fname, path=path, size=dict(w=W, h=H),
        machine=machine_name,
        codes=[c["norm"] for c in codes],
        ocr_text=text[:500],  # preview
        classifications=clf_list, category=cat,
        error_ids=error_ids, severity=sev,
        error_count=len(error_ids) if clf_list else 0,
        files=dict(annotated=ann_path),
        ts=datetime.now().isoformat(),
    )


# =====================================================================
# REPORTS
# =====================================================================

def gen_reports(results, out, machine_name=None):
    """Generate reports. If machine_name is provided, it's a per-machine report."""
    valid = [r for r in results if r]
    total = len(results)
    n_valid = len(valid)
    n_clf = len([r for r in valid if r["category"] != "Unknown"])
    cats, sevs = {}, {}
    for r in valid:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
        sevs[r["severity"]] = sevs.get(r["severity"], 0) + 1

    label = machine_name if machine_name else "ALL MACHINES"

    # JSON
    jp = os.path.join(out, "error_report.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(dict(
            ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            machine=label,
            summary=dict(total=total, valid=n_valid, classified=n_clf,
                         categories=cats, severities=sevs),
            results=valid,
        ), f, indent=2, ensure_ascii=False, default=str)

    # CSV
    rows = []
    for r in valid:
        clf_list = r.get("classifications")
        rows.append(dict(
            machine=r.get("machine", ""),
            filename=r["filename"], category=r["category"],
            error_ids=", ".join(r["error_ids"]), severity=r["severity"],
            error_count=r.get("error_count", 0),
            codes=", ".join(r["codes"]),
            descriptions="; ".join(c["desc"] for c in clf_list) if clf_list else "",
            handlers=", ".join(set(c["handler"] for c in clf_list)) if clf_list else "",
        ))
    cp = os.path.join(out, "error_report.csv")
    pd.DataFrame(rows).to_csv(cp, index=False, encoding="utf-8-sig")

    # TXT summary
    lines = [
        "=" * 60,
        f"  ERROR DETECTION - {label}",
        "=" * 60,
        f"  Total: {total} | Classified: {n_clf} | Unknown: {n_valid - n_clf}",
        "",
        "  CATEGORIES:",
    ]
    for c, n in sorted(cats.items()):
        lines.append(f"    {c:<25}: {n}")
    lines.append("")
    for r in valid:
        ids = ", ".join(r["error_ids"])
        lines.append(f"  [{r['severity'].upper():>7}] {r['filename']} -> {ids}")
    lines.append("=" * 60)
    
    tp = os.path.join(out, "summary.txt")
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return jp, cp, tp


def gen_overall_report(all_results, out):
    """สร้าง overall summary รวมทุกเครื่อง"""
    valid = [r for r in all_results if r]
    total = len(all_results)
    n_valid = len(valid)
    n_clf = len([r for r in valid if r["category"] != "Unknown"])

    # Group by machine
    machines = {}
    for r in valid:
        m = r.get("machine", "Unknown")
        if m not in machines:
            machines[m] = {"total": 0, "classified": 0, "categories": {}, "severities": {}}
        machines[m]["total"] += 1
        if r["category"] != "Unknown":
            machines[m]["classified"] += 1
        cat = r["category"]
        machines[m]["categories"][cat] = machines[m]["categories"].get(cat, 0) + 1
        sev = r["severity"]
        machines[m]["severities"][sev] = machines[m]["severities"].get(sev, 0) + 1

    # Overall JSON
    jp = os.path.join(out, "overall_report.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(dict(
            ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            summary=dict(total=total, valid=n_valid, classified=n_clf),
            machines=machines,
            results=valid,
        ), f, indent=2, ensure_ascii=False, default=str)

    # Overall CSV
    rows = []
    for r in valid:
        clf_list = r.get("classifications")
        rows.append(dict(
            machine=r.get("machine", ""),
            filename=r["filename"], category=r["category"],
            error_ids=", ".join(r["error_ids"]), severity=r["severity"],
            error_count=r.get("error_count", 0),
            codes=", ".join(r["codes"]),
            descriptions="; ".join(c["desc"] for c in clf_list) if clf_list else "",
            handlers=", ".join(set(c["handler"] for c in clf_list)) if clf_list else "",
        ))
    cp = os.path.join(out, "overall_report.csv")
    pd.DataFrame(rows).to_csv(cp, index=False, encoding="utf-8-sig")

    # Overall TXT summary
    lines = [
        "=" * 60,
        "  OVERALL ERROR DETECTION - ALL MACHINES",
        "=" * 60,
        f"  Total: {total} | Classified: {n_clf} | Unknown: {n_valid - n_clf}",
        "",
    ]
    for mname in sorted(machines.keys()):
        mdata = machines[mname]
        lines.append(f"  {'─' * 50}")
        lines.append(f"  MACHINE: {mname}")
        lines.append(f"    Total: {mdata['total']} | Classified: {mdata['classified']}")
        lines.append(f"    Categories: {mdata['categories']}")
        lines.append(f"    Severities: {mdata['severities']}")
    lines.append("")
    lines.append(f"  {'─' * 50}")
    lines.append("  ALL RESULTS:")
    for r in valid:
        ids = ", ".join(r["error_ids"])
        lines.append(f"  [{r['severity'].upper():>7}] [{r.get('machine','?'):>12}] {r['filename']} -> {ids}")
    lines.append("=" * 60)

    tp = os.path.join(out, "overall_summary.txt")
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return jp, cp, tp


# =====================================================================
# MAIN
# =====================================================================

def discover_machines(data_path):
    """ค้นหา machine folders (subfolders) ใน data_path
    ถ้าไม่มี subfolder → ใช้ data_path เป็น machine เดียวชื่อ 'Default'
    """
    machines = []
    for entry in sorted(os.listdir(data_path)):
        full = os.path.join(data_path, entry)
        if os.path.isdir(full):
            machines.append((entry, full))
    if not machines:
        # fallback: ใช้ root folder เป็น machine เดียว
        machines = [("Default", data_path)]
    return machines


def main():
    print("=" * 60)
    print("  SIMPLE ERROR DETECTION (No BBox) - Per Machine")
    print("=" * 60)
    print(f"  Source: {DATA_PATH}\n")

    out = make_output(OUTPUT_BASE)
    print(f"  Output: {out}\n")

    # ค้นหา machine folders
    machines = discover_machines(DATA_PATH)
    print(f"  Found {len(machines)} machine(s): {', '.join(m[0] for m in machines)}\n")

    exts = ["*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG", "*.bmp"]
    all_results = []

    for machine_name, machine_path in machines:
        print(f"\n  {'━' * 50}")
        print(f"  MACHINE: {machine_name}")
        print(f"  {'━' * 50}")

        # สร้าง output folder สำหรับเครื่องนี้
        machine_out = make_machine_dirs(out, machine_name)

        # ค้นหาภาพในโฟลเดอร์เครื่อง
        files = sorted({f for e in exts
                        for f in glob.glob(os.path.join(machine_path, e))})
        if not files:
            print(f"    No images found in {machine_path}")
            continue
        print(f"    {len(files)} images\n")

        machine_results = []
        for i, fp in enumerate(files, 1):
            nm = os.path.basename(fp)
            print(f"    [{i:>3}/{len(files)}] {nm}", end="")
            r = process_image(fp, machine_out, machine_name=machine_name)
            machine_results.append(r)
            if r:
                ids = ", ".join(r["error_ids"])
                print(f"  ->  {r['severity'].upper():>7} | {r['category']} | {ids} ({r['error_count']} errors)")
            else:
                print("  ->  SKIP")

        # Per-machine reports
        print(f"\n    Generating reports for {machine_name}...")
        jp, cp, tp = gen_reports(machine_results, machine_out, machine_name=machine_name)
        print(f"    JSON: {jp}")
        print(f"    CSV : {cp}")
        print(f"    TXT : {tp}")

        machine_valid = [r for r in machine_results if r]
        machine_clf = [r for r in machine_valid if r["category"] != "Unknown"]
        print(f"    {machine_name}: {len(machine_clf)}/{len(machine_valid)} classified")

        all_results.extend(machine_results)

    # Overall report (รวมทุกเครื่อง)
    print(f"\n  {'━' * 50}")
    print(f"  OVERALL SUMMARY")
    print(f"  {'━' * 50}")
    jp, cp, tp = gen_overall_report(all_results, out)
    print(f"  JSON: {jp}")
    print(f"  CSV : {cp}")
    print(f"  TXT : {tp}")

    valid = [r for r in all_results if r]
    clf = [r for r in valid if r["category"] != "Unknown"]
    print(f"\n  DONE: {len(clf)}/{len(valid)} classified across {len(machines)} machine(s)")
    print("=" * 60)


if __name__ == "__main__":
    main()
