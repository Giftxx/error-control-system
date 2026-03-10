"""
db_helper.py — PostgreSQL CRUD สำหรับ Detect_Na Error Detection Dashboard
ใช้แทน CSV/JSON file operations ทั้งหมด

Tables: machines, error_codes, error_code_keywords, error_events
"""

import psycopg2
import psycopg2.extras
import os
import threading
from datetime import datetime
from contextlib import contextmanager

# =====================================================
# Connection Pool (simple thread-safe)
# =====================================================

_DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "ai_agent",
    "user": "athip",
    "password": "123456",
}

_local = threading.local()


def _get_conn():
    """Get a per-thread connection (auto-reconnect)."""
    conn = getattr(_local, "conn", None)
    if conn is None or conn.closed:
        conn = psycopg2.connect(**_DB_CONFIG)
        conn.autocommit = True
        _local.conn = conn
    return conn


@contextmanager
def get_cursor():
    """Context manager for a RealDictCursor."""
    conn = _get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def test_connection():
    """Test DB connection. Returns True if OK."""
    try:
        with get_cursor() as cur:
            cur.execute("SELECT 1")
            return True
    except Exception as e:
        print(f"[DB] Connection failed: {e}")
        return False


# =====================================================
# MACHINES CRUD
# =====================================================

def get_all_machines(active_only=False):
    """Return all machines as list of dicts."""
    with get_cursor() as cur:
        if active_only:
            cur.execute("SELECT * FROM machines WHERE is_active = true ORDER BY machine_id")
        else:
            cur.execute("SELECT * FROM machines ORDER BY machine_id")
        return cur.fetchall()


def get_all_machines_dict():
    """Return machines in the same format as config.py MACHINES dict.
    { "wftb33_01": {"ip": "10.246.12.29", "name": "Tester-01", "vnc_id": "...", "vnc_password": "...", "vnc_port": 5900}, ... }
    """
    machines = get_all_machines(active_only=True)
    result = {}
    for m in machines:
        result[m["machine_id"]] = {
            "ip": str(m["ip_address"]),
            "name": m["display_name"],
            "vnc_id": m["vnc_window_id"] or f"RealVNC_{m['machine_id']}",
            "vnc_password": m.get("vnc_password", "") or "",
            "vnc_port": m.get("vnc_port", 0) or 0,
        }
    return result


def get_machine(machine_id):
    """Get single machine by ID."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM machines WHERE machine_id = %s", (machine_id,))
        return cur.fetchone()


def upsert_machine(machine_id, display_name, ip_address, vnc_port=0, vnc_password="", vnc_window_id="", is_active=True):
    """Insert or update a machine."""
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO machines (machine_id, display_name, ip_address, vnc_port, vnc_password, vnc_window_id, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (machine_id) DO UPDATE SET
              display_name  = EXCLUDED.display_name,
              ip_address    = EXCLUDED.ip_address,
              vnc_port      = EXCLUDED.vnc_port,
              vnc_password  = EXCLUDED.vnc_password,
              vnc_window_id = EXCLUDED.vnc_window_id,
              is_active     = EXCLUDED.is_active
            RETURNING *;
        """, (machine_id, display_name, ip_address, vnc_port, vnc_password, vnc_window_id, is_active))
        return cur.fetchone()


