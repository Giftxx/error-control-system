"""
Detect_Na Fleet Monitor - Flask Backend API
Serves machine status, wafer images, and error detection results.
"""

from flask import Flask, jsonify, send_file, send_from_directory, request
from flask_cors import CORS
import os
import glob
import re
import json
import sys
import importlib
import threading
from datetime import datetime
from pathlib import Path
from config import (
    PROJECT_DIR, DATA_MERGED, DATA_ERROR, DATA_FULL_GREEN,
    WAFER_ANALYSIS, HOST, PORT, DEBUG, REFRESH_INTERVAL,
    ROI_RESULTS, PIPELINE_RESULT_JSON,
    get_latest_wafer_analysis, ERROR_ANALYSIS,
    get_latest_error_analysis
)
import db_helper

# Load MACHINES from DB (fallback to config if DB unavailable)
try:
    if db_helper.test_connection():
        MACHINES = db_helper.get_all_machines_dict()
        print(f"[DB] Loaded {len(MACHINES)} machines from PostgreSQL")
    else:
        from config import MACHINES, save_machines, load_machines
        print("[DB] PostgreSQL unavailable, using JSON fallback")
except Exception as e:
    from config import MACHINES, save_machines, load_machines
    print(f"[DB] DB connection error: {e}, using JSON fallback")

app = Flask(__name__, static_folder=None)
CORS(app)

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def get_latest_image(folder_path):
    """Get the most recent image file from a folder."""
    if not os.path.isdir(folder_path):
        return None
    patterns = ["*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG"]
    files = []
    for pat in patterns:
        files.extend(glob.glob(os.path.join(folder_path, pat)))
    if not files:
        return None
    # Sort by modification time, newest first
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


def get_image_count(folder_path):
    """Count images in a folder."""
    if not os.path.isdir(folder_path):
        return 0
    count = 0
    for ext in ["*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG"]:
        count += len(glob.glob(os.path.join(folder_path, ext)))
    return count


def parse_wafer_analysis():
    """Parse the wafer analysis summary to get per-machine results."""
    results = {}
    
    # Check subfolder structure for red analysis results
    for level in ["red_normal", "red_high_concern", "red_severe_concern"]:
        level_path = os.path.join(WAFER_ANALYSIS, level)
        if not os.path.isdir(level_path):
            continue
        
        # Walk through all files in this level
        for root, dirs, files in os.walk(level_path):
            for f in files:
                if f.lower().endswith(('.png', '.jpg', '.jpeg')):
                    # Parse machine ID from filename
                    for machine_id, info in MACHINES.items():
                        vnc_id = info["vnc_id"]
                        if machine_id.replace("_", "-") in f or vnc_id.lower().replace("realvnc_", "") in f.lower():
                            if machine_id not in results:
                                results[machine_id] = {"normal": 0, "high": 0, "severe": 0}
                            if "normal" in level:
                                results[machine_id]["normal"] += 1
                            elif "high" in level:
                                results[machine_id]["high"] += 1
                            elif "severe" in level:
                                results[machine_id]["severe"] += 1
    
    return results


def get_machine_status(machine_id, machine_info):
    """Determine status for a single machine based on latest capture analysis."""
    vnc_folder = machine_info["vnc_id"]
    merged_path = os.path.join(DATA_MERGED, vnc_folder)
    green_path = os.path.join(DATA_FULL_GREEN, vnc_folder)
    
    # Get image counts
    merged_count = get_image_count(merged_path)
    green_count = get_image_count(green_path)
    
    # Get latest image  
    latest_img = get_latest_image(merged_path)
    latest_green = get_latest_image(green_path)
    
    # Default status
    status = {
        "id": machine_id,
        "name": machine_info["name"],
        "ip": machine_info["ip"],
        "vnc_id": machine_info.get("vnc_id", ""),
        "vnc_port": machine_info.get("vnc_port", 5900) or 5900,
        "has_password": bool(machine_info.get("vnc_password", "")),
        "status": "OK",
        "statusLabel": "OK",
        "yield": 0.0,
        "yieldNote": "No Data",
        "description": "No captures available",
        "totalCaptures": merged_count,
        "greenCaptures": green_count,
        "hasImage": False,
        "imageType": "map",  # map or image
        "lastUpdate": None,
    }
    
    if latest_img:
        mtime = os.path.getmtime(latest_img)
        status["lastUpdate"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        status["hasImage"] = True
    
    if green_count > 0 and merged_count > 0:
        # Calculate approximate yield from green ratio
        approx_yield = (green_count / merged_count) * 100 if merged_count > 0 else 0
        # Clamp to reasonable range
        if approx_yield > 100:
            approx_yield = 99.0 + (hash(machine_id) % 10) / 10.0
        
        status["yield"] = round(min(approx_yield, 99.9), 1)
        status["description"] = f"Yield: {status['yield']}%"
        
        if status["yield"] >= 95:
            status["status"] = "OK"
            status["statusLabel"] = "OK"
            status["yieldNote"] = "Stable"
        elif status["yield"] >= 90:
            status["status"] = "WARNING"
            status["statusLabel"] = "WARNING"
            status["yieldNote"] = "Dipping"
        elif status["yield"] >= 80:
            status["status"] = "YIELD_FAIL"
            status["statusLabel"] = "YIELD FAIL"
            status["yieldNote"] = "Low Yield"
        else:
            status["status"] = "CRITICAL"
            status["statusLabel"] = "CRITICAL"
            status["yieldNote"] = "Critical"
    elif merged_count > 0:
        status["yield"] = 0
        status["description"] = f"{merged_count} captures, analysis pending"
        status["yieldNote"] = "Pending"
    
    return status


# ──────────────────────────────────────────────
# API Routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the frontend."""
    frontend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
    return send_from_directory(frontend_dir, "index.html")


@app.route("/error-codes")
def error_codes_page():
    """Serve the Error Management Console page."""
    frontend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
    return send_from_directory(frontend_dir, "error_codes.html")


@app.route("/css/<path:filename>")
def serve_css(filename):
    frontend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "css")
    return send_from_directory(frontend_dir, filename)


@app.route("/js/<path:filename>")
def serve_js(filename):
    frontend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "js")
    return send_from_directory(frontend_dir, filename)


@app.route("/images/<path:filename>")
def serve_images(filename):
    images_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "images")
    return send_from_directory(images_dir, filename)


@app.route("/api/machines")
def api_machines():
    """Get status of all machines."""
    machines = []
    wafer_results = parse_wafer_analysis()
    
    for machine_id, machine_info in MACHINES.items():
        status = get_machine_status(machine_id, machine_info)
        
        # Enrich with wafer analysis data
        if machine_id in wafer_results:
            wr = wafer_results[machine_id]
            total = wr["normal"] + wr["high"] + wr["severe"]
            if total > 0:
                health = round((wr["normal"] / total) * 100, 1)
                status["waferHealth"] = health
                status["waferNormal"] = wr["normal"]
                status["waferHigh"] = wr["high"]
                status["waferSevere"] = wr["severe"]
                
                if wr["severe"] > 0:
                    status["status"] = "YIELD_FAIL"
                    status["statusLabel"] = "YIELD FAIL"
                    status["yieldNote"] = "Severe Clusters"
                elif wr["high"] > 5:
                    status["status"] = "WARNING"
                    status["statusLabel"] = "WARNING"
                    status["yieldNote"] = "High Clusters"
        
        machines.append(status)
    
    return jsonify({
        "machines": machines,
        "totalMachines": len(machines),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "refreshInterval": REFRESH_INTERVAL,
    })


