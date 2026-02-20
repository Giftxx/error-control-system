# ======================================================================
# Simple Error Detection (No BBox) - OCR Full Image + Match ERROR_DB
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
OUTPUT_BASE = r"C:\Users\chama\Desktop\Detect_Na\error_analysis_simple"

# === Severity Colors (for banner only) ===
COLORS = {
    "high":    (0, 0, 255),      # Red
    "medium":  (0, 140, 255),    # Orange
    "low":     (0, 220, 255),    # Yellow
    "unknown": (180, 180, 180),  # Gray
}
FONT = cv2.FONT_HERSHEY_SIMPLEX


# =====================================================================
# ERROR DATABASE
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
            "command.*execution.*error", "gp.?ib",
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

# Code extraction regex
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
    return root


# =====================================================================
# OCR (Full Image)
# =====================================================================

def ocr_full(img):
    """OCR ทั้งภาพ - ลองหลายวิธี"""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    texts = []
    
    # 1) Normal threshold
    gn = cv2.convertScaleAbs(g, alpha=1.3, beta=10)
    _, bn = cv2.threshold(gn, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    texts.append(_tess(bn))
    
    # 2) Inverted (for dark backgrounds)
    gi = cv2.bitwise_not(g)
    gi = cv2.convertScaleAbs(gi, alpha=1.5, beta=10)
    _, bi = cv2.threshold(gi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    texts.append(_tess(bi))
    
    # 3) Adaptive threshold
    ad = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 15, 8)
    texts.append(_tess(ad))
    
    # 4) Red channel extraction (for red error text)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    r1 = cv2.inRange(hsv, (0, 50, 80), (10, 255, 255))
    r2 = cv2.inRange(hsv, (170, 50, 80), (180, 255, 255))
    rm = cv2.bitwise_or(r1, r2)
    rm = cv2.dilate(rm, np.ones((2, 2), np.uint8))
    texts.append(_tess(rm))
    
    # 5) Green channel extraction (for green text on dark bg)
    gm = cv2.inRange(hsv, (35, 50, 80), (85, 255, 255))
    gm = cv2.dilate(gm, np.ones((2, 2), np.uint8))
    texts.append(_tess(gm))
    
    return "\n".join(t for t in texts if t).strip()


def _tess(bw):
    """Run Tesseract"""
    if bw.size == 0:
        return ""
    try:
        return pytesseract.image_to_string(
            bw, config="--oem 3 --psm 6"
        ).strip()
    except Exception:
        return ""


# =====================================================================
# CODE EXTRACTION & CLASSIFICATION
# =====================================================================

def extract_codes(text):
    """Extract E/O/Alarm codes"""
    out, seen = [], set()
    for pat, ctype in CODE_RE:
        for m in re.finditer(pat, text, re.I):
            n = f"{ctype}{m.group(1)}"
            if n not in seen:
                seen.add(n)
                out.append(dict(raw=m.group(0).strip(), norm=n, type=ctype))
    return out


def classify(codes, text):
    """Match by exact code first, then keyword fallback"""
    lo = text.lower()

    # exact code match
    for c in codes:
        n = c["norm"]
        if n in ERROR_DB:
            return dict(ERROR_DB[n], error_id=n, method="code_exact")

    # keyword match (best score)
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
# DRAW BANNER (No BBox)
# =====================================================================

def draw_banner(img, clf, error_id, severity):
    """วาดแค่ banner บนสุดแสดงผล error"""
    ann = img.copy()
    color = COLORS.get(severity, COLORS["unknown"])
    
    if clf:
        banner = f"{error_id} | {clf['cat']} | {severity.upper()} | {clf['desc']}"
    else:
        banner = f"UNCLASSIFIED | {error_id}"
    
    (bw, bh), _ = cv2.getTextSize(banner, FONT, 0.65, 2)
    cv2.rectangle(ann, (0, 0), (bw + 20, bh + 20), color, -1)
    cv2.putText(ann, banner, (10, bh + 10), FONT, 0.65,
                (255, 255, 255), 2, cv2.LINE_AA)
    
    return ann


# =====================================================================
# PROCESS SINGLE IMAGE
# =====================================================================

def process_image(path, out_root):
    fname = os.path.basename(path)
    img = cv2.imread(path)
    if img is None:
        return None
    H, W = img.shape[:2]

    # OCR full image
    text = ocr_full(img)
    codes = extract_codes(text)
    
    # Classify
    clf = classify(codes, text)
    if clf:
        cat = clf["cat"]
        eid = clf.get("error_id", "?")
        sev = clf["sev"]
    else:
        cat, eid, sev = "Unknown", "UNKNOWN", "unknown"

    # Save outputs
    cat_dir = os.path.join(out_root, cat)
    os.makedirs(cat_dir, exist_ok=True)

    # Original copy
    cv2.imwrite(os.path.join(cat_dir, fname), img)

    # Annotated (banner only)
    ann = draw_banner(img, clf, eid, sev)
    ann_path = os.path.join(cat_dir, f"annotated_{fname}")
    cv2.imwrite(ann_path, ann)

    return dict(
        filename=fname, path=path, size=dict(w=W, h=H),
        codes=[c["norm"] for c in codes],
        ocr_text=text[:500],  # preview
        classification=clf, category=cat,
        error_id=eid, severity=sev,
        files=dict(annotated=ann_path),
        ts=datetime.now().isoformat(),
    )


# =====================================================================
# REPORTS
# =====================================================================

def gen_reports(results, out):
    valid = [r for r in results if r]
    total = len(results)
    n_valid = len(valid)
    n_clf = len([r for r in valid if r["category"] != "Unknown"])
    cats, sevs = {}, {}
    for r in valid:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
        sevs[r["severity"]] = sevs.get(r["severity"], 0) + 1

    # JSON
    jp = os.path.join(out, "error_report.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(dict(
            ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            summary=dict(total=total, valid=n_valid, classified=n_clf,
                         categories=cats, severities=sevs),
            results=valid,
        ), f, indent=2, ensure_ascii=False, default=str)

    # CSV
    rows = []
    for r in valid:
        c = r["classification"]
        rows.append(dict(
            filename=r["filename"], category=r["category"],
            error_id=r["error_id"], severity=r["severity"],
            codes=", ".join(r["codes"]),
            method=c["method"] if c else "",
            description=c["desc"] if c else "",
            handler=c["handler"] if c else "",
        ))
    cp = os.path.join(out, "error_report.csv")
    pd.DataFrame(rows).to_csv(cp, index=False, encoding="utf-8-sig")

    # TXT summary
    lines = [
        "=" * 60,
        "  SIMPLE ERROR DETECTION (No BBox)",
        "=" * 60,
        f"  Total: {total} | Classified: {n_clf} | Unknown: {n_valid - n_clf}",
        "",
        "  CATEGORIES:",
    ]
    for c, n in sorted(cats.items()):
        lines.append(f"    {c:<25}: {n}")
    lines.append("")
    for r in valid:
        lines.append(f"  [{r['severity'].upper():>7}] {r['filename']} -> {r['error_id']}")
    lines.append("=" * 60)
    
    tp = os.path.join(out, "summary.txt")
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return jp, cp, tp


# =====================================================================
# MAIN
# =====================================================================

def main():
    print("=" * 60)
    print("  SIMPLE ERROR DETECTION (No BBox)")
    print("=" * 60)
    print(f"  Source: {DATA_PATH}\n")

    out = make_output(OUTPUT_BASE)
    print(f"  Output: {out}\n")

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
            print(f"  ->  {r['severity'].upper():>7} | {r['category']} | {r['error_id']}")
        else:
            print("  ->  SKIP")

    print("\n  Generating reports...")
    jp, cp, tp = gen_reports(results, out)
    print(f"  JSON: {jp}")
    print(f"  CSV : {cp}")
    print(f"  TXT : {tp}")

    valid = [r for r in results if r]
    clf = [r for r in valid if r["category"] != "Unknown"]
    print(f"\n  DONE: {len(clf)}/{len(valid)} classified")
    print("=" * 60)


if __name__ == "__main__":
    main()
