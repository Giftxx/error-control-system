"""
Event Tracker — OPEN/CLOSED state machine for real-time error detection.
Manages active error events and moves them to history when resolved.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIVE_ERRORS_FILE = os.path.join(_SCRIPT_DIR, "active_errors.json")
ERROR_HISTORY_FILE = os.path.join(_SCRIPT_DIR, "error_history.json")
EVIDENCE_DIR = os.path.join(_SCRIPT_DIR, "evidence")

# Ensure evidence directory exists
Path(EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)


class EventTracker:
    """
    Tracks error events with OPEN/CLOSED lifecycle.
    Key = "ERROR_ID@MACHINE_ID"

    Event dict:
      {
        "error_id": "E225",
        "machine_id": "Tester-01",
        "status": "OPEN",
        "first_seen_at": "2026-03-02T10:00:00",
        "last_seen_at": "2026-03-02T10:05:00",
        "miss_count": 0,
        "seen_count": 5,
        "evidence_image": "E225_Tester-01_20260302_100000.png",
        "category": "PROBER",
        "severity": "high",
        "description": "...",
        "handler": "...",
        "method": "code_exact"
      }
    """

    def __init__(self, max_history=500):
        self.active = {}       # key → event dict
        self.history = []      # list of CLOSED events
        self.max_history = max_history
        self._lock = threading.Lock()
        self.load()

    # ─── Update from a capture cycle ───
    def update(self, machine_id, detected_items, evidence_images=None):
        """
        Update tracker with detected error codes for one machine.

        Args:
            machine_id: e.g. "Tester-01"
            detected_items: list of dicts with keys:
                error_id, category, severity, description, handler, method
            evidence_images: dict {error_id: image_path} — only for NEW events
        """
        now = datetime.now().isoformat()
        evidence_images = evidence_images or {}

        detected_ids = set()

        with self._lock:
            for item in detected_items:
                eid = item["error_id"]
                detected_ids.add(eid)
                key = f"{eid}@{machine_id}"

                if key in self.active:
                    # Existing → just update last_seen, reset miss_count
                    self.active[key]["last_seen_at"] = now
                    self.active[key]["miss_count"] = 0
                    self.active[key]["seen_count"] += 1
                else:
                    # New → create OPEN event
                    self.active[key] = {
                        "error_id": eid,
                        "machine_id": machine_id,
                        "status": "OPEN",
                        "first_seen_at": now,
                        "last_seen_at": now,
                        "miss_count": 0,
                        "seen_count": 1,
                        "evidence_image": evidence_images.get(eid),
                        "category": item.get("category", ""),
                        "severity": item.get("severity", "unknown"),
                        "description": item.get("description", ""),
                        "handler": item.get("handler", ""),
                        "method": item.get("method", ""),
                    }

            # Increment miss_count for codes NOT detected this cycle (same machine)
            for key, event in self.active.items():
                if event["machine_id"] == machine_id:
                    if event["error_id"] not in detected_ids:
                        event["miss_count"] += 1

    # ─── Check timeouts and close expired events ───
    def check_timeouts(self, threshold=3):
        """Move events with miss_count >= threshold to CLOSED."""
        to_close = []
        with self._lock:
            for key, event in self.active.items():
                if event["miss_count"] >= threshold:
                    to_close.append(key)

            for key in to_close:
                event = self.active.pop(key)
                event["status"] = "CLOSED"
                event["resolved_at"] = datetime.now().isoformat()
                self.history.append(event)

            # Trim history
            if len(self.history) > self.max_history:
                self.history = self.history[-self.max_history:]

        return to_close

    # ─── Get active events ───
    def get_active(self):
        """Return list of all OPEN events."""
        with self._lock:
            return list(self.active.values())

    # ─── Get history ───
    def get_history(self, limit=100):
        """Return most recent CLOSED events."""
        with self._lock:
            return list(reversed(self.history[-limit:]))

    # ─── Remove events for a specific error_id (when deleted from error_db) ───
    def remove_error(self, error_id):
        """Remove all active events for a given error_id across all machines."""
        removed = []
        with self._lock:
            to_remove = [k for k, v in self.active.items() if v["error_id"] == error_id]
            for key in to_remove:
                event = self.active.pop(key)
                event["status"] = "REMOVED"
                event["resolved_at"] = datetime.now().isoformat()
                self.history.append(event)
                removed.append(key)
        return removed

    # ─── Persistence ───
    def save(self):
        """Save state to JSON files."""
        with self._lock:
            active_list = list(self.active.values())
            history_list = self.history[-self.max_history:]

        try:
            with open(ACTIVE_ERRORS_FILE, "w", encoding="utf-8") as f:
                json.dump(active_list, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"[EventTracker] Error saving active_errors.json: {e}")

        try:
            with open(ERROR_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history_list, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"[EventTracker] Error saving error_history.json: {e}")

    def load(self):
        """Load state from JSON files (for restart recovery)."""
        with self._lock:
            # Load active
            if os.path.isfile(ACTIVE_ERRORS_FILE):
                try:
                    with open(ACTIVE_ERRORS_FILE, "r", encoding="utf-8") as f:
                        items = json.load(f)
                    self.active = {}
                    for item in items:
                        key = f"{item['error_id']}@{item['machine_id']}"
                        self.active[key] = item
                    print(f"[EventTracker] Loaded {len(self.active)} active events")
                except (json.JSONDecodeError, IOError, KeyError) as e:
                    print(f"[EventTracker] Error loading active_errors.json: {e}")
                    self.active = {}

            # Load history
            if os.path.isfile(ERROR_HISTORY_FILE):
                try:
                    with open(ERROR_HISTORY_FILE, "r", encoding="utf-8") as f:
                        self.history = json.load(f)
                    print(f"[EventTracker] Loaded {len(self.history)} history events")
                except (json.JSONDecodeError, IOError) as e:
                    print(f"[EventTracker] Error loading error_history.json: {e}")
                    self.history = []

    # ─── Stats ───
    def stats(self):
        """Return summary statistics."""
        with self._lock:
            active_count = len(self.active)
            machines = set(e["machine_id"] for e in self.active.values())
            severities = {}
            for e in self.active.values():
                s = e.get("severity", "unknown")
                severities[s] = severities.get(s, 0) + 1
            return {
                "active_count": active_count,
                "history_count": len(self.history),
                "machines_affected": list(machines),
                "severities": severities,
            }