def delete_machine(machine_id):
    """Delete a machine (will fail if events reference it)."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM machines WHERE machine_id = %s", (machine_id,))
        return cur.rowcount > 0


# =====================================================
# ERROR CODES CRUD
# =====================================================

def get_all_error_codes(active_only=False):
    """Return all error codes as list of dicts."""
    with get_cursor() as cur:
        if active_only:
            cur.execute("SELECT * FROM error_codes WHERE is_active = true ORDER BY error_id")
        else:
            cur.execute("SELECT * FROM error_codes ORDER BY error_id")
        return cur.fetchall()


def get_error_codes_with_keywords():
    """Return error codes with keywords (stored directly in error_codes.keywords column).
    Format matches _read_error_db() output:
    { error_id, category, description, severity, handler, keywords, updated_at }
    """
    with get_cursor() as cur:
        cur.execute("""
            SELECT error_id, category, description, severity, handler,
                   COALESCE(keywords, '') AS keywords, updated_at
            FROM error_codes
            WHERE is_active = true
            ORDER BY error_id;
        """)
        rows = cur.fetchall()
        result = []
        for r in rows:
            result.append({
                "error_id": r["error_id"],
                "category": r["category"],
                "description": r["description"],
                "severity": r["severity"],
                "handler": r["handler"],
                "keywords": r["keywords"] or "",
                "updated_at": r["updated_at"].strftime("%Y-%m-%d %H:%M:%S") if r["updated_at"] else "",
            })
        return result


def upsert_error_code(error_id, category, description, severity, handler, keywords="", is_active=True):
    """Insert or update an error code (keywords stored as pipe-delimited string).
    If keywords is None, existing keywords are preserved on update.
    """
    with get_cursor() as cur:
        if keywords is None:
            # Preserve existing keywords on update
            cur.execute("""
                INSERT INTO error_codes (error_id, category, description, severity, handler, keywords, is_active)
                VALUES (%s, %s, %s, %s, %s, '', %s)
                ON CONFLICT (error_id) DO UPDATE SET
                  category    = EXCLUDED.category,
                  description = EXCLUDED.description,
                  severity    = EXCLUDED.severity,
                  handler     = EXCLUDED.handler,
                  is_active   = EXCLUDED.is_active
                RETURNING *;
            """, (error_id, category, description, severity, handler, is_active))
        else:
            cur.execute("""
                INSERT INTO error_codes (error_id, category, description, severity, handler, keywords, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (error_id) DO UPDATE SET
                  category    = EXCLUDED.category,
                  description = EXCLUDED.description,
                  severity    = EXCLUDED.severity,
                  handler     = EXCLUDED.handler,
                  keywords    = EXCLUDED.keywords,
                  is_active   = EXCLUDED.is_active
                RETURNING *;
            """, (error_id, category, description, severity, handler, keywords or "", is_active))
        return cur.fetchone()


def delete_error_code(error_id):
    """Soft-delete an error code (set is_active=false).
    Hard delete would fail if events reference it.
    """
    with get_cursor() as cur:
        cur.execute(
            "UPDATE error_codes SET is_active = false WHERE error_id = %s RETURNING *",
            (error_id,)
        )
        return cur.fetchone()


def hard_delete_error_code(error_id):
    """Hard delete — only if no events reference it."""
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM error_events WHERE error_id = %s", (error_id,))
        if cur.fetchone()["cnt"] > 0:
            return delete_error_code(error_id)
        cur.execute("DELETE FROM error_codes WHERE error_id = %s RETURNING *", (error_id,))
        return cur.fetchone()


# =====================================================
# ERROR EVENTS CRUD
# =====================================================

def get_active_events(machine_id=None):
    """Get all OPEN events, optionally filtered by machine."""
    with get_cursor() as cur:
        if machine_id:
            cur.execute(
                "SELECT * FROM error_events WHERE status = 'OPEN' AND machine_id = %s ORDER BY first_seen_at DESC",
                (machine_id,)
            )
        else:
            cur.execute("SELECT * FROM error_events WHERE status = 'OPEN' ORDER BY first_seen_at DESC")
        rows = cur.fetchall()
        return [_event_to_dict(r) for r in rows]


def get_event_history(limit=100, machine_id=None):
    """Get CLOSED/REMOVED events."""
    with get_cursor() as cur:
        if machine_id:
            cur.execute(
                "SELECT * FROM error_events WHERE status IN ('CLOSED', 'REMOVED') AND machine_id = %s ORDER BY resolved_at DESC NULLS LAST LIMIT %s",
                (machine_id, limit)
            )
        else:
            cur.execute(
                "SELECT * FROM error_events WHERE status IN ('CLOSED', 'REMOVED') ORDER BY resolved_at DESC NULLS LAST LIMIT %s",
                (limit,)
            )
        rows = cur.fetchall()
        return [_event_to_dict(r) for r in rows]


def find_open_event(error_id, machine_id):
    """Find an existing OPEN event for this error+machine combo."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM error_events WHERE error_id = %s AND machine_id = %s AND status = 'OPEN'",
            (error_id, machine_id)
        )
        return cur.fetchone()


