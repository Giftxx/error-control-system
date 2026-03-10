"""
Real-Time Monitor — Capture + OCR + Track events every 60 seconds.
Runs in a background thread, captures all VNC testers in parallel
(direct IP connection via RFB protocol — no VNC Viewer app needed),
performs OCR + error/fail detection, and updates the EventTracker.
"""

import os
import sys
import time
import json
import socket
import struct
import threading
import importlib
import re
import cv2
import numpy as np
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Paths ──
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EVIDENCE_DIR = os.path.join(_SCRIPT_DIR, "evidence")
Path(EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)

# ── Import siblings ──
sys.path.insert(0, _SCRIPT_DIR)
sys.path.insert(0, os.path.join(_SCRIPT_DIR, "dashboard", "backend"))
from event_tracker import EventTracker

# Try DB-backed tracker (PostgreSQL), fall back to JSON
try:
    import db_helper
    if db_helper.test_connection():
        _USE_DB_TRACKER = True
        print("[Monitor] Using DBEventTracker (PostgreSQL)")
    else:
        _USE_DB_TRACKER = False
        print("[Monitor] PostgreSQL unavailable, using JSON EventTracker")
except ImportError:
    _USE_DB_TRACKER = False
    print("[Monitor] db_helper not available, using JSON EventTracker")

# Lazy-import 3_simple (has side-effects: loads ERROR_DB, sets tesseract path)
_simple = None


def _get_simple():
    """Lazy import of 3_simple module."""
    global _simple
    if _simple is None:
        _simple = importlib.import_module("3_simple")
    return _simple


# ── Win32 capture (only on Windows) ──
try:
    import win32gui
    import win32ui
    import win32con
    import ctypes
    HAS_WIN32 = True

    # Enable DPI Awareness
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
except ImportError:
    HAS_WIN32 = False


# =====================================================================
# Window Capture
# =====================================================================

def find_vnc_window(vnc_id):
    """Find a VNC window by title containing the vnc_id string.
    Returns hwnd or None.
    """
    if not HAS_WIN32:
        return None

    result = [None]

    def enum_cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title and vnc_id.lower() in title.lower():
                result[0] = hwnd
                return False  # stop enumeration
        return True

    try:
        win32gui.EnumWindows(enum_cb, None)
    except Exception:
        pass
    return result[0]


def capture_window_hwnd(hwnd):
    """Capture a window by its hwnd. Returns numpy BGR image or None."""
    if not HAS_WIN32:
        return None
    try:
        rect = win32gui.GetWindowRect(hwnd)
        x, y, x2, y2 = rect
        width = x2 - x
        height = y2 - y
        if width <= 0 or height <= 0:
            return None

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()

        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)

        result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
        if result == 0:
            save_dc.BitBlt((0, 0), (width, height), mfc_dc, (0, 0), win32con.SRCCOPY)

        bmp_info = bitmap.GetInfo()
        bmp_str = bitmap.GetBitmapBits(True)

        img = np.frombuffer(bmp_str, dtype=np.uint8)
        img = img.reshape((bmp_info['bmHeight'], bmp_info['bmWidth'], 4))
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        win32gui.DeleteObject(bitmap.GetHandle())

        return img
    except Exception as e:
        print(f"[Monitor] Capture error: {e}")
        return None


# =====================================================================
# Direct VNC Screenshot (RFB Protocol — connect by IP, no viewer needed)
# =====================================================================

