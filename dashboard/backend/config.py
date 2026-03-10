"""
Detect_Na Fleet Monitor - Backend Configuration
"""
import os
import json
import glob

# Base paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(BASE_DIR)  # Detect_Na root

# Data paths
DATA_MERGED = os.path.join(PROJECT_DIR, "data_merged")
DATA_ERROR = os.path.join(PROJECT_DIR, "data_error")
DATA_FULL_GREEN = os.path.join(PROJECT_DIR, "data_full_green")
ROI_RESULTS = os.path.join(PROJECT_DIR, "roi_results")

# Pipeline result JSON (written by pipeline.py)
PIPELINE_RESULT_JSON = os.path.join(PROJECT_DIR, "latest_pipeline_result.json")

# Error analysis paths


def get_latest_error_analysis():
    """Find the error_analysis_simple folder.
    First check fixed name, then fallback to timestamped pattern.
    """
    # 1) Fixed folder name (no timestamp)
    fixed = os.path.join(PROJECT_DIR, "error_analysis_simple")
    if os.path.isdir(fixed):
        return fixed
    # 2) Fallback: timestamped folders (legacy)
    pattern = os.path.join(PROJECT_DIR, "error_analysis_simple_*")
    dirs = [d for d in glob.glob(pattern) if os.path.isdir(d)]
    if not dirs:
        return None
    dirs.sort()
    return dirs[-1]


ERROR_ANALYSIS = get_latest_error_analysis()


def get_latest_wafer_analysis():
    """Find the wafer_analysis folder.
    First check fixed name, then fallback to timestamped pattern.
    """
    # 1) Fixed folder name (no timestamp)
    fixed = os.path.join(PROJECT_DIR, "wafer_analysis")
    if os.path.isdir(fixed):
        return fixed
    # 2) Fallback: timestamped folders (legacy)
    pattern = os.path.join(PROJECT_DIR, "wafer_analysis_*")
    dirs = [d for d in glob.glob(pattern) if os.path.isdir(d)]
    if not dirs:
        return None
    dirs.sort()
    return dirs[-1]


WAFER_ANALYSIS = get_latest_wafer_analysis() or os.path.join(PROJECT_DIR, "wafer_analysis")

# Machine config is loaded from PostgreSQL via db_helper

# Server config
HOST = "0.0.0.0"
PORT = 5000
DEBUG = True

# Auto-refresh interval (seconds)  
REFRESH_INTERVAL = 30