def create_event(error_id, machine_id, detection_method=None, severity=None,
                 category=None, description=None, handler=None, evidence_path=None):
    """Create a new OPEN event. Auto-inserts error_code if FK would fail.
    Rejects fail IDs (WAFER_FAIL_CLUSTER) — use create_fail_event() instead.
    """
    if is_fail_id(error_id):
        # Route to fail_events instead
        return create_fail_event(error_id, machine_id, detection_method, severity,
                                 category, description, handler, evidence_path)
    with get_cursor() as cur:
        # Auto-insert error_code if missing (avoids FK violation from new detections)
        cur.execute("""
            INSERT INTO error_codes (error_id, category, description, severity, handler, is_active)
            VALUES (%s, %s, %s, %s, %s, true)
            ON CONFLICT (error_id) DO NOTHING;
        """, (error_id, category or 'Unknown', description or '', severity or 'unknown', handler or ''))
        cur.execute("""
            INSERT INTO error_events
              (error_id, machine_id, status, detection_method, severity, category, description, handler, evidence_path)
            VALUES (%s, %s, 'OPEN', %s, %s, %s, %s, %s, %s)
            RETURNING *;
        """, (error_id, machine_id, detection_method, severity, category, description, handler, evidence_path))
        return cur.fetchone()


def update_event_seen(event_id, evidence_path=None, severity=None, description=None, detection_method=None):
    """Increment seen_count, reset miss_count. Also update severity/description/method if provided."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE error_events
            SET last_seen_at = NOW(), miss_count = 0, seen_count = seen_count + 1,
                evidence_path = COALESCE(%s, evidence_path),
                severity = COALESCE(%s, severity),
                description = COALESCE(%s, description),
                detection_method = COALESCE(%s, detection_method)
            WHERE id = %s AND status = 'OPEN'
            RETURNING *;
        """, (evidence_path, severity, description, detection_method, event_id))
        return cur.fetchone()


def increment_miss_count(machine_id, exclude_error_ids):
    """Increment miss_count for OPEN events on this machine, excluding the given error_ids."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE error_events
            SET miss_count = miss_count + 1
            WHERE machine_id = %s AND status = 'OPEN'
              AND error_id != ALL(%s::VARCHAR[])
            RETURNING *;
        """, (machine_id, exclude_error_ids or []))
        return cur.fetchall()


def close_timed_out_events(threshold=3):
    """Close events where miss_count >= threshold."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE error_events
            SET status = 'CLOSED', resolved_at = NOW(), evidence_path = NULL
            WHERE status = 'OPEN' AND miss_count >= %s
            RETURNING *;
        """, (threshold,))
        rows = cur.fetchall()
        return [_event_to_dict(r) for r in rows]


def close_event(event_id, resolution_note=None):
    """Manually close an event."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE error_events
            SET status = 'CLOSED', resolved_at = NOW(), resolution_note = %s, evidence_path = NULL
            WHERE id = %s AND status = 'OPEN'
            RETURNING *;
        """, (resolution_note, event_id))
        return cur.fetchone()


def close_all_open_events():
    """Close ALL open error events (used when monitor stops)."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE error_events
            SET status = 'CLOSED', resolved_at = NOW(), evidence_path = NULL
            WHERE status = 'OPEN'
            RETURNING *;
        """)
        rows = cur.fetchall()
        return [_event_to_dict(r) for r in rows]