def _recv_exact(sock, n):
    """Receive exactly *n* bytes from a socket."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("VNC connection closed unexpectedly")
        buf += chunk
    return buf


def _vnc_des_encrypt(password, challenge):
    """DES-encrypt a 16-byte VNC challenge (VNC uses bit-reversed key)."""
    key = (password or "").encode("latin-1")[:8].ljust(8, b"\x00")
    # VNC reverses bits in each byte
    def _rev(b):
        r = 0
        for i in range(8):
            if b & (1 << i):
                r |= 1 << (7 - i)
        return r
    key = bytes(_rev(b) for b in key)
    try:
        from Crypto.Cipher import DES
        d = DES.new(key, DES.MODE_ECB)
        return d.encrypt(challenge[:8]) + d.encrypt(challenge[8:16])
    except ImportError:
        raise ImportError(
            "VNC password auth requires pycryptodome.\n"
            "Install with: pip install pycryptodome"
        )


# Common VNC ports to scan (includes non-standard 59000 used in some environments)
_VNC_PORTS = [5900, 59000, 5901, 5902, 5903, 59001, 59002, 5800]
# Cache: ip → discovered port
_port_cache = {}


def detect_vnc_port(host, timeout=2):
    """Auto-detect VNC port by trying common ports.
    Returns port number or None. Caches result per host."""
    if host in _port_cache:
        return _port_cache[host]

    for port in _VNC_PORTS:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            # Check if it speaks RFB (VNC protocol)
            banner = s.recv(12)
            s.close()
            if b"RFB" in banner:
                print(f"[VNC] Auto-detected {host} → port {port}")
                _port_cache[host] = port
                return port
        except Exception:
            pass

    print(f"[VNC] No VNC port found on {host} (tried {_VNC_PORTS})")
    _port_cache[host] = None
    return None


def vnc_screenshot(host, port=5900, password=None, timeout=15):
    """
    Capture a screenshot directly from a VNC server via the RFB protocol.
    Connects by IP address — no VNC Viewer app window needed.
    Supports: No auth (1), VNC password (2), VeNCrypt/TLS (19).

    Returns: numpy BGR image (OpenCV format) or None on failure.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))

        # ── Protocol Version ──
        srv_ver = _recv_exact(sock, 12).decode("ascii", errors="replace").strip()
        sock.send(b"RFB 003.008\n")
        version = 3 if "003.003" in srv_ver else 8

        # ── Security Handshake ──
        if version >= 7:
            n = struct.unpack("!B", _recv_exact(sock, 1))[0]
            if n == 0:
                rlen = struct.unpack("!I", _recv_exact(sock, 4))[0]
                reason = _recv_exact(sock, rlen).decode("utf-8", errors="replace")
                raise Exception(f"VNC error: {reason}")
            sec_types = list(_recv_exact(sock, n))
        else:
            sec_types = [struct.unpack("!I", _recv_exact(sock, 4))[0]]

        print(f"[VNC] {host}:{port} security types: {sec_types}")

        if 1 in sec_types:                             # None (no auth)
            if version >= 7:
                sock.send(struct.pack("!B", 1))
        elif 2 in sec_types:                           # VNC password
            if version >= 7:
                sock.send(struct.pack("!B", 2))
            challenge = _recv_exact(sock, 16)
            sock.send(_vnc_des_encrypt(password, challenge))
        elif 19 in sec_types:                          # VeNCrypt (RealVNC with TLS)
            sock.send(struct.pack("!B", 19))
            # VeNCrypt version negotiation
            srv_major = struct.unpack("!B", _recv_exact(sock, 1))[0]
            srv_minor = struct.unpack("!B", _recv_exact(sock, 1))[0]
            # We support 0.2
            sock.send(struct.pack("!BB", 0, 2))
            ack = struct.unpack("!B", _recv_exact(sock, 1))[0]
            if ack != 0:
                raise Exception(f"VeNCrypt version rejected (ack={ack})")

            # Get available sub-types
            n_subtypes = struct.unpack("!B", _recv_exact(sock, 1))[0]
            subtypes = []
            for _ in range(n_subtypes):
                st = struct.unpack("!I", _recv_exact(sock, 4))[0]
                subtypes.append(st)
            print(f"[VNC] VeNCrypt subtypes: {subtypes}")

            # Prefer: 258=TLSVnc, 257=TLSNone, 256=Plain, 2=VncAuth
            chosen = None
            for preferred in [258, 257, 256, 2]:
                if preferred in subtypes:
                    chosen = preferred
                    break

            if chosen is None:
                raise Exception(f"No supported VeNCrypt subtype in {subtypes}")

            sock.send(struct.pack("!I", chosen))
            # Read server acceptance (1 byte, 0 = ok)
            sub_ack = struct.unpack("!B", _recv_exact(sock, 1))[0]
            if sub_ack != 1:
                raise Exception(f"VeNCrypt subtype {chosen} rejected (ack={sub_ack})")

            if chosen in (257, 258):
                # TLS handshake (anonymous TLS, no cert verification)
                import ssl
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(sock, server_hostname=host)

            if chosen == 258:
                # TLSVnc: standard VNC auth inside TLS
                challenge = _recv_exact(sock, 16)
                sock.send(_vnc_des_encrypt(password, challenge))
            elif chosen == 256:
                # Plain: send username + password as plain text (over TLS)
                username = b""  # no username needed
                pwd = (password or "").encode("utf-8")
                sock.send(struct.pack("!II", len(username), len(pwd)))
                sock.send(username)
                sock.send(pwd)
            # 257 = TLSNone: no further auth needed

        else:
            raise Exception(f"Unsupported VNC security types: {sec_types}")

        # SecurityResult
        if version >= 8 or (version == 3 and 2 in sec_types):
            result_code = struct.unpack("!I", _recv_exact(sock, 4))[0]
            if result_code != 0:
                # Try to read error reason
                try:
                    rlen = struct.unpack("!I", _recv_exact(sock, 4))[0]
                    reason = _recv_exact(sock, rlen).decode("utf-8", errors="replace")
                    raise Exception(f"VNC authentication failed: {reason}")
                except Exception:
                    raise Exception("VNC authentication failed")

        # ── ClientInit (shared=True) ──
        sock.send(struct.pack("!B", 1))

        # ── ServerInit ──
        init = _recv_exact(sock, 24)
        width, height = struct.unpack("!HH", init[:4])
        name_len = struct.unpack("!I", init[20:24])[0]
        _recv_exact(sock, name_len)  # desktop name (discard)

        # ── SetPixelFormat: 32 bpp, little-endian, RGB ──
        pfmt = struct.pack("!BBBBHHHBBBxxx",
            32, 24, 0, 1,            # bpp, depth, big-endian=0, true-color=1
            255, 255, 255,           # r/g/b max
            16, 8, 0,                # r/g/b shift
        )
        sock.send(struct.pack("!Bxxx", 0) + pfmt)

        # ── SetEncodings: Raw only ──
        sock.send(struct.pack("!BxH", 2, 1) + struct.pack("!i", 0))

        # ── FramebufferUpdateRequest (full) ──
        sock.send(struct.pack("!BBHHHH", 3, 0, 0, 0, width, height))

        # ── Receive FramebufferUpdate ──
        pixels = np.zeros((height, width, 3), dtype=np.uint8)
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = struct.unpack("!B", _recv_exact(sock, 1))[0]
            if msg == 0:  # FramebufferUpdate
                _recv_exact(sock, 1)  # padding
                nrects = struct.unpack("!H", _recv_exact(sock, 2))[0]
                for _ in range(nrects):
                    rx, ry, rw, rh, enc = struct.unpack("!HHHHi",
                                                         _recv_exact(sock, 12))
                    if enc != 0:
                        raise Exception(f"Unsupported VNC encoding: {enc}")
                    raw = _recv_exact(sock, rw * rh * 4)
                    arr = np.frombuffer(raw, dtype=np.uint8).reshape(rh, rw, 4)
                    pixels[ry:ry+rh, rx:rx+rw] = arr[:, :, :3]  # BGR
                break
            elif msg == 1:  # SetColourMapEntries
                _recv_exact(sock, 1)
                _recv_exact(sock, 2)
                n = struct.unpack("!H", _recv_exact(sock, 2))[0]
                _recv_exact(sock, n * 6)
            elif msg == 2:  # Bell
                pass
            elif msg == 3:  # ServerCutText
                _recv_exact(sock, 3)
                tlen = struct.unpack("!I", _recv_exact(sock, 4))[0]
                _recv_exact(sock, tlen)
            else:
                break

        sock.close()
        print(f"[VNC] Captured {host}:{port} → {width}×{height}")
        return pixels

    except Exception as e:
        print(f"[VNC Direct] {host}:{port} — {e}")
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        return None


# =====================================================================
# Load machines config
# =====================================================================