@app.route("/api/machines", methods=["POST"])
def api_add_machine():
    """Add a new machine."""
    global MACHINES
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    machine_id = data.get("id", "").strip()
    name = data.get("name", "").strip()
    ip = data.get("ip", "").strip()
    password = data.get("vnc_password", "").strip()
    vnc_id = data.get("vnc_id", "").strip()

    if not machine_id or not name:
        return jsonify({"error": "id and name are required"}), 400

    # Sanitise id: lowercase, underscores only
    machine_id = re.sub(r"[^a-z0-9_]", "_", machine_id.lower())

    if machine_id in MACHINES:
        return jsonify({"error": f"Machine '{machine_id}' already exists"}), 409

    # Auto-generate vnc_id if not provided
    if not vnc_id:
        vnc_id = f"RealVNC_{machine_id}"

    try:
        db_helper.upsert_machine(machine_id, name, ip, vnc_password=password, vnc_window_id=vnc_id)
        MACHINES = db_helper.get_all_machines_dict()  # refresh cache
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500

    return jsonify({"message": f"Machine '{machine_id}' added", "id": machine_id}), 201


@app.route("/api/machines/<machine_id>", methods=["DELETE"])
def api_delete_machine(machine_id):
    """Remove a machine (deletes all associated events first)."""
    global MACHINES
    if machine_id not in MACHINES:
        return jsonify({"error": "Machine not found"}), 404

    name = MACHINES[machine_id]["name"]
    try:
        # 1) Delete all events associated with this machine
        removed_count = db_helper.remove_events_by_machine(machine_id)
        # 2) Delete the machine itself
        db_helper.delete_machine(machine_id)
        MACHINES = db_helper.get_all_machines_dict()  # refresh cache
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500
    return jsonify({
        "message": f"Machine '{machine_id}' removed",
        "name": name,
        "events_deleted": removed_count
    })


@app.route("/api/machines/<machine_id>", methods=["PUT"])
def api_update_machine(machine_id):
    """Update machine properties (name, IP, password, vnc_id)."""
    global MACHINES
    if machine_id not in MACHINES:
        return jsonify({"error": "Machine not found"}), 404

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    current = MACHINES[machine_id]
    name = data.get("name", current["name"]).strip()
    ip = data.get("ip", current["ip"]).strip()
    password = data.get("vnc_password", None)  # None = don't change
    vnc_id = data.get("vnc_id", current.get("vnc_id", "")).strip()
    vnc_port = data.get("vnc_port", current.get("vnc_port", 5900))

    try:
        # If password is None, keep existing; if empty string, clear it
        if password is None:
            password = current.get("vnc_password", "")
        db_helper.upsert_machine(machine_id, name, ip,
                                  vnc_port=vnc_port,
                                  vnc_password=password,
                                  vnc_window_id=vnc_id)
        MACHINES = db_helper.get_all_machines_dict()  # refresh cache
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500

    return jsonify({"message": f"Machine '{machine_id}' updated"})


@app.route("/api/machines/<machine_id>")
def api_machine_detail(machine_id):
    """Get detailed info for a single machine."""
    if machine_id not in MACHINES:
        return jsonify({"error": "Machine not found"}), 404
    
    machine_info = MACHINES[machine_id]
    status = get_machine_status(machine_id, machine_info)
    
    # Add recent captures list
    vnc_folder = machine_info["vnc_id"]
    merged_path = os.path.join(DATA_MERGED, vnc_folder)
    
    recent_captures = []
    if os.path.isdir(merged_path):
        files = []
        for ext in ["*.png", "*.jpg", "*.jpeg"]:
            files.extend(glob.glob(os.path.join(merged_path, ext)))
        files.sort(key=os.path.getmtime, reverse=True)
        
        for f in files[:20]:  # Last 20 captures
            fname = os.path.basename(f)
            mtime = os.path.getmtime(f)
            recent_captures.append({
                "filename": fname,
                "timestamp": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "url": f"/api/images/{machine_id}/{fname}"
            })
    
    status["recentCaptures"] = recent_captures
    return jsonify(status)


@app.route("/api/images/<machine_id>/latest")
def api_latest_image(machine_id):
    """Serve the latest image for a machine."""
    if machine_id not in MACHINES:
        return jsonify({"error": "Machine not found"}), 404
    
    vnc_folder = MACHINES[machine_id]["vnc_id"]
    
    # Try merged first, then full green
    for base_path in [DATA_MERGED, DATA_FULL_GREEN]:
        folder = os.path.join(base_path, vnc_folder)
        latest = get_latest_image(folder)
        if latest:
            return send_file(latest, mimetype="image/png")
    
    return jsonify({"error": "No image available"}), 404


@app.route("/api/images/<machine_id>/<filename>")
def api_specific_image(machine_id, filename):
    """Serve a specific image file for a machine."""
    if machine_id not in MACHINES:
        return jsonify({"error": "Machine not found"}), 404
    
    vnc_folder = MACHINES[machine_id]["vnc_id"]
    
    for base_path in [DATA_MERGED, DATA_FULL_GREEN]:
        filepath = os.path.join(base_path, vnc_folder, filename)
        if os.path.isfile(filepath):
            return send_file(filepath, mimetype="image/png")
    
    return jsonify({"error": "Image not found"}), 404


@app.route("/api/errors")
def api_errors():
    """Get list of error screenshots."""
    errors = []
    if os.path.isdir(DATA_ERROR):
        files = []
        for ext in ["*.png", "*.jpg", "*.jpeg", "*.PNG"]:
            files.extend(glob.glob(os.path.join(DATA_ERROR, ext)))
        files.sort(key=os.path.getmtime, reverse=True)
        
        for f in files:
            fname = os.path.basename(f)
            mtime = os.path.getmtime(f)
            errors.append({
                "filename": fname,
                "timestamp": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "url": f"/api/errors/image/{fname}"
            })
    
    return jsonify({"errors": errors, "total": len(errors)})


@app.route("/api/errors/image/<filename>")
def api_error_image(filename):
    """Serve an error screenshot."""
    filepath = os.path.join(DATA_ERROR, filename)
    if os.path.isfile(filepath):
        return send_file(filepath, mimetype="image/png")
    return jsonify({"error": "Image not found"}), 404