def remove_events_by_machine(machine_id):
    """Delete ALL events (OPEN + CLOSED) for a given machine (used when machine is deleted)."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM error_events WHERE machine_id = %s", (machine_id,))
        count = cur.rowcount
        return count


def remove_events_by_error_id(error_id):
    """Remove all OPEN events for a given error_id (used when error code is deleted)."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE error_events
            SET status = 'REMOVED', resolved_at = NOW(), evidence_path = NULL
            WHERE error_id = %s AND status = 'OPEN'
            RETURNING *;
        """, (error_id,))
        rows = cur.fetchall()
        return [_event_to_dict(r) for r in rows]


def get_event_stats(days=7):
    """Error frequency stats for the last N days."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT error_id, category, severity,
                   COUNT(*) AS total_events,
                   COUNT(*) FILTER (WHERE status = 'OPEN') AS currently_open,
                   ROUND(AVG(seen_count), 1) AS avg_seen
            FROM error_events
            WHERE first_seen_at >= NOW() - (%s || ' days')::INTERVAL
            GROUP BY error_id, category, severity
            ORDER BY total_events DESC;
        """, (str(days),))
        return cur.fetchall()


def get_mttr():
    """Mean Time To Resolution by category."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT category,
                   COUNT(*) AS resolved_count,
                   ROUND(AVG(EXTRACT(EPOCH FROM (resolved_at - first_seen_at))) / 60, 1) AS avg_minutes
            FROM error_events
            WHERE status = 'CLOSED' AND resolved_at IS NOT NULL
            GROUP BY category
            ORDER BY avg_minutes DESC;
        """)
        return cur.fetchall()


# =====================================================
# FAIL CODES CRUD (separate from error codes)
# =====================================================

# IDs that are "fail detections" (visual wafer analysis only) → fail_codes / fail_events
# YIELD_VIOLATION is OCR-detected → stays in error_codes / error_events
FAIL_IDS = {'WAFER_FAIL_CLUSTER'}


def is_fail_id(error_id):
    """Return True if this ID belongs to the fail_codes domain."""
    return error_id in FAIL_IDS


def get_all_fail_codes(active_only=False):
    """Return all fail codes as list of dicts."""
    with get_cursor() as cur:
        if active_only:
            cur.execute("SELECT * FROM fail_codes WHERE is_active = true ORDER BY fail_id")
        else:
            cur.execute("SELECT * FROM fail_codes ORDER BY fail_id")
        return cur.fetchall()


def upsert_fail_code(fail_id, category, description, severity, handler, keywords="", is_active=True):
    """Insert or update a fail code."""
    with get_cursor() as cur:
        if keywords is None:
            cur.execute("""
                INSERT INTO fail_codes (fail_id, category, description, severity, handler, keywords, is_active)
                VALUES (%s, %s, %s, %s, %s, '', %s)
                ON CONFLICT (fail_id) DO UPDATE SET
                  category    = EXCLUDED.category,
                  description = EXCLUDED.description,
                  severity    = EXCLUDED.severity,
                  handler     = EXCLUDED.handler,
                  is_active   = EXCLUDED.is_active
                RETURNING *;
            """, (fail_id, category, description, severity, handler, is_active))
        else:
            cur.execute("""
                INSERT INTO fail_codes (fail_id, category, description, severity, handler, keywords, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (fail_id) DO UPDATE SET
                  category    = EXCLUDED.category,
                  description = EXCLUDED.description,
                  severity    = EXCLUDED.severity,
                  handler     = EXCLUDED.handler,
                  keywords    = EXCLUDED.keywords,
                  is_active   = EXCLUDED.is_active
                RETURNING *;
            """, (fail_id, category, description, severity, handler, keywords or "", is_active))
        return cur.fetchone()


def delete_fail_code(fail_id):
    """Soft-delete a fail code."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE fail_codes SET is_active = false WHERE fail_id = %s RETURNING *",
            (fail_id,)
        )
        return cur.fetchone()


# =====================================================
# FAIL EVENTS CRUD
# =====================================================

def get_active_fail_events(machine_id=None):
    """Get all OPEN fail events."""
    with get_cursor() as cur:
        if machine_id:
            cur.execute(
                "SELECT * FROM fail_events WHERE status = 'OPEN' AND machine_id = %s ORDER BY first_seen_at DESC",
                (machine_id,)
            )
        else:
            cur.execute("SELECT * FROM fail_events WHERE status = 'OPEN' ORDER BY first_seen_at DESC")
        rows = cur.fetchall()
        return [_fail_event_to_dict(r) for r in rows]