def load_machines_config():
    """Load machines from PostgreSQL."""
    try:
        machines = db_helper.get_all_machines_dict()
        if machines:
            print(f"[Monitor] Loaded {len(machines)} machines from PostgreSQL")
            return machines
    except Exception as e:
        print(f"[Monitor] DB machines load error: {e}, using defaults")

    # Hardcoded defaults (fallback if DB is down)
    return {
        "wftb33_01": {"ip": "10.246.12.29", "name": "Tester-01", "vnc_id": "RealVNC_wftb33_01", "vnc_password": "", "vnc_port": 5900},
        "wftb33_02": {"ip": "10.246.12.30", "name": "Tester-02", "vnc_id": "RealVNC_wftb33_02", "vnc_password": "", "vnc_port": 5900},
        "wftb33_13": {"ip": "10.246.12.41", "name": "Tester-13", "vnc_id": "RealVNC_wftb33_13", "vnc_password": "", "vnc_port": 5900},
        "wftb33_14": {"ip": "10.246.12.42", "name": "Tester-14", "vnc_id": "RealVNC_wftb33_14", "vnc_password": "", "vnc_port": 5900},
    }


# =====================================================================
# Real-Time Monitor
# =====================================================================

class RealtimeMonitor:
    """
    Real-time monitor that captures VNC windows, runs OCR,
    and tracks error/fail events with OPEN/CLOSED lifecycle.
    """

    def __init__(self, capture_interval=60, miss_threshold=1, max_workers=4):
        self.capture_interval = capture_interval   # seconds between cycles
        self.miss_threshold = miss_threshold        # miss N cycles → CLOSED
        self.max_workers = max_workers
        # Use DB tracker if available, otherwise JSON file tracker
        if _USE_DB_TRACKER:
            self.tracker = db_helper.DBEventTracker(miss_threshold=miss_threshold)
            self.fail_tracker = db_helper.DBFailTracker(miss_threshold=miss_threshold)
        else:
            self.tracker = EventTracker()
            self.fail_tracker = None  # JSON mode has no separate fail tracker
        self.machines = load_machines_config()
        # Reverse lookup: display_name → machine_id  (e.g. "Tester-01" → "wftb33_01")
        self._name_to_id = {v.get("name", k): k for k, v in self.machines.items()}
        self.running = False
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Video test mode
        self._video_mode = False          # True = testing with video/images
        self._video_sources = {}          # machine_name → {"type": "video"|"folder", "path": ..., "cap": cv2.VideoCapture|None, "files": [...], "index": 0}
        self._video_loop = True           # loop video when it ends

        # Stats
        self.cycle_count = 0
        self.started_at = None
        self.last_cycle_at = None
        self.last_cycle_duration = 0
        self.last_cycle_results = {}  # machine_id → list of detected codes

    def _mid(self, display_name):
        """Resolve display_name (e.g. 'Tester-01') → machine_id (e.g. 'wftb33_01').
        Falls back to display_name if no match (for JSON EventTracker compat)."""
        return self._name_to_id.get(display_name, display_name)

    # ─── Start / Stop ───
    def start(self):
        """Start the monitor in a background daemon thread."""
        with self._lock:
            if self.running:
                return False

            # Close all stale events from previous run → start fresh
            try:
                old_errors = self.tracker.close_all()
                if old_errors:
                    print(f"[Monitor] Cleared {len(old_errors)} stale error events from previous run")
                if self.fail_tracker:
                    old_fails = self.fail_tracker.close_all()
                    if old_fails:
                        print(f"[Monitor] Cleared {len(old_fails)} stale fail events from previous run")
            except Exception as e:
                print(f"[Monitor] Warning: could not clear stale events: {e}")

            self.running = True
            self._stop_event.clear()
            self.started_at = datetime.now().isoformat()
            self.cycle_count = 0
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
            mode = "VIDEO TEST" if self._video_mode else "LIVE"
            print(f"[Monitor] Started [{mode}] (interval={self.capture_interval}s, miss_threshold={self.miss_threshold})")
            return True

    def start_video_test(self, source_path=None, interval=5, loop=True):
        """Start monitor in Video Test Mode.

        source_path can be:
          - None        → auto-use data_error/ folder (images per machine subfolder)
          - folder path → subfolders = machine names, images inside
          - video file  → single machine "Video-Test", read frames

        interval: seconds between cycles (faster for testing, default 5s)
        loop: restart from beginning when all frames consumed
        """
        with self._lock:
            if self.running:
                return False, "Monitor is already running"

            self._video_mode = True
            self._video_loop = loop
            self._video_sources = {}
            self.capture_interval = interval

            # Determine source
            if source_path is None:
                source_path = os.path.join(_SCRIPT_DIR, "data_error")
            elif not os.path.isabs(source_path):
                # Resolve relative paths against Detect_Na/ directory
                source_path = os.path.join(_SCRIPT_DIR, source_path)

            if not os.path.exists(source_path):
                self._video_mode = False
                return False, f"Path not found: {source_path}"

            if os.path.isfile(source_path):
                # Single video file
                cap = cv2.VideoCapture(source_path)
                if not cap.isOpened():
                    self._video_mode = False
                    return False, f"Cannot open video: {source_path}"
                fps = cap.get(cv2.CAP_PROP_FPS) or 30
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                self._video_sources["Video-Test"] = {
                    "type": "video",
                    "path": source_path,
                    "cap": cap,
                    "fps": fps,
                    "total_frames": total_frames,
                    "frame_index": 0,
                }
                print(f"[Monitor] Video test: {source_path} ({total_frames} frames, {fps:.1f} fps)")

            elif os.path.isdir(source_path):
                # Folder with machine subfolders containing images
                import glob as _glob
                exts = ["*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG", "*.bmp"]
                for entry in sorted(os.listdir(source_path)):
                    full = os.path.join(source_path, entry)
                    if not os.path.isdir(full):
                        continue
                    files = sorted({f for e in exts for f in _glob.glob(os.path.join(full, e))})
                    if files:
                        self._video_sources[entry] = {
                            "type": "folder",
                            "path": full,
                            "files": files,
                            "index": 0,
                        }
                        print(f"[Monitor] Video test: {entry} → {len(files)} images")

                # Also check for video files directly in source_path
                video_exts = ["*.mp4", "*.avi", "*.mkv", "*.mov", "*.MP4", "*.AVI"]
                for entry in sorted(os.listdir(source_path)):
                    full = os.path.join(source_path, entry)
                    if not os.path.isfile(full):
                        continue
                    if any(full.lower().endswith(e.replace("*", "")) for e in video_exts):
                        cap = cv2.VideoCapture(full)
                        if cap.isOpened():
                            name = os.path.splitext(entry)[0]
                            fps = cap.get(cv2.CAP_PROP_FPS) or 30
                            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                            self._video_sources[name] = {
                                "type": "video",
                                "path": full,
                                "cap": cap,
                                "fps": fps,
                                "total_frames": total_frames,
                                "frame_index": 0,
                            }
                            print(f"[Monitor] Video test: {name} → {total_frames} frames ({fps:.1f} fps)")

            if not self._video_sources:
                self._video_mode = False
                return False, f"No images or videos found in: {source_path}"

        # Start the monitor loop
        return self.start(), None

    def stop(self):
        """Stop the monitor gracefully."""
        with self._lock:
            if not self.running:
                return False
            self.running = False
            self._stop_event.set()

            # Release video captures
            for src in self._video_sources.values():
                if src.get("cap"):
                    try:
                        src["cap"].release()
                    except Exception:
                        pass
            self._video_mode = False
            self._video_sources = {}

            # Close ALL remaining open events — monitor is stopping
            try:
                closed_errors = self.tracker.close_all()
                if closed_errors:
                    print(f"[Monitor] Auto-closed {len(closed_errors)} error events on stop")
                if self.fail_tracker:
                    closed_fails = self.fail_tracker.close_all()
                    if closed_fails:
                        print(f"[Monitor] Auto-closed {len(closed_fails)} fail events on stop")
            except Exception as e:
                print(f"[Monitor] Error closing events on stop: {e}")

            print("[Monitor] Stop requested")
            return True

    # ─── Main loop ───
    def _run_loop(self):
        """Main capture-OCR-track loop."""
        while not self._stop_event.is_set():
            cycle_start = time.time()
            try:
                self._do_cycle()
            except Exception as e:
                print(f"[Monitor] Cycle error: {e}")

            self.cycle_count += 1
            self.last_cycle_at = datetime.now().isoformat()
            self.last_cycle_duration = round(time.time() - cycle_start, 2)

            # Sleep until next cycle
            elapsed = time.time() - cycle_start
            sleep_time = max(0, self.capture_interval - elapsed)
            self._stop_event.wait(timeout=sleep_time)

        # Save state on exit
        self.running = False
        # Release video captures if any
        if self._video_mode:
            for src in self._video_sources.values():
                if src.get("cap"):
                    try:
                        src["cap"].release()
                    except Exception:
                        pass
            self._video_mode = False
            self._video_sources = {}

        # Close ALL remaining open events — monitor is done, nothing to track
        closed_errors = self.tracker.close_all()
        if closed_errors:
            print(f"[Monitor] Auto-closed {len(closed_errors)} error events on stop")
        if self.fail_tracker:
            closed_fails = self.fail_tracker.close_all()
            if closed_fails:
                print(f"[Monitor] Auto-closed {len(closed_fails)} fail events on stop")

        self.tracker.save()
        print(f"[Monitor] Stopped after {self.cycle_count} cycles")

    def _do_cycle(self):
        """Single capture → OCR → track cycle for all machines in parallel.
        Frame-by-frame mode: detect first, then close old + create new atomically.
        Dashboard shows ONLY what is detected in the current frame.
        """
        simple = _get_simple()
        cycle_results = {}

        # 1) Capture images
        if self._video_mode:
            # Video test mode: read next frame from video/folder sources
            captured = self._capture_from_video_sources()
        else:
            # Live mode: capture VNC (direct IP → Win32 window → fallback)
            captured = {}  # machine_name → numpy image
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {}
                for mid, minfo in self.machines.items():
                    machine_name = minfo.get("name", mid)
                    futures[pool.submit(self._capture_machine, minfo)] = machine_name

                for future in as_completed(futures):
                    machine_name = futures[future]
                    try:
                        img = future.result()
                        if img is not None:
                            captured[machine_name] = img
                    except Exception as e:
                        print(f"[Monitor] Capture failed for {machine_name}: {e}")

            # 1b) Fallback: if no VNC windows captured, load latest images from data_error/
            if not captured:
                captured = self._fallback_load_data_error(simple)

        if not captured:
            # No images → close everything (nothing detected this frame)
            self.tracker.close_all()
            if self.fail_tracker:
                self.fail_tracker.close_all()
            self.tracker.save()
            self.last_cycle_results = cycle_results
            return

        # 2) Enhance video frames for better OCR (video compression degrades text)
        if self._video_mode:
            for machine_name in list(captured.keys()):
                captured[machine_name] = self._enhance_video_frame(captured[machine_name])

        # 3) OCR + Classify all captured images in parallel
        # Collect all detections FIRST, before touching the DB
        all_detections = []  # list of (tracker_mid, error_items, fail_items, error_evidence, fail_evidence)
        fast = self._video_mode  # Use fast OCR for video frames
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {}
            for machine_name, img in captured.items():
                futures[pool.submit(self._analyze_image, img, simple, fast_mode=fast)] = (machine_name, img)

            for future in as_completed(futures):
                machine_name, img = futures[future]
                try:
                    detected_items = future.result()
                    cycle_results[machine_name] = [d["error_id"] for d in detected_items]
                    tracker_mid = self._mid(machine_name)

                    # Split detected items into errors vs fails
                    error_items = []
                    fail_items = []
                    for item in detected_items:
                        eid = item["error_id"]
                        if _USE_DB_TRACKER and db_helper.is_fail_id(eid):
                            fail_items.append(item)
                        else:
                            error_items.append(item)

                    # Save evidence image for each detection
                    error_evidence = {}
                    for item in error_items:
                        eid = item["error_id"]
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        img_name = f"{eid}_{machine_name}_{ts}.png"
                        img_path = os.path.join(EVIDENCE_DIR, img_name)
                        try:
                            cv2.imwrite(img_path, img)
                            error_evidence[eid] = img_name
                        except Exception as e:
                            print(f"[Monitor] Evidence save error: {e}")

                    fail_evidence = {}
                    if self.fail_tracker:
                        for item in fail_items:
                            eid = item["error_id"]
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            img_name = f"{eid}_{machine_name}_{ts}.png"
                            img_path = os.path.join(EVIDENCE_DIR, img_name)
                            try:
                                cv2.imwrite(img_path, img)
                                fail_evidence[eid] = img_name
                            except Exception as e:
                                print(f"[Monitor] Evidence save error: {e}")

                    all_detections.append((tracker_mid, error_items, fail_items, error_evidence, fail_evidence))

                except Exception as e:
                    print(f"[Monitor] Analyze failed for {machine_name}: {e}")

        # ── Update events: reuse existing OPEN events, close only those not seen ──
        # Update first (creates new or bumps seen_count on existing)
        for tracker_mid, error_items, fail_items, error_evidence, fail_evidence in all_detections:
            self.tracker.update(tracker_mid, error_items, error_evidence)
            if self.fail_tracker:
                self.fail_tracker.update(tracker_mid, fail_items, fail_evidence)

        # Close events that exceeded miss threshold (not detected this cycle)
        self.tracker.check_timeouts()
        if self.fail_tracker:
            self.fail_tracker.check_timeouts()

        # 4) Save state
        self.tracker.save()
        if self.fail_tracker:
            self.fail_tracker.save()
        self.last_cycle_results = cycle_results

        # Log summary
        active = self.tracker.get_active()
        fail_active = self.fail_tracker.get_active() if self.fail_tracker else []
        if active or fail_active:
            parts = []
            if active:
                parts.append(", ".join(f"{e['error_id']}@{e['machine_id']}" for e in active))
            if fail_active:
                fid_key = 'fail_id' if 'fail_id' in (fail_active[0] if fail_active else {}) else 'error_id'
                parts.append(", ".join(f"{e[fid_key]}@{e['machine_id']}" for e in fail_active))
            print(f"[Monitor] Cycle #{self.cycle_count+1}: {len(active)} errors, {len(fail_active)} fails → {'; '.join(parts)}")
        else:
            print(f"[Monitor] Cycle #{self.cycle_count+1}: No active errors or fails")

    def _capture_from_video_sources(self):
        """Read the next frame/image from each video test source.
        Returns dict of machine_name → numpy image.
        """
        captured = {}
        exhausted_all = True

        for machine_name, src in self._video_sources.items():
            img = None

            if src["type"] == "video":
                cap = src["cap"]
                if cap and cap.isOpened():
                    fps = src.get("fps", 30)
                    current = src.get("frame_index", 0)
                    total = src.get("total_frames", 0)

                    # For video test: advance by 1 second worth of frames (not capture_interval)
                    # This gives denser coverage of the video content for better detection
                    if current == 0:
                        target = 0
                    else:
                        skip_frames = max(1, int(fps))  # 1 second of video per cycle
                        target = current + skip_frames

                    if target >= total:
                        if self._video_loop:
                            target = target % total if total > 0 else 0
                            cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                            print(f"[Monitor] Video loop restart: {machine_name}")
                        else:
                            # Exhausted
                            continue
                    else:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                    ret, frame = cap.read()
                    if ret:
                        img = frame
                        src["frame_index"] = target + 1
                        exhausted_all = False
                        video_sec = target / fps
                        print(f"[Monitor] Video [{machine_name}] frame {target}/{total} ({video_sec:.0f}s)")

            elif src["type"] == "folder":
                files = src["files"]
                idx = src["index"]
                if idx < len(files):
                    try:
                        img = cv2.imread(files[idx])
                    except Exception as e:
                        print(f"[Monitor] Read error {files[idx]}: {e}")
                    src["index"] = idx + 1
                    exhausted_all = False
                    fname = os.path.basename(files[idx])
                    print(f"[Monitor] Test [{machine_name}] image {idx+1}/{len(files)}: {fname}")
                elif self._video_loop:
                    src["index"] = 0
                    if files:
                        try:
                            img = cv2.imread(files[0])
                        except Exception:
                            pass
                        src["index"] = 1
                        exhausted_all = False
                        print(f"[Monitor] Folder loop restart: {machine_name} ({len(files)} images)")

            if img is not None:
                captured[machine_name] = img

        # If all sources are exhausted and not looping, stop the monitor
        if exhausted_all and not self._video_loop:
            print("[Monitor] All video sources exhausted, stopping...")
            self._stop_event.set()

        return captured

    def _fallback_load_data_error(self, simple):
        """Fallback: load the latest image per machine from data_error/ folder.
        Used when no VNC windows are available (e.g., dev machine, no VNC connections).
        Returns dict of machine_name → numpy image.
        """
        captured = {}
        data_path = getattr(simple, "DATA_PATH", os.path.join(_SCRIPT_DIR, "data_error"))
        if not os.path.isdir(data_path):
            return captured

        import glob as _glob
        exts = ["*.png", "*.jpg", "*.jpeg", "*.PNG", "*.JPG", "*.bmp"]

        for entry in sorted(os.listdir(data_path)):
            full = os.path.join(data_path, entry)
            if not os.path.isdir(full):
                continue
            machine_name = entry  # e.g., "Tester-01"

            # Find latest image by modification time
            files = sorted(
                {f for e in exts for f in _glob.glob(os.path.join(full, e))},
                key=os.path.getmtime,
                reverse=True
            )
            if not files:
                continue

            # Load the latest image
            latest = files[0]
            try:
                img = cv2.imread(latest)
                if img is not None:
                    captured[machine_name] = img
            except Exception as e:
                print(f"[Monitor] Fallback load error for {machine_name}: {e}")

        if captured:
            print(f"[Monitor] Fallback: loaded {len(captured)} images from data_error/")
        return captured

    def _capture_machine(self, minfo):
        """Capture from a machine.
        Priority: 1) Direct VNC via IP  2) Win32 window capture  3) None
        """
        ip = minfo.get("ip", "")
        port = minfo.get("vnc_port", 0)  # 0 = auto-detect
        password = minfo.get("vnc_password") or None
        vnc_id = minfo.get("vnc_id", "")
        name = minfo.get("name", "")

        # 1) Direct VNC via IP (primary — no viewer app needed)
        if ip:
            # Auto-detect port if not specified
            if not port:
                port = detect_vnc_port(ip) or 5900
            img = vnc_screenshot(ip, port, password, timeout=15)
            if img is not None:
                return img

        # 2) Win32 window capture (fallback — if VNC Viewer is open)
        if vnc_id and HAS_WIN32:
            hwnd = find_vnc_window(vnc_id)
            if hwnd is None:
                short_id = vnc_id.replace("RealVNC_", "")
                hwnd = find_vnc_window(short_id)
            if hwnd is not None:
                img = capture_window_hwnd(hwnd)
                if img is not None:
                    return img

        return None

    @staticmethod
    def _enhance_video_frame(img):
        """Enhance video frame for better OCR: 1.5× upscale + sharpen.
        Video compression degrades text; upscaling restores detail for Tesseract."""
        H, W = img.shape[:2]
        up = cv2.resize(img, (int(W * 1.5), int(H * 1.5)),
                         interpolation=cv2.INTER_CUBIC)
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
        sharp = cv2.filter2D(up, -1, kernel)
        return sharp

    @staticmethod
    def _fast_ocr(img, simple):
        """Fast OCR for video mode — 4 key preprocessing passes × 1 PSM mode.
        ~3× faster than full ocr_full() (11 passes × 4 PSM modes)."""
        import pytesseract

        console_mask = simple.mask_console_area(img)
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        g_filtered = cv2.bitwise_and(g, g, mask=console_mask)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def tess(bw):
            if bw.size == 0:
                return ""
            try:
                return pytesseract.image_to_string(
                    bw, config="--oem 3 --psm 6"
                ).strip()
            except Exception:
                return ""

        texts = []
        # 1) Normal threshold (catches most black-on-white text)
        gn = cv2.convertScaleAbs(g_filtered, alpha=1.3, beta=10)
        _, bn = cv2.threshold(gn, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        texts.append(tess(cv2.bitwise_and(bn, bn, mask=console_mask)))

        # 2) Inverted (white-on-dark text, title bars)
        gi = cv2.bitwise_not(g_filtered)
        gi = cv2.convertScaleAbs(gi, alpha=1.5, beta=10)
        _, bi = cv2.threshold(gi, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        texts.append(tess(cv2.bitwise_and(bi, bi, mask=console_mask)))

        # 3) Yellow popup text (yield violation dialogs)
        y1 = cv2.inRange(hsv, (20, 100, 100), (35, 255, 255))
        y1 = cv2.dilate(y1, np.ones((2, 2), np.uint8))
        texts.append(tess(cv2.bitwise_and(y1, y1, mask=console_mask)))

        # 4) Red text extraction (error messages)
        r1 = cv2.inRange(hsv, (0, 50, 80), (10, 255, 255))
        r2 = cv2.inRange(hsv, (170, 50, 80), (180, 255, 255))
        rm = cv2.bitwise_or(r1, r2)
        rm = cv2.dilate(rm, np.ones((2, 2), np.uint8))
        texts.append(tess(cv2.bitwise_and(rm, rm, mask=console_mask)))

        # 5) Green text (ALARM dialogs, GPIB errors on dark bg)
        g1 = cv2.inRange(hsv, (35, 50, 80), (85, 255, 255))
        g1 = cv2.morphologyEx(g1, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
        texts.append(tess(cv2.bitwise_and(g1, g1, mask=console_mask)))

        # 6) Adaptive threshold (catches mixed backgrounds)
        ad = cv2.adaptiveThreshold(g_filtered, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, 15, 8)
        texts.append(tess(cv2.bitwise_and(ad, ad, mask=console_mask)))

        # 7) Purple/Magenta text (dialog codes like "E 7036")
        p1 = cv2.inRange(hsv, (125, 30, 50), (170, 255, 255))
        p1 = cv2.dilate(p1, np.ones((2, 2), np.uint8))
        texts.append(tess(cv2.bitwise_and(p1, p1, mask=console_mask)))

        return "\n".join(t for t in texts if t).strip()

    def _analyze_image(self, img, simple, fast_mode=False):
        """
        Run OCR + extract codes + classify on a single image.
        Also runs wafer fail blob detection (visual, not OCR).
        fast_mode=True uses _fast_ocr (6 passes) for video frames.
        Returns list of detected items (dicts with error_id, category, severity, etc.)
        """
        # Reload ERROR_DB to pick up any changes
        simple.ERROR_DB = simple.load_error_db()

        if fast_mode:
            text = self._fast_ocr(img, simple)
        else:
            text = simple.ocr_full(img)

        # Supplement: detect small dialog boxes (blue/gray) and OCR at 2x scale
        dialog_text = self._ocr_dialog_boxes(img)
        if dialog_text:
            text = text + "\n" + dialog_text

        codes = simple.extract_codes(text)
        clf_list = simple.classify(codes, text)

        detected = []
        if clf_list:
            for c in clf_list:
                detected.append({
                    "error_id": c.get("error_id", "UNKNOWN"),
                    "category": c.get("cat", ""),
                    "severity": c.get("sev", "unknown"),
                    "description": c.get("desc", ""),
                    "handler": c.get("handler", ""),
                    "method": c.get("method", ""),
                })

        # ── Visual wafer fail blob detection ──
        # Error dialogs may co-exist with wafer fails on the same screen.
        # Always run wafer detection, but raise threshold when OCR errors
        # are present to reduce false positives from red dialog UI.
        has_ocr_errors = any(
            not (_USE_DB_TRACKER and db_helper.is_fail_id(d["error_id"]))
            for d in detected
        )
        wafer = self._detect_wafer_fail(img)
        if wafer and wafer["is_fail"]:
            # Only add if not already detected by OCR
            already = any(d["error_id"] == "WAFER_FAIL_CLUSTER" for d in detected)
            if not already:
                fp = wafer["fail_pct"]
                cc = wafer["cluster_count"]
                # When OCR errors are also present, require higher wafer fail %
                # to avoid false positives from red/yellow dialog UI elements.
                min_fail_pct = 5.0 if has_ocr_errors else 3.0
                if fp >= min_fail_pct:
                    desc = (f"Wafer fail cluster detected: {fp}% fail, "
                            f"{cc} cluster(s)")
                    severity = "high" if (fp > 10 or cc >= 15 or (fp > 7 and cc >= 10)) else "medium"
                    detected.append({
                        "error_id": "WAFER_FAIL_CLUSTER",
                        "category": "Yield / Wafer",
                        "severity": severity,
                        "description": desc,
                        "handler": "",
                        "method": f"visual(fail={fp}%,clusters={cc})",
                    })

        return detected

    @staticmethod
    def _ocr_dialog_boxes(img):
        """Detect and OCR small popup dialog boxes that full-image OCR misses.
        Uses two strategies:
        1. Center-region scaled OCR (dialogs typically appear center/bottom)
        2. Blue-title-bar detection for specific dialog types
        Returns supplementary OCR text from dialog regions.
        """
        import pytesseract
        h, w = img.shape[:2]
        texts = []

        # --- Strategy 1: Center-region scaled OCR ---
        # Dialog boxes typically appear in the center 40% of the screen
        # Extract this region and OCR at 2x scale for better text resolution
        roi_y1 = int(h * 0.40)
        roi_y2 = int(h * 0.88)
        roi_x1 = int(w * 0.30)
        roi_x2 = int(w * 0.75)
        center_roi = img[roi_y1:roi_y2, roi_x1:roi_x2]
        center_big = cv2.resize(center_roi, None, fx=2, fy=2,
                                interpolation=cv2.INTER_LANCZOS4)
        gray_center = cv2.cvtColor(center_big, cv2.COLOR_BGR2GRAY)

        # Direct OCR on scaled center
        try:
            t = pytesseract.image_to_string(gray_center,
                                            config="--oem 3 --psm 6").strip()
            if t and len(t) > 10:
                texts.append(t)
        except Exception:
            pass

        # Inverted (for white-on-dark dialogs)
        try:
            inv = cv2.bitwise_not(gray_center)
            t = pytesseract.image_to_string(inv,
                                            config="--oem 3 --psm 6").strip()
            if t and len(t) > 10:
                texts.append(t)
        except Exception:
            pass

        # --- Strategy 2: Title-bar detection (teal/cyan colored) ---
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        cyan_mask = cv2.inRange(hsv, (75, 30, 100), (110, 255, 255))
        cyan_mask = cv2.morphologyEx(
            cyan_mask, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (20, 5)))
        contours_c, _ = cv2.findContours(
            cyan_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours_c:
            x, y, bw, bh = cv2.boundingRect(cnt)
            if bw < 150 or bh < 10 or bh > 60:
                continue
            # Extract title bar + dialog body below
            ry1 = max(0, y - 5)
            ry2 = min(h, y + bh * 6)
            rx1 = max(0, x - 5)
            rx2 = min(w, x + bw + 5)
            if (ry2 - ry1) < 50 or (rx2 - rx1) < 150:
                continue
            roi = img[ry1:ry2, rx1:rx2]
            roi_big = cv2.resize(roi, None, fx=2, fy=2,
                                 interpolation=cv2.INTER_LANCZOS4)
            gray_roi = cv2.cvtColor(roi_big, cv2.COLOR_BGR2GRAY)
            try:
                t = pytesseract.image_to_string(
                    gray_roi, config="--oem 3 --psm 6").strip()
                if t and len(t) > 10:
                    texts.append(t)
            except Exception:
                pass

        return "\n".join(texts) if texts else ""

    @staticmethod
    def _compute_arc_coverage(pts, cx, cy, n_bins=36):
        """Check how many degrees (%) the contour covers around the circle."""
        angles = np.arctan2(pts[:, 1] - cy, pts[:, 0] - cx)
        angles = np.mod(angles, 2 * np.pi)
        bins = (angles / (2 * np.pi) * n_bins).astype(int)
        bins = np.clip(bins, 0, n_bins - 1)
        return len(np.unique(bins)) / n_bins

    @staticmethod
    def _detect_wafer_fail(frame, min_wafer_area=30000, fail_threshold=5.0):
        """Detect wafer map in screenshot and calculate fail % from red die clusters.
        Returns dict with wafer info or None if no wafer map found.

        Algorithm (aligned with standalone 1_extract_good_bad.py):
          1. HSV color seg → green dies mask (small morphology to keep shapes separate)
          2. Find contours → filter by area, circularity (≥0.55), arc coverage (≥0.60)
          3. Verify green fill ratio inside contour
          4. Then count red pixels inside the wafer ROI → fail %, clusters
        """
        H, W = frame.shape[:2]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # Green dies (pass) — same HSV range as standalone
        green = cv2.inRange(hsv, (35, 50, 50), (85, 255, 255))
        # Red dies (fail)
        red1 = cv2.inRange(hsv, (0, 50, 50), (12, 255, 255))
        red2 = cv2.inRange(hsv, (160, 50, 50), (180, 255, 255))
        red = cv2.bitwise_or(red1, red2)

        # ── Step 1: Find wafer shape from GREEN mask only ──
        # Use small kernel like standalone (5x5 ellipse, close 2x, open 1x)
        # This prevents merging separate UI elements (bar charts, buttons)
        kern = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        clean = cv2.morphologyEx(green, cv2.MORPH_CLOSE, kern, iterations=2)
        clean = cv2.morphologyEx(clean, cv2.MORPH_OPEN, kern, iterations=1)

        # Also create a combined dies mask (green+red) with light morphology
        # for verifying die density later
        dies = cv2.bitwise_or(green, red)
        dies_clean = cv2.morphologyEx(dies, cv2.MORPH_CLOSE, kern, iterations=2)
        dies_clean = cv2.morphologyEx(dies_clean, cv2.MORPH_OPEN, kern, iterations=1)

        contours, _ = cv2.findContours(clean, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_NONE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)

        min_area_ratio = 0.003  # minimum contour area relative to image
        min_area_abs = max(min_wafer_area, H * W * min_area_ratio)

        for c in contours[:10]:
            area = cv2.contourArea(c)
            if area < min_area_abs:
                continue

            x, y, w, h = cv2.boundingRect(c)
            cx_rect = x + w // 2

            # ── Shape check 1: circularity ≥ 0.55 (standalone uses 0.65) ──
            perimeter = cv2.arcLength(c, True)
            if perimeter < 1:
                continue
            circularity = (4 * np.pi * area) / (perimeter * perimeter)
            if circularity < 0.55:
                continue

            # ── Shape check 2: aspect ratio 0.7 ~ 1.4 ──
            aspect = w / h if h > 0 else 0
            if aspect < 0.7 or aspect > 1.4:
                continue

            # ── Shape check 3: arc coverage ≥ 0.60 (standalone uses 0.75) ──
            pts = c.reshape(-1, 2).astype(np.float64)
            if len(pts) < 20:
                continue
            cnt_cx = x + w / 2.0
            cnt_cy = y + h / 2.0
            arc_cov = RealtimeMonitor._compute_arc_coverage(pts, cnt_cx, cnt_cy)
            if arc_cov < 0.60:
                continue

            # ── Shape check 4: green fill ratio inside contour ──
            mask = np.zeros((H, W), dtype=np.uint8)
            cv2.drawContours(mask, [c], -1, 255, -1)
            total_in_mask = cv2.countNonZero(mask)
            green_in = cv2.countNonZero(cv2.bitwise_and(green, mask))
            green_fill = green_in / total_in_mask if total_in_mask > 0 else 0
            if green_fill < 0.25:
                # Not enough green density → not a wafer (UI panel / bar chart)
                continue

            # ── Passed all shape checks → count red dies ──
            gpx = green_in
            rpx = cv2.countNonZero(cv2.bitwise_and(red, mask))
            total = gpx + rpx

            if cx_rect > W * 0.5 and gpx > 3000 and rpx > 200:
                fail_pct = rpx / total * 100 if total else 0

                # Count fail clusters
                red_in = cv2.bitwise_and(red, mask)
                red_closed = cv2.morphologyEx(
                    red_in, cv2.MORPH_CLOSE,
                    np.ones((11, 11), np.uint8), iterations=5)
                rcs, _ = cv2.findContours(red_closed, cv2.RETR_EXTERNAL,
                                          cv2.CHAIN_APPROX_SIMPLE)
                big_clusters = [rc for rc in rcs if cv2.contourArea(rc) > 200]

                return {
                    "wafer_bbox": (x, y, w, h),
                    "green_px": gpx,
                    "red_px": rpx,
                    "fail_pct": round(fail_pct, 1),
                    "cluster_count": len(big_clusters),
                    "is_fail": fail_pct >= fail_threshold,
                }
        return None

    # ─── Hot-reload error_db ───
    def reload_error_db(self):
        """Reload error codes from PostgreSQL into the OCR module (called after add/delete error)."""
        simple = _get_simple()
        simple.ERROR_DB = simple.load_error_db()
        print(f"[Monitor] Reloaded ERROR_DB: {len(simple.ERROR_DB)} codes")

    # ─── Remove error from active tracking ───
    def remove_error(self, error_id):
        """Remove an error from active tracking (when deleted from error_db)."""
        removed = self.tracker.remove_error(error_id)
        if removed:
            self.tracker.save()
            print(f"[Monitor] Removed {error_id} from active tracking: {removed}")
        return removed

    # ─── Status ───
    def get_status(self):
        """Return current monitor status."""
        stats = self.tracker.stats()
        status = {
            "running": self.running,
            "capture_interval": self.capture_interval,
            "miss_threshold": self.miss_threshold,
            "cycle_count": self.cycle_count,
            "started_at": self.started_at,
            "last_cycle_at": self.last_cycle_at,
            "last_cycle_duration": self.last_cycle_duration,
            "last_cycle_results": self.last_cycle_results,
            "machines": list(self.machines.keys()),
            "video_test_mode": self._video_mode,
            **stats,
        }
        # Add video source progress info
        if self._video_mode and self._video_sources:
            sources_info = {}
            for name, src in self._video_sources.items():
                if src["type"] == "video":
                    sources_info[name] = {
                        "type": "video",
                        "path": os.path.basename(src["path"]),
                        "frame": src.get("frame_index", 0),
                        "total": src.get("total_frames", 0),
                    }
                elif src["type"] == "folder":
                    sources_info[name] = {
                        "type": "folder",
                        "path": os.path.basename(src["path"]),
                        "index": src.get("index", 0),
                        "total": len(src.get("files", [])),
                    }
            status["video_sources"] = sources_info
        return status

    def get_active_errors(self):
        """Return list of OPEN error events (errors only, no fails)."""
        return self.tracker.get_active()

    def get_active_fails(self):
        """Return list of OPEN fail events (WAFER_FAIL_CLUSTER, YIELD_VIOLATION)."""
        if self.fail_tracker:
            return self.fail_tracker.get_active()
        return []

    def get_error_history(self, limit=100):
        """Return list of CLOSED error events (most recent first)."""
        return self.tracker.get_history(limit)

    def get_fail_history(self, limit=100):
        """Return list of CLOSED fail events (most recent first)."""
        if self.fail_tracker:
            return self.fail_tracker.get_history(limit)
        return []


# =====================================================================
# Singleton monitor instance (used by Flask app)
# =====================================================================
_monitor_instance = None
_monitor_lock = threading.Lock()


def get_monitor(capture_interval=30, miss_threshold=3):
    """Get or create the singleton monitor instance."""
    global _monitor_instance
    with _monitor_lock:
        if _monitor_instance is None:
            _monitor_instance = RealtimeMonitor(
                capture_interval=capture_interval,
                miss_threshold=miss_threshold,
            )
        return _monitor_instance


# =====================================================================
# CLI — Run standalone for testing
# =====================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  Real-Time Error Monitor (Standalone)")
    print("=" * 60)

    monitor = get_monitor(capture_interval=30, miss_threshold=3)
    machines = load_machines_config()
    print(f"  Machines: {list(machines.keys())}")
    print(f"  Interval: {monitor.capture_interval}s")
    print(f"  Miss threshold: {monitor.miss_threshold}")
    print(f"  Press Ctrl+C to stop\n")

    monitor.start()

    try:
        while monitor.running:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Monitor] Stopping...")
        monitor.stop()
        time.sleep(1)
        print("[Monitor] Done.")