@app.route("/api/summary")
def api_summary():
    """Get overall fleet summary statistics."""
    total_machines = len(MACHINES)
    total_captures = 0
    total_green = 0
    statuses = {"OK": 0, "WARNING": 0, "YIELD_FAIL": 0, "CRITICAL": 0, "ALARM": 0, "SITE_FAIL": 0}
    
    for machine_id, machine_info in MACHINES.items():
        vnc_folder = machine_info["vnc_id"]
        merged_path = os.path.join(DATA_MERGED, vnc_folder)
        green_path = os.path.join(DATA_FULL_GREEN, vnc_folder)
        total_captures += get_image_count(merged_path)
        total_green += get_image_count(green_path)
        
        status = get_machine_status(machine_id, machine_info)
        s = status["status"]
        if s in statuses:
            statuses[s] += 1
    
    error_count = get_image_count(DATA_ERROR) if os.path.isdir(DATA_ERROR) else 0
    
    return jsonify({
        "totalMachines": total_machines,
        "totalCaptures": total_captures,
        "totalGreen": total_green,
        "errorScreenshots": error_count,
        "statusCounts": statuses,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


import sys
sys.path.insert(0, PROJECT_DIR)

import csv

# ──────────────────────────────────────────────
# Error Analysis API Routes
# ──────────────────────────────────────────────


def _purge_error_from_reports(error_id):
    """Remove all references to a deleted error_id from cached error analysis reports.
    This ensures deleted errors don't reappear even if the pipeline hasn't re-run."""
    analysis_dir = get_latest_error_analysis()
    if not analysis_dir:
        return

    def _filter_results(results):
        """Remove items whose ONLY codes are the deleted error_id."""
        filtered = []
        for item in results:
            if not isinstance(item, dict):
                continue
            codes_in_item = set()
            for cls in item.get("classifications", []):
                if isinstance(cls, dict):
                    eid = cls.get("error_id", "")
                    if eid:
                        codes_in_item.add(eid)
            for c in item.get("codes", []):
                if c:
                    codes_in_item.add(c)
            for eid in item.get("error_ids", []):
                if eid:
                    codes_in_item.add(eid)
            # Keep if no codes or if there are other codes besides the deleted one
            remaining = codes_in_item - {error_id}
            if not codes_in_item or remaining:
                filtered.append(item)
        return filtered

    def _update_json(path):
        """Update a single JSON report file, removing the deleted error."""
        if not os.path.isfile(path):
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            changed = False
            if isinstance(raw, dict) and "results" in raw:
                original = raw["results"]
                raw["results"] = _filter_results(original)
                if len(raw["results"]) != len(original):
                    changed = True
                    # Update summary counts
                    if "summary" in raw:
                        raw["summary"]["total"] = len(raw["results"])
            elif isinstance(raw, list):
                filtered = _filter_results(raw)
                if len(filtered) != len(raw):
                    raw = filtered
                    changed = True
            if changed:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(raw, f, indent=2, ensure_ascii=False, default=str)
        except (json.JSONDecodeError, IOError):
            pass

    # 1) overall_report.json
    _update_json(os.path.join(analysis_dir, "overall_report.json"))

    # 2) Per-machine error_report.json
    for d in os.listdir(analysis_dir):
        mdir = os.path.join(analysis_dir, d)
        if os.path.isdir(mdir):
            _update_json(os.path.join(mdir, "error_report.json"))

    # 3) Flat error_report.json
    _update_json(os.path.join(analysis_dir, "error_report.json"))


@app.route("/api/error-analysis")
def api_error_analysis():
    """Get the latest error analysis results (supports per-machine structure)."""
    analysis_dir = get_latest_error_analysis()
    if not analysis_dir:
        return jsonify({"error": "No error analysis results found"}), 404

    result = {
        "analysis_folder": os.path.basename(analysis_dir),
        "summary": {},
        "results": [],
        "machines": {},
    }

    # 1) Try overall_report.json (new per-machine structure)
    overall_json = os.path.join(analysis_dir, "overall_report.json")
    if os.path.isfile(overall_json):
        try:
            with open(overall_json, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            result["summary"] = raw.get("summary", {})
            result["results"] = raw.get("results", [])
            result["machines"] = raw.get("machines", {})
            if raw.get("ts"):
                result["timestamp"] = raw["ts"]
            return jsonify(result)
        except (json.JSONDecodeError, IOError):
            pass

    # 2) Try per-machine folders
    machine_dirs = [d for d in os.listdir(analysis_dir)
                    if os.path.isdir(os.path.join(analysis_dir, d))]
    if machine_dirs:
        all_results = []
        machines_summary = {}
        for mdir in sorted(machine_dirs):
            mpath = os.path.join(analysis_dir, mdir)
            report_json = os.path.join(mpath, "error_report.json")
            if os.path.isfile(report_json):
                try:
                    with open(report_json, 'r', encoding='utf-8') as f:
                        raw = json.load(f)
                    if isinstance(raw, dict):
                        m_results = raw.get("results", [])
                        all_results.extend(m_results)
                        machines_summary[mdir] = raw.get("summary", {})
                except (json.JSONDecodeError, IOError):
                    pass
        if all_results:
            result["results"] = all_results
            result["machines"] = machines_summary
            result["summary"] = {
                "total": len(all_results),
                "classified": len([r for r in all_results if r.get("category") != "Unknown"]),
            }
            return jsonify(result)

    # 3) Fallback: old flat error_report.json
    report_json = os.path.join(analysis_dir, "error_report.json")
    if os.path.isfile(report_json):
        try:
            with open(report_json, 'r', encoding='utf-8') as f:
                raw = json.load(f)

            if isinstance(raw, dict):
                result["summary"] = raw.get("summary", {})
                result["results"] = raw.get("results", [])
                if raw.get("ts"):
                    result["timestamp"] = raw["ts"]
            elif isinstance(raw, list):
                report = raw
                severities = {}
                categories = {}
                handlers = {}
                for item in report:
                    if not isinstance(item, dict):
                        continue
                    sev = item.get("severity", "unknown")
                    severities[sev] = severities.get(sev, 0) + 1
                    for cls in item.get("classifications", []):
                        if isinstance(cls, dict):
                            cat = cls.get("cat", cls.get("category", "Unknown"))
                            categories[cat] = categories.get(cat, 0) + 1
                            handler = cls.get("handler", "Unknown")
                            handlers[handler] = handlers.get(handler, 0) + 1
                result["summary"] = {
                    "total": len(report),
                    "severities": severities,
                    "categories": categories,
                    "handlers": handlers,
                }
                result["results"] = report

            return jsonify(result)
        except (json.JSONDecodeError, IOError):
            pass

    return jsonify({"error": "No error report data found"}), 404


@app.route("/api/error-analysis/codes")
def api_error_codes():
    """Return the error code database as JSON."""
    try:
        codes = db_helper.get_error_codes_with_keywords()
        return jsonify({"codes": codes, "total": len(codes)})
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500


# ──────────────────────────────────────────────
# Error Code Management CRUD API Routes
# ──────────────────────────────────────────────


def _trigger_auto_scan():
    """Auto-trigger quick scan in background after error code changes.
    Non-blocking: starts scan in a thread so CRUD response returns immediately.
    """
    def _go():
        try:
            with _quick_scan_lock:
                if _quick_scan_state["running"]:
                    return  # already running
                total = _count_scan_images()
                if total == 0:
                    return  # no images to scan
                _quick_scan_state.update({
                    "running": True,
                    "progress": 0,
                    "total": total,
                    "current_file": "",
                    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "finished_at": None,
                    "result": None,
                    "error": None,
                })
            _run_quick_scan()
        except Exception as e:
            print(f"[AutoScan] Error: {e}")

    thread = threading.Thread(target=_go, daemon=True)
    thread.start()

def _read_error_db():
    """Read all error codes from PostgreSQL."""
    try:
        return db_helper.get_error_codes_with_keywords()
    except Exception as e:
        print(f"[DB] Error reading error codes: {e}")
        return []


def _write_error_db(codes):
    """Write error codes to PostgreSQL (upsert each code)."""
    try:
        for c in codes:
            db_helper.upsert_error_code(
                error_id=c.get("error_id", ""),
                category=c.get("category", ""),
                description=c.get("description", ""),
                severity=c.get("severity", "medium"),
                handler=c.get("handler", ""),
                keywords=c.get("keywords", ""),
            )
        return True
    except Exception as e:
        print(f"[DB] Error writing error codes: {e}")
        return False


@app.route("/api/error-codes")
def api_error_codes_list():
    """Return all error codes as JSON."""
    try:
        codes = db_helper.get_error_codes_with_keywords()
        return jsonify({"codes": codes, "total": len(codes)})
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500


@app.route("/api/error-codes", methods=["POST"])
def api_error_codes_add():
    """Add a new error code."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    error_id = data.get("error_id", "").strip()
    description = data.get("description", "").strip()
    if not error_id or not description:
        return jsonify({"error": "error_id and description are required"}), 400

    # Check duplicate
    existing = db_helper.get_all_error_codes()
    if any(c["error_id"] == error_id for c in existing):
        return jsonify({"error": f"Error code '{error_id}' already exists"}), 409

    try:
        db_helper.upsert_error_code(
            error_id=error_id,
            category=data.get("category", ""),
            description=description,
            severity=data.get("severity", "medium"),
            handler=data.get("handler", ""),
            keywords=data.get("keywords", ""),
        )
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500

    # Notify real-time monitor to reload error_db
    try:
        monitor = _get_monitor()
        if monitor:
            monitor.reload_error_db()
    except Exception:
        pass

    # Auto re-scan data_error/ images with updated error codes
    _trigger_auto_scan()

    return jsonify({"message": f"Error code '{error_id}' added", "error_id": error_id, "scanning": True}), 201


@app.route("/api/error-codes/<path:error_id>", methods=["PUT"])
def api_error_codes_update(error_id):
    """Update an existing error code."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    # Check exists
    existing = db_helper.get_all_error_codes()
    found = None
    for c in existing:
        if c["error_id"] == error_id:
            found = c
            break
    if not found:
        return jsonify({"error": f"Error code '{error_id}' not found"}), 404

    try:
        db_helper.upsert_error_code(
            error_id=error_id,
            category=data.get("category", found["category"]),
            description=data.get("description", found["description"]),
            severity=data.get("severity", found["severity"]),
            handler=data.get("handler", found["handler"]),
            keywords=data.get("keywords", None),  # None = don't touch keywords
        )
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500

    # Notify real-time monitor to reload error_db
    try:
        monitor = _get_monitor()
        if monitor:
            monitor.reload_error_db()
    except Exception:
        pass

    # Auto re-scan data_error/ images with updated error codes
    _trigger_auto_scan()

    return jsonify({"message": f"Error code '{error_id}' updated", "scanning": True})


@app.route("/api/error-codes/<path:error_id>", methods=["DELETE"])
def api_error_codes_delete(error_id):
    """Delete an error code (soft delete)."""
    try:
        result = db_helper.delete_error_code(error_id)
        if not result:
            return jsonify({"error": f"Error code '{error_id}' not found"}), 404
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500

    # Purge this error from cached analysis reports so it won't reappear
    _purge_error_from_reports(error_id)

    # Also remove from real-time monitor active tracking + reload error_db
    try:
        monitor = _get_monitor()
        if monitor:
            monitor.remove_error(error_id)
            monitor.reload_error_db()
    except Exception:
        pass

    # Auto re-scan data_error/ images with updated error codes
    _trigger_auto_scan()

    return jsonify({"message": f"Error code '{error_id}' deleted", "scanning": True})


@app.route("/api/error-codes/import", methods=["POST"])
def api_error_codes_import():
    """Import multiple error codes from a list."""
    data = request.get_json()
    if not data or "codes" not in data:
        return jsonify({"error": "JSON body with 'codes' array required"}), 400

    incoming = data["codes"]
    added = 0
    updated = 0

    existing_codes = db_helper.get_all_error_codes()
    existing_ids = {c["error_id"] for c in existing_codes}

    try:
        for item in incoming:
            eid = item.get("error_id", "").strip()
            if not eid:
                continue
            if eid in existing_ids:
                updated += 1
            else:
                added += 1
                existing_ids.add(eid)

            db_helper.upsert_error_code(
                error_id=eid,
                category=item.get("category", ""),
                description=item.get("description", ""),
                severity=item.get("severity", "medium"),
                handler=item.get("handler", ""),
                keywords=item.get("keywords", ""),
            )
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500

    return jsonify({
        "message": f"Imported {added} new, updated {updated} existing",
        "imported": added,
        "updated": updated,
        "total": added + updated + len(existing_codes) - updated,
    })


# ──────────────────────────────────────────────
# FAIL CODES API (separate from error codes)
# ──────────────────────────────────────────────

@app.route("/api/fail-codes")
def api_fail_codes_list():
    """Return all fail codes as JSON."""
    try:
        codes = db_helper.get_all_fail_codes()
        result = []
        for c in codes:
            result.append({
                "fail_id": c["fail_id"],
                "category": c.get("category", ""),
                "description": c.get("description", ""),
                "severity": c.get("severity", "medium"),
                "handler": c.get("handler", ""),
                "keywords": c.get("keywords", ""),
                "is_active": c.get("is_active", True),
            })
        return jsonify({"codes": result, "total": len(result)})
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500


@app.route("/api/fail-codes", methods=["POST"])
def api_fail_codes_add():
    """Add a new fail code."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    fail_id = data.get("fail_id", "").strip()
    if not fail_id:
        return jsonify({"error": "fail_id is required"}), 400
    try:
        db_helper.upsert_fail_code(
            fail_id=fail_id,
            category=data.get("category", "Yield / Wafer"),
            description=data.get("description", ""),
            severity=data.get("severity", "medium"),
            handler=data.get("handler", ""),
            keywords=data.get("keywords", ""),
        )
        return jsonify({"message": f"Fail code '{fail_id}' added"}), 201
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500


@app.route("/api/fail-codes/<path:fail_id>", methods=["PUT"])
def api_fail_codes_update(fail_id):
    """Update an existing fail code."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400
    try:
        db_helper.upsert_fail_code(
            fail_id=fail_id,
            category=data.get("category", "Yield / Wafer"),
            description=data.get("description", ""),
            severity=data.get("severity", "medium"),
            handler=data.get("handler", ""),
            keywords=data.get("keywords", None),
        )
        return jsonify({"message": f"Fail code '{fail_id}' updated"})
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500


@app.route("/api/fail-codes/<path:fail_id>", methods=["DELETE"])
def api_fail_codes_delete(fail_id):
    """Delete a fail code (soft delete)."""
    try:
        result = db_helper.delete_fail_code(fail_id)
        if not result:
            return jsonify({"error": f"Fail code '{fail_id}' not found"}), 404
        return jsonify({"message": f"Fail code '{fail_id}' deleted"})
    except Exception as e:
        return jsonify({"error": f"DB error: {e}"}), 500


@app.route("/api/active-fails")
def api_active_fails():
    """Get currently OPEN fail events from PostgreSQL."""
    try:
        events = db_helper.get_active_fail_events()
        for ev in events:
            mid = ev.get("machine_id", "")
            if mid in MACHINES:
                ev["machine_name"] = MACHINES[mid].get("name", mid)
            else:
                ev["machine_name"] = mid
        return jsonify(events)
    except Exception as e:
        print(f"[DB] Error fetching active fail events: {e}")
        return jsonify([])


@app.route("/api/fail-history")
def api_fail_history():
    """Get CLOSED fail events history from PostgreSQL."""
    limit = request.args.get("limit", 100, type=int)
    try:
        events = db_helper.get_fail_event_history(limit=limit)
        for ev in events:
            mid = ev.get("machine_id", "")
            if mid in MACHINES:
                ev["machine_name"] = MACHINES[mid].get("name", mid)
            else:
                ev["machine_name"] = mid
        return jsonify(events)
    except Exception as e:
        print(f"[DB] Error fetching fail event history: {e}")
        return jsonify([])


@app.route("/api/error-analysis/images/<path:subpath>")
def api_error_analysis_image(subpath):
    """Serve an annotated image from the error analysis folder."""
    analysis_dir = get_latest_error_analysis()
    if not analysis_dir:
        return jsonify({"error": "No error analysis found"}), 404

    filepath = os.path.join(analysis_dir, subpath)
    if os.path.isfile(filepath):
        return send_file(filepath, mimetype="image/png")
    return jsonify({"error": "Image not found"}), 404


# ──────────────────────────────────────────────
# Quick Scan — Run Step 3 only (OCR detection on data_error/)
# ──────────────────────────────────────────────

_quick_scan_state = {
    "running": False,
    "progress": 0,
    "total": 0,
    "current_file": "",
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}
_quick_scan_lock = threading.Lock()


def _run_quick_scan():
    """Background worker: run OCR error detection on all data_error/ images."""
    import importlib
    import sys
    sys.path.insert(0, PROJECT_DIR)

    try:
        simple = importlib.import_module("3_simple")
        # Reload error codes from PostgreSQL
        simple.ERROR_DB = simple.load_error_db()

        data_error_path = simple.DATA_PATH
        if not os.path.isdir(data_error_path):
            with _quick_scan_lock:
                _quick_scan_state["error"] = f"data_error folder not found: {data_error_path}"
                _quick_scan_state["running"] = False
                _quick_scan_state["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return

        out = simple.make_output(simple.OUTPUT_BASE)
        machines = simple.discover_machines(data_error_path)

        exts = ["*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG", "*.bmp"]
        all_results = []

        for machine_name, machine_path in machines:
            machine_out = simple.make_machine_dirs(out, machine_name)
            # Use set() to avoid duplicate files on Windows (case-insensitive)
            files = sorted({f for e in exts
                            for f in glob.glob(os.path.join(machine_path, e))})
            if not files:
                continue

            machine_results = []
            for idx, fp in enumerate(files, 1):
                with _quick_scan_lock:
                    _quick_scan_state["progress"] += 1
                    _quick_scan_state["current_file"] = os.path.basename(fp)

                try:
                    r = simple.process_image(fp, machine_out, machine_name=machine_name)
                    machine_results.append(r)
                except Exception as e:
                    print(f"[QuickScan] Error processing {fp}: {e}")

            # Generate per-machine report
            simple.gen_reports(machine_results, machine_out, machine_name=machine_name)
            all_results.extend(machine_results)

        # Generate overall report
        simple.gen_overall_report(all_results, out)

        valid = [r for r in all_results if r]
        clf = [r for r in valid if r.get("category") != "Unknown"]

        with _quick_scan_lock:
            _quick_scan_state["result"] = {
                "total_images": len(all_results),
                "detected": len(valid),
                "classified": len(clf),
                "machines": len(machines),
                "output_folder": out,
            }
            _quick_scan_state["running"] = False
            _quick_scan_state["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        print(f"[QuickScan] Done: {len(valid)} detected, {len(clf)} classified across {len(machines)} machine(s)")

    except Exception as e:
        import traceback
        traceback.print_exc()
        with _quick_scan_lock:
            _quick_scan_state["error"] = str(e)
            _quick_scan_state["running"] = False
            _quick_scan_state["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _count_scan_images():
    """Count total images in data_error/ for progress tracking."""
    import sys
    sys.path.insert(0, PROJECT_DIR)
    try:
        simple = importlib.import_module("3_simple")
        data_path = simple.DATA_PATH
    except Exception:
        data_path = DATA_ERROR

    if not os.path.isdir(data_path):
        return 0

    exts = ["*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG", "*.bmp"]
    total = 0
    for root_dir, dirs, _files in os.walk(data_path):
        total += len({f for e in exts for f in glob.glob(os.path.join(root_dir, e))})
    return total


@app.route("/api/quick-scan", methods=["POST"])
def api_quick_scan():
    """Trigger a quick re-scan of data_error/ images using current error codes from PostgreSQL.
    This runs Step 3 only (OCR + classify), much faster than the full pipeline.
    """
    with _quick_scan_lock:
        if _quick_scan_state["running"]:
            return jsonify({"error": "Quick scan is already running", "state": _quick_scan_state}), 409

        total = _count_scan_images()
        _quick_scan_state.update({
            "running": True,
            "progress": 0,
            "total": total,
            "current_file": "",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "result": None,
            "error": None,
        })

    thread = threading.Thread(target=_run_quick_scan, daemon=True)
    thread.start()

    return jsonify({
        "message": "Quick scan started",
        "total_images": total,
    }), 202


@app.route("/api/quick-scan/status")
def api_quick_scan_status():
    """Get current quick scan progress."""
    with _quick_scan_lock:
        return jsonify(dict(_quick_scan_state))


# ──────────────────────────────────────────────
# Pipeline API Routes
# ──────────────────────────────────────────────

@app.route("/api/pipeline/run", methods=["POST"])
def api_pipeline_run():
    """Trigger the wafer analysis pipeline (runs in background)."""
    try:
        from pipeline import run_pipeline, get_pipeline_state
    except ImportError as e:
        return jsonify({"error": f"Pipeline module not found: {e}"}), 500

    state = get_pipeline_state()
    if state["running"]:
        return jsonify({"error": "Pipeline is already running", "state": state}), 409

    data = request.get_json() or {}
    data_path = data.get("data_path", DATA_MERGED)
    roi_output = data.get("roi_output", ROI_RESULTS)

    if not os.path.isdir(data_path):
        return jsonify({"error": f"Data path not found: {data_path}"}), 400

    def _run():
        try:
            run_pipeline(data_path, roi_output=roi_output)
        except Exception as e:
            print(f"Pipeline error: {e}")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({
        "message": "Pipeline started",
        "data_path": data_path,
        "roi_output": roi_output,
    }), 202


@app.route("/api/pipeline/status")
def api_pipeline_status():
    """Get current pipeline status (running/progress)."""
    try:
        from pipeline import get_pipeline_state
        state = get_pipeline_state()
        return jsonify(state)
    except ImportError:
        return jsonify({"running": False, "error": "Pipeline module not found"})


@app.route("/api/pipeline/results")
def api_pipeline_results():
    """Get the latest pipeline results."""
    # Try loading from JSON file
    if os.path.isfile(PIPELINE_RESULT_JSON):
        try:
            with open(PIPELINE_RESULT_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return jsonify(data)
        except (json.JSONDecodeError, IOError):
            pass

    # Fallback: try pipeline module state
    try:
        from pipeline import get_pipeline_state
        state = get_pipeline_state()
        if state.get("last_result"):
            return jsonify(state["last_result"])
    except ImportError:
        pass

    return jsonify({"error": "No pipeline results available"}), 404


# ──────────────────────────────────────────────
# Real-Time Monitor API Routes
# ──────────────────────────────────────────────

def _get_monitor():
    """Get or create the real-time monitor singleton."""
    try:
        sys.path.insert(0, PROJECT_DIR)
        from realtime_monitor import get_monitor
        return get_monitor(capture_interval=30, miss_threshold=1)
    except ImportError as e:
        print(f"[API] Cannot import realtime_monitor: {e}")
        return None


@app.route("/api/monitor/start", methods=["POST"])
def api_monitor_start():
    """Start the real-time monitor.
    Optional JSON body: { "mode": "video_test", "source": "...", "interval": 5, "loop": true }
    """
    monitor = _get_monitor()
    if not monitor:
        return jsonify({"error": "Monitor module not available"}), 500
    if monitor.running:
        return jsonify({"error": "Monitor is already running", "status": monitor.get_status()}), 409

    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "live")

    if mode == "video_test":
        source = data.get("source", None)  # None = auto use data_error/
        interval = data.get("interval", 5)
        loop = data.get("loop", True)
        # Allow overriding miss_threshold for demo/test (default 3)
        miss_threshold = data.get("miss_threshold", None)
        if miss_threshold is not None:
            monitor.miss_threshold = int(miss_threshold)
        ok, err = monitor.start_video_test(source_path=source, interval=interval, loop=loop)
        if not ok:
            return jsonify({"error": err or "Failed to start video test"}), 400
        return jsonify({
            "message": "Monitor started in VIDEO TEST mode",
            "status": monitor.get_status(),
        }), 200
    else:
        monitor.start()
        return jsonify({"message": "Monitor started", "status": monitor.get_status()}), 200


@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    """Stop the real-time monitor."""
    monitor = _get_monitor()
    if not monitor:
        return jsonify({"error": "Monitor module not available"}), 500
    if not monitor.running:
        return jsonify({"error": "Monitor is not running"}), 409
    monitor.stop()
    return jsonify({"message": "Monitor stopped"}), 200


@app.route("/api/vnc/detect-port", methods=["POST"])
def api_vnc_detect_port():
    """Auto-detect VNC port for a given IP.
    JSON body: { "ip": "10.246.12.29" }
    Or:        { "machine_id": "wftb33_01" }
    Returns:   { "port": 59000, "ip": "..." } or error.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    ip = data.get("ip", "").strip()
    machine_id = data.get("machine_id", "").strip()
    if machine_id and machine_id in MACHINES:
        ip = ip or MACHINES[machine_id].get("ip", "")

    if not ip or ip == "0.0.0.0":
        return jsonify({"error": "Valid IP address required"}), 400

    try:
        sys.path.insert(0, PROJECT_DIR)
        from realtime_monitor import detect_vnc_port, _VNC_PORTS
        port = detect_vnc_port(ip, timeout=2)
        if port:
            return jsonify({"success": True, "port": port, "ip": ip})
        else:
            return jsonify({
                "success": False,
                "error": f"No VNC port found on {ip} (scanned: {_VNC_PORTS})",
                "ip": ip,
                "scanned_ports": _VNC_PORTS,
            }), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "ip": ip}), 500


@app.route("/api/vnc/test", methods=["POST"])
def api_vnc_test():
    """Test VNC connection to a machine.
    JSON body: { "ip": "10.246.12.29", "port": 5900, "password": "..." }
    Or:        { "machine_id": "wftb33_01" }  (uses saved credentials)
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    ip = data.get("ip", "").strip()
    port = data.get("port", 5900)
    password = data.get("password", "")

    # If machine_id provided, load from DB
    machine_id = data.get("machine_id", "").strip()
    if machine_id and machine_id in MACHINES:
        minfo = MACHINES[machine_id]
        ip = ip or minfo.get("ip", "")
        password = password or minfo.get("vnc_password", "")
        port = port or minfo.get("vnc_port", 5900) or 5900

    if not ip:
        return jsonify({"error": "IP address required"}), 400

    try:
        sys.path.insert(0, PROJECT_DIR)
        from realtime_monitor import vnc_screenshot, detect_vnc_port

        # Auto-detect port if not specified
        if not port:
            port = detect_vnc_port(ip) or 5900

        img = vnc_screenshot(ip, port, password, timeout=10)
        if img is not None:
            h, w = img.shape[:2]
            return jsonify({
                "success": True,
                "message": f"Connected! Screen: {w}x{h}",
                "width": w,
                "height": h,
                "ip": ip,
                "port": port,
            })
        else:
            return jsonify({
                "success": False,
                "message": f"Failed to capture from {ip}:{port}",
                "ip": ip,
                "port": port,
            }), 400
    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e),
            "ip": ip,
            "port": port,
        }), 500


@app.route("/api/vnc/test-all", methods=["POST"])
def api_vnc_test_all():
    """Test VNC connection to all configured machines."""
    results = {}
    try:
        sys.path.insert(0, PROJECT_DIR)
        from realtime_monitor import vnc_screenshot, detect_vnc_port
    except ImportError as e:
        return jsonify({"error": f"Cannot import monitor: {e}"}), 500

    for mid, minfo in MACHINES.items():
        ip = minfo.get("ip", "")
        if not ip or ip == "0.0.0.0":
            results[mid] = {"success": False, "message": "No IP configured", "name": minfo.get("name", mid)}
            continue

        port = minfo.get("vnc_port", 0) or 0
        password = minfo.get("vnc_password", "")

        if not port:
            port = detect_vnc_port(ip) or 5900

        try:
            img = vnc_screenshot(ip, port, password, timeout=10)
            if img is not None:
                h, w = img.shape[:2]
                results[mid] = {
                    "success": True,
                    "message": f"Connected! {w}x{h}",
                    "name": minfo.get("name", mid),
                    "ip": ip,
                    "port": port,
                }
            else:
                results[mid] = {
                    "success": False,
                    "message": f"Cannot capture from {ip}:{port}",
                    "name": minfo.get("name", mid),
                    "ip": ip,
                    "port": port,
                }
        except Exception as e:
            results[mid] = {
                "success": False,
                "message": str(e),
                "name": minfo.get("name", mid),
                "ip": ip,
                "port": port,
            }

    ok_count = sum(1 for r in results.values() if r.get("success"))
    return jsonify({
        "results": results,
        "total": len(results),
        "connected": ok_count,
        "failed": len(results) - ok_count,
    })


@app.route("/api/monitor/status")
def api_monitor_status():
    """Get real-time monitor status."""
    monitor = _get_monitor()
    if not monitor:
        return jsonify({"running": False, "error": "Monitor module not available"})
    return jsonify(monitor.get_status())


@app.route("/api/active-errors")
def api_active_errors():
    """Get currently OPEN error events from PostgreSQL."""
    try:
        events = db_helper.get_active_events()
        # Enrich with display_name so frontend can show human-readable machine names
        for ev in events:
            mid = ev.get("machine_id", "")
            if mid in MACHINES:
                ev["machine_name"] = MACHINES[mid].get("name", mid)
            else:
                ev["machine_name"] = mid
        return jsonify(events)
    except Exception as e:
        print(f"[DB] Error fetching active events: {e}")
        # Fallback to monitor if DB fails
        monitor = _get_monitor()
        if monitor:
            return jsonify(monitor.get_active_errors())
        return jsonify([])


@app.route("/api/error-history")
def api_error_history():
    """Get CLOSED error events history from PostgreSQL."""
    limit = request.args.get("limit", 100, type=int)
    try:
        events = db_helper.get_event_history(limit=limit)
        for ev in events:
            mid = ev.get("machine_id", "")
            if mid in MACHINES:
                ev["machine_name"] = MACHINES[mid].get("name", mid)
            else:
                ev["machine_name"] = mid
        return jsonify(events)
    except Exception as e:
        print(f"[DB] Error fetching event history: {e}")
        monitor = _get_monitor()
        if monitor:
            return jsonify(monitor.get_error_history(limit))
        return jsonify([])


@app.route("/api/evidence/<path:filename>")
def api_evidence_image(filename):
    """Serve an evidence image."""
    evidence_dir = os.path.join(PROJECT_DIR, "evidence")
    filepath = os.path.join(evidence_dir, filename)
    if os.path.isfile(filepath):
        return send_file(filepath, mimetype="image/png")
    return jsonify({"error": "Evidence image not found"}), 404


# ──────────────────────────────────────────────
# Wafer Analysis API Routes
# ──────────────────────────────────────────────

@app.route("/api/wafer-analysis")
def api_wafer_analysis():
    """Get detailed wafer analysis breakdown from latest analysis folder."""
    analysis_dir = get_latest_wafer_analysis()
    if not analysis_dir:
        return jsonify({"error": "No wafer analysis found"}), 404

    result = {
        "analysis_dir": os.path.basename(analysis_dir),
        "timestamp": None,
        "categories": {},
        "total": 0,
        "health_score": 0,
    }

    # Check for pipeline_result.json first
    result_json = os.path.join(analysis_dir, "pipeline_result.json")
    if os.path.isfile(result_json):
        try:
            with open(result_json, 'r', encoding='utf-8') as f:
                pipeline_data = json.load(f)
            result["timestamp"] = pipeline_data.get("timestamp")
            if "step2" in pipeline_data:
                s2 = pipeline_data["step2"]
                result["categories"] = {
                    "normal": s2.get("normal", 0),
                    "high": s2.get("high", 0),
                    "severe": s2.get("severe", 0),
                }
                result["total"] = s2.get("total_analyzed", 0)
                result["health_score"] = s2.get("health_score", 0)
                result["red_counts"] = s2.get("red_counts", {})
            if "step1" in pipeline_data:
                result["step1"] = pipeline_data["step1"]
            return jsonify(result)
        except (json.JSONDecodeError, IOError):
            pass

    # Fallback: count files in subfolders
    categories = {"normal": 0, "high": 0, "severe": 0}
    for level_name, key in [("red_normal", "normal"), ("red_high_concern", "high"), ("red_severe_concern", "severe")]:
        level_path = os.path.join(analysis_dir, level_name)
        if os.path.isdir(level_path):
            for root, dirs, files in os.walk(level_path):
                for f in files:
                    if f.lower().endswith(('.png', '.jpg', '.jpeg')):
                        categories[key] += 1

    result["categories"] = categories
    total = sum(categories.values())
    result["total"] = total
    result["health_score"] = round(categories["normal"] / total * 100, 1) if total > 0 else 0

    return jsonify(result)


def parse_wafer_analysis_folder():
    """Parse wafer analysis folder structure to extract fail details.
    First tries to read pipeline_result.json for accurate data,
    then falls back to folder structure parsing."""
    from config import get_latest_wafer_analysis
    
    analysis_dir = get_latest_wafer_analysis()
    if not analysis_dir or not os.path.isdir(analysis_dir):
        return None
    
    # --- Try pipeline_result.json first (most accurate) ---
    json_result = _try_read_pipeline_json(analysis_dir)
    if json_result:
        return json_result
    
    # --- Fallback: parse folder structure ---
    # Read analysis summary if available
    summary_file = os.path.join(analysis_dir, "analysis_summary.txt")
    total_analyzed = 0
    normal_count = 0
    high_count = 0
    severe_count = 0
    
    if os.path.isfile(summary_file):
        try:
            with open(summary_file, 'r', encoding='utf-8') as f:
                content = f.read()
                total_match = re.search(r'Total images processed:\s*(\d+)', content, re.IGNORECASE)
                if total_match:
                    total_analyzed = int(total_match.group(1))
                red_normal_match = re.search(r'Red Normal:\s*(\d+)', content, re.IGNORECASE)
                if red_normal_match:
                    normal_count = int(red_normal_match.group(1))
                red_high_match = re.search(r'Red High:\s*(\d+)', content, re.IGNORECASE) or re.search(r'High:\s*(\d+)', content, re.IGNORECASE)
                if red_high_match:
                    high_count = int(red_high_match.group(1))
                red_severe_match = re.search(r'Red Severe:\s*(\d+)', content, re.IGNORECASE) or re.search(r'Severe:\s*(\d+)', content, re.IGNORECASE)
                if red_severe_match:
                    severe_count = int(red_severe_match.group(1))
        except (IOError, ValueError):
            pass
    
    # If no summary file, count files in folders
    if total_analyzed == 0:
        normal_folder = os.path.join(analysis_dir, "red_normal")
        if os.path.isdir(normal_folder):
            for ext in ['*.png', '*.jpg', '*.jpeg']:
                normal_count += len(glob.glob(os.path.join(normal_folder, ext)))
        # Count high/severe from folders
        high_folder = os.path.join(analysis_dir, "red_high_concern")
        if os.path.isdir(high_folder):
            for f in _walk_image_files(high_folder):
                high_count += 1
        severe_folder = os.path.join(analysis_dir, "red_severe_concern")
        if os.path.isdir(severe_folder):
            for f in _walk_image_files(severe_folder):
                severe_count += 1
        total_analyzed = normal_count + high_count + severe_count
    
    health_score = round((normal_count / total_analyzed) * 100, 1) if total_analyzed > 0 else 0
    
    # Process HIGH and SEVERE concerns for fail details
    fails = []
    high_folder = os.path.join(analysis_dir, "red_high_concern")
    if os.path.isdir(high_folder):
        fails.extend(_process_concern_folder(high_folder, "HIGH", 1))
    severe_folder = os.path.join(analysis_dir, "red_severe_concern")
    if os.path.isdir(severe_folder):
        fails.extend(_process_concern_folder(severe_folder, "SEVERE", 2))
    
    fails.sort(key=lambda x: (-x["concern_level"], x["filename"]))
    
    # Get timestamp
    folder_name = os.path.basename(analysis_dir)
    timestamp_match = re.search(r'(\d{8})_(\d{6})', folder_name)
    timestamp = ""
    if timestamp_match:
        date_part = timestamp_match.group(1)
        time_part = timestamp_match.group(2)
        timestamp = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]} {time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}"
    
    return {
        "timestamp": timestamp,
        "total_analyzed": total_analyzed,
        "health_score": health_score,
        "summary": {
            "high": high_count,
            "severe": severe_count,
            "normal": normal_count,
        },
        "fails": fails,
        "total_fails": len(fails),
    }


def _walk_image_files(folder):
    """Generator: yield image file paths recursively."""
    for root, dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(('.png', '.jpg', '.jpeg')):
                yield os.path.join(root, f)


def _try_read_pipeline_json(analysis_dir):
    """Try to read pipeline_result.json for accurate per-file data."""
    json_path = os.path.join(analysis_dir, "pipeline_result.json")
    if not os.path.isfile(json_path):
        return None
    
    try:
        import json as _json
        with open(json_path, 'r', encoding='utf-8') as f:
            data = _json.load(f)
    except (IOError, ValueError):
        return None
    
    step2 = data.get("step2", {})
    details = data.get("details", {}).get("step2", [])
    
    if not details:
        return None
    
    timestamp = data.get("timestamp", "")
    total_analyzed = step2.get("total_analyzed", 0)
    normal_count = step2.get("normal", 0)
    high_count = step2.get("high", 0)
    severe_count = step2.get("severe", 0)
    health_score = step2.get("health_score", 0)
    
    # Build fails list from step2 details (only HIGH and SEVERE)
    fails = []
    for item in details:
        concern_level = item.get("concern_level", 0)
        if concern_level == 0:  # normal, skip
            continue
        
        filename = item.get("filename", "")
        status = item.get("status", "HIGH")
        location = item.get("location", "center")
        red_ratio = item.get("red_ratio", 0)
        largest_cluster_ratio = item.get("largest_cluster_ratio", 0)
        total_clusters = item.get("total_clusters", 0)
        
        # Extract machine name from filename
        machine = _extract_machine_name(filename)
        
        # Build image_path from status and location
        # e.g. red_severe_concern/center/filename.png or red_high_concern/edge/top_edge/filename.png
        concern_folder = "red_severe_concern" if concern_level >= 2 else "red_high_concern"
        if location == "center":
            image_path = f"{concern_folder}/center/{filename}"
        elif "/" in location:
            # e.g. edge/top_edge -> edge/top_edge
            loc_path = location  # already has edge/xxx_edge format
            image_path = f"{concern_folder}/{loc_path}/{filename}"
        else:
            image_path = f"{concern_folder}/{location}/{filename}"
        
        fails.append({
            "filename": filename,
            "machine": machine,
            "status": status,
            "concern_level": concern_level,
            "location": location,
            "image_path": image_path,
            "count_key": item.get("count_key", f"{status}_{location}"),
            "red_ratio": round(red_ratio * 100, 1) if red_ratio < 1 else red_ratio,
            "largest_cluster_ratio": round(largest_cluster_ratio * 100, 1) if largest_cluster_ratio < 1 else largest_cluster_ratio,
            "total_clusters": total_clusters,
        })
    
    fails.sort(key=lambda x: (-x["concern_level"], -x["largest_cluster_ratio"], x["filename"]))
    
    return {
        "timestamp": timestamp,
        "total_analyzed": total_analyzed,
        "health_score": health_score,
        "summary": {
            "high": high_count,
            "severe": severe_count,
            "normal": normal_count,
        },
        "fails": fails,
        "total_fails": len(fails),
    }


def _extract_machine_name(filename):
    """Extract and map machine name from wafer image filename."""
    machine = "Unknown"
    machine_match = re.search(r'\(([^)]+)\)', filename)
    if machine_match:
        raw_machine = machine_match.group(1).strip()
        for mid, minfo in MACHINES.items():
            vnc_id = minfo.get("vnc_id", "")
            vnc_short = vnc_id.replace("RealVNC_", "").replace("_", "-")
            machine_short = mid.replace("_", "-")
            if (raw_machine.lower() == vnc_short.lower() or 
                raw_machine.lower() == machine_short.lower()):
                machine = minfo.get("name", mid)
                break
        else:
            machine = raw_machine
    else:
        for mid, minfo in MACHINES.items():
            machine_short = mid.replace("_", "-").lower()
            vnc_id = minfo.get("vnc_id", "").replace("RealVNC_", "").replace("_", "-").lower()
            if machine_short in filename.lower() or vnc_id in filename.lower():
                machine = minfo.get("name", mid)
                break
    return machine


def _process_concern_folder(folder_path, status, concern_level):
    """Process a concern folder (HIGH or SEVERE) and extract fail details.
    Extracts specific edge direction from folder structure.
    Tries to look up real values from pipeline_result.json if available."""
    from config import get_latest_wafer_analysis
    analysis_dir = get_latest_wafer_analysis() or ""
    
    # Try to load pipeline_result.json for real per-file values
    json_lookup = {}
    if analysis_dir:
        json_path = os.path.join(analysis_dir, "pipeline_result.json")
        if os.path.isfile(json_path):
            try:
                import json as _json
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = _json.load(f)
                for item in data.get("details", {}).get("step2", []):
                    json_lookup[item.get("filename", "")] = item
            except (IOError, ValueError):
                pass
    
    fails = []
    
    for root, dirs, files in os.walk(folder_path):
        for filename in files:
            if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
                machine = _extract_machine_name(filename)
                
                # Determine location from folder structure (specific direction)
                location = "center"  # default
                rel_path = os.path.relpath(root, folder_path).lower()
                if "edge" in rel_path:
                    if "top_edge" in rel_path:
                        location = "edge/top_edge"
                    elif "bottom_edge" in rel_path:
                        location = "edge/bottom_edge"
                    elif "left_edge" in rel_path:
                        location = "edge/left_edge"
                    elif "right_edge" in rel_path:
                        location = "edge/right_edge"
                    elif "multiple" in rel_path:
                        location = "edge/multiple_edges"
                    else:
                        location = "edge"
                elif "center" in rel_path:
                    location = "center"
                
                # Build image_path relative to analysis_dir for serving via API
                image_path = ""
                if analysis_dir:
                    full_path = os.path.join(root, filename)
                    image_path = os.path.relpath(full_path, analysis_dir).replace("\\", "/")
                
                # Look up real values from JSON, fallback to 0
                file_data = json_lookup.get(filename, {})
                red_ratio = file_data.get("red_ratio", 0)
                largest_cluster_ratio = file_data.get("largest_cluster_ratio", 0)
                total_clusters = file_data.get("total_clusters", 0)
                
                fails.append({
                    "filename": filename,
                    "machine": machine,
                    "status": status,
                    "concern_level": concern_level,
                    "location": location,
                    "image_path": image_path,
                    "count_key": f"{status}_{location}",
                    "red_ratio": round(red_ratio * 100, 1) if red_ratio < 1 else red_ratio,
                    "largest_cluster_ratio": round(largest_cluster_ratio * 100, 1) if largest_cluster_ratio < 1 else largest_cluster_ratio,
                    "total_clusters": total_clusters,
                })
    
    return fails


@app.route("/api/wafer-fails")
def api_wafer_fails():
    """Return per-image wafer fail details (HIGH + SEVERE) from wafer analysis folder.
    Updated to read from wafer_analysis_* directory instead of pipeline JSON."""
    
    result = parse_wafer_analysis_folder()
    if result is None:
        return jsonify({"error": "No wafer analysis found", "fails": []}), 404
    
    return jsonify(result)


@app.route("/api/wafer-analysis/images/<path:subpath>")
def api_wafer_analysis_image(subpath):
    """Serve an image from the wafer analysis folder."""
    analysis_dir = get_latest_wafer_analysis()
    if not analysis_dir:
        return jsonify({"error": "No wafer analysis found"}), 404

    filepath = os.path.join(analysis_dir, subpath)
    if os.path.isfile(filepath):
        return send_file(filepath, mimetype="image/png")
    return jsonify({"error": "Image not found"}), 404


@app.route("/api/roi-results")
def api_roi_results():
    """Get ROI detection results summary."""
    if not os.path.isdir(ROI_RESULTS):
        return jsonify({"error": "ROI results not found"}), 404

    good_folder = os.path.join(ROI_RESULTS, "good_full_circle")
    bad_folder = os.path.join(ROI_RESULTS, "bad_partial_or_none")

    good_count = 0
    bad_count = 0

    if os.path.isdir(good_folder):
        for ext in ['*.png', '*.jpg', '*.jpeg']:
            good_count += len(glob.glob(os.path.join(good_folder, ext)))

    if os.path.isdir(bad_folder):
        for ext in ['*.png', '*.jpg', '*.jpeg']:
            bad_count += len(glob.glob(os.path.join(bad_folder, ext)))

    total = good_count + bad_count
    return jsonify({
        "good": good_count,
        "bad": bad_count,
        "total": total,
        "success_rate": round(good_count / total * 100, 1) if total > 0 else 0,
    })


# ──────────────────────────────────────────────
# Run
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    
    print(f"""
========================================
  Detect_Na Fleet Monitor - Backend API
========================================
  Dashboard: http://localhost:{PORT}
  API:       http://localhost:{PORT}/api/machines
========================================
    """)
    print(f"Project root: {PROJECT_DIR}")
    print(f"Machines: {list(MACHINES.keys())}")
    print(f"Data merged: {DATA_MERGED}")
    print(f"Data error:  {DATA_ERROR}")
    print()
    
    app.run(host=HOST, port=PORT, debug=DEBUG, use_reloader=False)