def get_fail_event_history(limit=100, machine_id=None):
    """Get CLOSED/REMOVED fail events."""
    with get_cursor() as cur:
        if machine_id:
            cur.execute(
                "SELECT * FROM fail_events WHERE status IN ('CLOSED', 'REMOVED') AND machine_id = %s ORDER BY resolved_at DESC NULLS LAST LIMIT %s",
                (machine_id, limit)
            )
        else:
            cur.execute(
                "SELECT * FROM fail_events WHERE status IN ('CLOSED', 'REMOVED') ORDER BY resolved_at DESC NULLS LAST LIMIT %s",
                (limit,)
            )
        rows = cur.fetchall()
        return [_fail_event_to_dict(r) for r in rows]


def find_open_fail_event(fail_id, machine_id):
    """Find an existing OPEN fail event for this fail+machine combo."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM fail_events WHERE fail_id = %s AND machine_id = %s AND status = 'OPEN'",
            (fail_id, machine_id)
        )
        return cur.fetchone()


def create_fail_event(fail_id, machine_id, detection_method=None, severity=None,
                      category=None, description=None, handler=None, evidence_path=None):
    """Create a new OPEN fail event. Auto-inserts fail_code if needed."""
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO fail_codes (fail_id, category, description, severity, handler, is_active)
            VALUES (%s, %s, %s, %s, %s, true)
            ON CONFLICT (fail_id) DO NOTHING;
        """, (fail_id, category or 'Yield / Wafer', description or '', severity or 'unknown', handler or ''))
        cur.execute("""
            INSERT INTO fail_events
              (fail_id, machine_id, status, detection_method, severity, category, description, handler, evidence_path)
            VALUES (%s, %s, 'OPEN', %s, %s, %s, %s, %s, %s)
            RETURNING *;
        """, (fail_id, machine_id, detection_method, severity, category, description, handler, evidence_path))
        return cur.fetchone()


def update_fail_event_seen(event_id, evidence_path=None, severity=None, description=None, detection_method=None):
    """Increment seen_count, reset miss_count for a fail event. Also update severity/description/method if provided."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE fail_events
            SET last_seen_at = NOW(), miss_count = 0, seen_count = seen_count + 1,
                evidence_path = COALESCE(%s, evidence_path),
                severity = COALESCE(%s, severity),
                description = COALESCE(%s, description),
                detection_method = COALESCE(%s, detection_method)
            WHERE id = %s AND status = 'OPEN'
            RETURNING *;
        """, (evidence_path, severity, description, detection_method, event_id))
        return cur.fetchone()


def increment_fail_miss_count(machine_id, exclude_fail_ids):
    """Increment miss_count for OPEN fail events on this machine, excluding given fail_ids."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE fail_events
            SET miss_count = miss_count + 1
            WHERE machine_id = %s AND status = 'OPEN'
              AND fail_id != ALL(%s::VARCHAR[])
            RETURNING *;
        """, (machine_id, exclude_fail_ids or []))
        return cur.fetchall()


def close_timed_out_fail_events(threshold=3):
    """Close fail events where miss_count >= threshold."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE fail_events
            SET status = 'CLOSED', resolved_at = NOW(), evidence_path = NULL
            WHERE status = 'OPEN' AND miss_count >= %s
            RETURNING *;
        """, (threshold,))
        rows = cur.fetchall()
        return [_fail_event_to_dict(r) for r in rows]


def close_fail_event(event_id, resolution_note=None):
    """Manually close a fail event."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE fail_events
            SET status = 'CLOSED', resolved_at = NOW(), resolution_note = %s, evidence_path = NULL
            WHERE id = %s AND status = 'OPEN'
            RETURNING *;
        """, (resolution_note, event_id))
        return cur.fetchone()


def close_all_open_fail_events():
    """Close ALL open fail events (used when monitor stops)."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE fail_events
            SET status = 'CLOSED', resolved_at = NOW(), evidence_path = NULL
            WHERE status = 'OPEN'
            RETURNING *;
        """)
        rows = cur.fetchall()
        return [_fail_event_to_dict(r) for r in rows]


def remove_fail_events_by_fail_id(fail_id):
    """Remove all OPEN fail events for a given fail_id."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE fail_events
            SET status = 'REMOVED', resolved_at = NOW(), evidence_path = NULL
            WHERE fail_id = %s AND status = 'OPEN'
            RETURNING *;
        """, (fail_id,))
        rows = cur.fetchall()
        return [_fail_event_to_dict(r) for r in rows]


def _fail_event_to_dict(row):
    """Convert a DB fail event row to dict format."""
    if row is None:
        return None
    return {
        "id": row["id"],
        "fail_id": row["fail_id"],
        "error_id": row["fail_id"],  # alias for frontend compatibility
        "machine_id": row["machine_id"],
        "status": row["status"],
        "first_seen_at": row["first_seen_at"].isoformat() if row["first_seen_at"] else None,
        "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None,
        "miss_count": row["miss_count"],
        "seen_count": row["seen_count"],
        "evidence_image": row.get("evidence_path"),
        "category": row.get("category", ""),
        "severity": row.get("severity", "unknown"),
        "description": row.get("description", ""),
        "handler": row.get("handler", ""),
        "method": row.get("detection_method", ""),
        "resolution_note": row.get("resolution_note", ""),
        "type": "fail",  # distinguish from error events
    }


def _event_to_dict(row):
    """Convert a DB event row to the same format as EventTracker events."""
    if row is None:
        return None
    return {
        "id": row["id"],
        "error_id": row["error_id"],
        "machine_id": row["machine_id"],
        "status": row["status"],
        "first_seen_at": row["first_seen_at"].isoformat() if row["first_seen_at"] else None,
        "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None,
        "miss_count": row["miss_count"],
        "seen_count": row["seen_count"],
        "evidence_image": row.get("evidence_path"),
        "category": row.get("category", ""),
        "severity": row.get("severity", "unknown"),
        "description": row.get("description", ""),
        "handler": row.get("handler", ""),
        "method": row.get("detection_method", ""),
        "resolution_note": row.get("resolution_note", ""),
    }


# =====================================================
# EVENT TRACKER (DB-backed, replaces event_tracker.py)
# =====================================================

class DBEventTracker:
    """
    Drop-in replacement for EventTracker that uses PostgreSQL.
    Same interface: update(), check_timeouts(), get_active(), get_history(),
                    remove_error(), stats(), save(), load()
    Also exposes .active as a property for compatibility with direct access.
    """

    def __init__(self, max_history=500, miss_threshold=1):
        self.max_history = max_history
        self.miss_threshold = miss_threshold

    @property
    def active(self):
        """Dict of OPEN events keyed by 'error_id@machine_id' — for compatibility."""
        events = get_active_events()
        result = {}
        for e in events:
            key = f"{e['error_id']}@{e['machine_id']}"
            result[key] = e
        return result

    @property
    def history(self):
        """List of CLOSED/REMOVED events — for compatibility."""
        return get_event_history(limit=self.max_history)

    def update(self, machine_id, detected_items, evidence_images=None):
        """Update from a capture cycle — same interface as EventTracker.update()"""
        evidence_images = evidence_images or {}
        detected_ids = set()

        for item in detected_items:
            eid = item["error_id"]
            detected_ids.add(eid)

            existing = find_open_event(eid, machine_id)
            if existing:
                update_event_seen(
                    existing["id"],
                    evidence_path=evidence_images.get(eid),
                    severity=item.get("severity"),
                    description=item.get("description"),
                    detection_method=item.get("method"),
                )
            else:
                create_event(
                    error_id=eid,
                    machine_id=machine_id,
                    detection_method=item.get("method", ""),
                    severity=item.get("severity", "unknown"),
                    category=item.get("category", ""),
                    description=item.get("description", ""),
                    handler=item.get("handler", ""),
                    evidence_path=evidence_images.get(eid),
                )

        # Increment miss_count for codes NOT detected
        if detected_ids:
            increment_miss_count(machine_id, list(detected_ids))
        else:
            # No detections → all OPEN events on this machine get miss++
            increment_miss_count(machine_id, [])

    def check_timeouts(self, threshold=None):
        """Close events with miss_count >= threshold."""
        th = self.miss_threshold if threshold is None else threshold
        closed = close_timed_out_events(th)
        return [f"{e['error_id']}@{e['machine_id']}" for e in closed]

    def close_all(self):
        """Close ALL open error events (used when monitor stops)."""
        closed = close_all_open_events()
        return [f"{e['error_id']}@{e['machine_id']}" for e in closed]

    def get_active(self):
        """Return list of all OPEN events."""
        return get_active_events()

    def get_history(self, limit=100):
        """Return most recent CLOSED/REMOVED events."""
        return get_event_history(limit=min(limit, self.max_history))

    def remove_error(self, error_id):
        """Remove all OPEN events for a given error_id."""
        removed = remove_events_by_error_id(error_id)
        return [f"{e['error_id']}@{e['machine_id']}" for e in removed]

    def save(self):
        """No-op — data is already in PostgreSQL."""
        pass

    def load(self):
        """No-op — data comes from PostgreSQL."""
        pass

    def stats(self):
        """Return summary statistics (same format as EventTracker.stats)."""
        events = get_active_events()
        machines = set(e["machine_id"] for e in events)
        severities = {}
        for e in events:
            s = e.get("severity", "unknown")
            severities[s] = severities.get(s, 0) + 1
        history = get_event_history(limit=1)  # just to get count
        # Get actual history count
        with get_cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM error_events WHERE status IN ('CLOSED', 'REMOVED')")
            history_count = cur.fetchone()["cnt"]
        return {
            "active_count": len(events),
            "history_count": history_count,
            "machines_affected": list(machines),
            "severities": severities,
        }


# =====================================================
# FAIL TRACKER (DB-backed, for fail detections)
# =====================================================

class DBFailTracker:
    """
    DB-backed tracker for fail detections (WAFER_FAIL_CLUSTER, YIELD_VIOLATION).
    Same interface as DBEventTracker but uses fail_codes/fail_events tables.
    """

    def __init__(self, miss_threshold=1):
        self.miss_threshold = miss_threshold

    @property
    def active(self):
        """Dict of OPEN fail events keyed by 'fail_id@machine_id'."""
        events = get_active_fail_events()
        result = {}
        for e in events:
            key = f"{e['fail_id']}@{e['machine_id']}"
            result[key] = e
        return result

    def update(self, machine_id, detected_items, evidence_images=None):
        """Update from a capture cycle — only for fail items."""
        evidence_images = evidence_images or {}
        detected_ids = set()

        for item in detected_items:
            fid = item.get("fail_id") or item.get("error_id")
            detected_ids.add(fid)

            existing = find_open_fail_event(fid, machine_id)
            if existing:
                update_fail_event_seen(
                    existing["id"],
                    evidence_path=evidence_images.get(fid),
                    severity=item.get("severity"),
                    description=item.get("description"),
                    detection_method=item.get("method"),
                )
            else:
                create_fail_event(
                    fail_id=fid,
                    machine_id=machine_id,
                    detection_method=item.get("method", ""),
                    severity=item.get("severity", "unknown"),
                    category=item.get("category", ""),
                    description=item.get("description", ""),
                    handler=item.get("handler", ""),
                    evidence_path=evidence_images.get(fid),
                )

        # Increment miss_count for fail codes NOT detected
        if detected_ids:
            increment_fail_miss_count(machine_id, list(detected_ids))
        else:
            increment_fail_miss_count(machine_id, [])

    def check_timeouts(self, threshold=None):
        """Close fail events with miss_count >= threshold."""
        th = self.miss_threshold if threshold is None else threshold
        closed = close_timed_out_fail_events(th)
        return [f"{e['fail_id']}@{e['machine_id']}" for e in closed]

    def close_all(self):
        """Close ALL open fail events (used when monitor stops)."""
        closed = close_all_open_fail_events()
        return [f"{e['fail_id']}@{e['machine_id']}" for e in closed]

    def get_active(self):
        """Return list of all OPEN fail events."""
        return get_active_fail_events()

    def get_history(self, limit=100):
        """Return most recent CLOSED/REMOVED fail events."""
        return get_fail_event_history(limit=limit)

    def remove_fail(self, fail_id):
        """Remove all OPEN events for a given fail_id."""
        removed = remove_fail_events_by_fail_id(fail_id)
        return [f"{e['fail_id']}@{e['machine_id']}" for e in removed]

    def save(self):
        pass

    def load(self):
        pass
