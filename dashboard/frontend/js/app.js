/**
 * Manufacturing Status Dashboard
 * 3-Column Layout: Machine Errors | Yield Issues | Search/Filters
 */

const API_BASE = window.location.origin;

// ─────────────────────────────────────────
// State
// ─────────────────────────────────────────
let machineData = null;
let errorAnalysisData = null;
let waferFailData = null;
let activeErrorsData = [];  // real-time active errors from monitor
let activeFailsData = [];   // real-time active fails (WAFER_FAIL_CLUSTER, YIELD_VIOLATION)
let failCardItems = [];  // stored for click handling
let waferFailItems = []; // stored for wafer fail Investigate clicks
let refreshTimer = null;
let monitorPollingTimer = null;
let monitorRunning = false;
let currentView = "severity"; // severity | machines
let currentFilter = "all";   // all | critical | warning | ok
let selectedMachines = new Set();
let searchQuery = "";
let mgvFilter = "all"; // all | connected | unknown | disconnected

// ─────────────────────────────────────────
// SVG Icons
// ─────────────────────────────────────────
const ICONS = {
    check: '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z"/></svg>',
    search: '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M11.5 7a4.5 4.5 0 11-9 0 4.5 4.5 0 019 0zm-.82 4.74a6 6 0 111.06-1.06l3.04 3.04a.75.75 0 11-1.06 1.06l-3.04-3.04z"/></svg>',
    trash: '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M6.5 1.75a.25.25 0 01.25-.25h2.5a.25.25 0 01.25.25V3h-3V1.75zm4.5 0V3h2.25a.75.75 0 010 1.5H2.75a.75.75 0 010-1.5H5V1.75C5 .784 5.784 0 6.75 0h2.5C10.216 0 11 .784 11 1.75zM4.496 6.675a.75.75 0 10-1.492.15l.66 6.6A1.75 1.75 0 005.405 15h5.19a1.75 1.75 0 001.741-1.575l.66-6.6a.75.75 0 00-1.492-.15l-.66 6.6a.25.25 0 01-.249.225h-5.19a.25.25 0 01-.249-.225l-.66-6.6z"/></svg>',
    wafer: '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>',
};

// ─────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────
function getSeverityClass(status) {
    switch (status) {
        case "CRITICAL":
        case "ALARM":
        case "YIELD_FAIL":
        case "SITE_FAIL":
            return "critical";
        case "WARNING":
            return "warning";
        default:
            return "ok";
    }
}

function getSeverityLabel(status) {
    switch (status) {
        case "CRITICAL": return "CRITICAL";
        case "ALARM": return "ALARM";
        case "YIELD_FAIL": return "YIELD FAIL";
        case "SITE_FAIL": return "SITE FAIL";
        case "WARNING": return "WARNING";
        default: return "OK";
    }
}

function matchesFilter(machine) {
    const sev = getSeverityClass(machine.status);
    if (currentFilter !== "all" && sev !== currentFilter) return false;
    if (selectedMachines.size > 0 && !selectedMachines.has(machine.id)) return false;
    if (searchQuery) {
        const q = searchQuery.toLowerCase();
        const searchFields = [
            machine.name, machine.id, machine.ip,
            machine.description, machine.statusLabel,
            machine.yieldNote
        ].filter(Boolean).join(" ").toLowerCase();
        if (!searchFields.includes(q)) return false;
    }
    return true;
}

// ─────────────────────────────────────────
// Data Fetching
// ─────────────────────────────────────────
async function fetchMachines() {
    try {
        const resp = await fetch(`${API_BASE}/api/machines`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        machineData = await resp.json();
        render();
        return machineData;
    } catch (err) {
        console.error("Failed to fetch machines:", err);
        document.getElementById("errorCriticalList").innerHTML =
            `<div class="loading">Failed to connect to backend<br><small>${err.message}</small></div>`;
    }
}

async function fetchErrorAnalysis() {
    try {
        const resp = await fetch(`${API_BASE}/api/error-analysis`);
        if (resp.ok) {
            errorAnalysisData = await resp.json();
            renderErrorSummary();
        }
    } catch (e) { /* ignore */ }
}

async function fetchWaferFails() {
    try {
        const resp = await fetch(`${API_BASE}/api/wafer-fails`);
        if (resp.ok) {
            waferFailData = await resp.json();
        }
    } catch (e) { /* ignore */ }
}

// ─────────────────────────────────────────
// Real-Time Monitor
// ─────────────────────────────────────────

async function fetchActiveErrors() {
    try {
        const resp = await fetch(`${API_BASE}/api/active-errors`);
        if (resp.ok) {
            activeErrorsData = await resp.json();
            // Re-render fail cards with real-time data
            if (machineData) render();
        }
    } catch (e) { /* ignore */ }
}

async function fetchActiveFails() {
    try {
        const resp = await fetch(`${API_BASE}/api/active-fails`);
        if (resp.ok) {
            activeFailsData = await resp.json();
            // Re-render Machine Fail Status with real-time fail data
            if (machineData) render();
        }
    } catch (e) { /* ignore */ }
}

async function fetchMonitorStatus() {
    try {
        const resp = await fetch(`${API_BASE}/api/monitor/status`);
        if (resp.ok) {
            const status = await resp.json();
            monitorRunning = status.running;
            updateMonitorUI(status);
        }
    } catch (e) { /* ignore */ }
}

async function toggleMonitor() {
    const btn = document.getElementById("btnMonitorToggle");
    btn.disabled = true;

    try {
        if (monitorRunning) {
            const resp = await fetch(`${API_BASE}/api/monitor/stop`, { method: "POST" });
            if (resp.ok) {
                monitorRunning = false;
                stopMonitorPolling();
            }
        } else {
            const resp = await fetch(`${API_BASE}/api/monitor/start`, { method: "POST" });
            if (resp.ok) {
                monitorRunning = true;
                startMonitorPolling();
            }
        }
    } catch (e) {
        console.error("Monitor toggle error:", e);
    }

    btn.disabled = false;
    updateMonitorUI({ running: monitorRunning, video_test_mode: false });
}

function startMonitorPolling() {
    if (monitorPollingTimer) clearInterval(monitorPollingTimer);
    // Poll active errors + fails every 5 seconds for responsive live updates
    monitorPollingTimer = setInterval(() => {
        fetchActiveErrors();
        fetchActiveFails();
        fetchMonitorStatus();
    }, 5000);
    // Fetch immediately
    fetchActiveErrors();
    fetchActiveFails();
    fetchMonitorStatus();
}

function stopMonitorPolling() {
    if (monitorPollingTimer) {
        clearInterval(monitorPollingTimer);
        monitorPollingTimer = null;
    }
    activeErrorsData = [];
    activeFailsData = [];
    if (machineData) render();
}

function updateMonitorUI(status) {
    const btn = document.getElementById("btnMonitorToggle");
    const dot = document.getElementById("monitorDot");
    const label = document.getElementById("monitorBtnLabel");
    const videoBtn = document.getElementById("btnVideoTest");
    const videoLabel = document.getElementById("videoTestLabel");

    if (status.running) {
        btn.classList.add("active");
        dot.className = "monitor-dot running";
        label.textContent = "Stop Monitor";
        // Update video test button state
        if (status.video_test_mode) {
            if (videoBtn) { videoBtn.disabled = false; videoBtn.style.background = "#e53e3e"; }
            if (videoLabel) videoLabel.textContent = "⏹ Stop Video";
        }
    } else {
        btn.classList.remove("active");
        dot.className = "monitor-dot stopped";
        dot.style.background = "";
        dot.style.boxShadow = "";
        label.textContent = "Start Monitor";
        // Reset video test button
        if (videoBtn) { videoBtn.disabled = false; videoBtn.style.background = ""; }
        if (videoLabel) videoLabel.textContent = "Video Test";
    }
}

/**
 * Start monitor in Video Test mode.
 * Uses test.mp4 file, cycling frames every 5 seconds.
 */
async function startVideoTest() {
    const videoBtn = document.getElementById("btnVideoTest");
    const videoLabel = document.getElementById("videoTestLabel");

    // If monitor is running, stop it first
    if (monitorRunning) {
        await fetch(`${API_BASE}/api/monitor/stop`, { method: "POST" });
        monitorRunning = false;
        stopMonitorPolling();
        updateMonitorUI({ running: false });
        // Wait a moment for cleanup
        await new Promise(r => setTimeout(r, 500));
    }

    videoBtn.disabled = true;
    videoLabel.textContent = "Starting...";

    try {
        const resp = await fetch(`${API_BASE}/api/monitor/start`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                mode: "video_test",
                source: "test.mp4",
                interval: 5,
                loop: true,
                miss_threshold: 1,
            }),
        });
        const result = await resp.json();
        if (resp.ok) {
            monitorRunning = true;
            startMonitorPolling();
            updateMonitorUI({ running: true, video_test_mode: true });
        } else {
            alert(result.error || "Failed to start video test");
            videoBtn.disabled = false;
            videoLabel.textContent = "Video Test";
        }
    } catch (e) {
        console.error("Video test error:", e);
        videoBtn.disabled = false;
        videoLabel.textContent = "Video Test";
    }
}

function formatDuration(startIso, endIso) {
    if (!startIso) return "";
    const start = new Date(startIso);
    const end = endIso ? new Date(endIso) : new Date();
    const diff = Math.max(0, Math.floor((end - start) / 1000));
    if (diff < 60) return `${diff}s`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ${diff % 60}s`;
    const h = Math.floor(diff / 3600);
    const m = Math.floor((diff % 3600) / 60);
    return `${h}h ${m}m`;
}

// ─────────────────────────────────────────
// Quick Scan (Step 3 only — re-detect errors from data_error/)
// ─────────────────────────────────────────
let scanPollingTimer = null;

async function triggerQuickScan() {
    const btn = document.getElementById("btnQuickScan");
    const label = document.getElementById("scanBtnLabel");

    btn.classList.add("scanning");
    btn.classList.remove("done");
    label.textContent = "Scanning...";

    try {
        const resp = await fetch(`${API_BASE}/api/quick-scan`, { method: "POST" });
        if (resp.ok) {
            // Start polling for progress
            startScanPolling();
        } else {
            const err = await resp.json();
            label.textContent = err.error || "Scan Failed";
            btn.classList.remove("scanning");
            setTimeout(() => { label.textContent = "Scan Now"; }, 3000);
        }
    } catch (e) {
        console.error("Quick scan error:", e);
        label.textContent = "Error";
        btn.classList.remove("scanning");
        setTimeout(() => { label.textContent = "Scan Now"; }, 3000);
    }
}

function startScanPolling() {
    if (scanPollingTimer) clearInterval(scanPollingTimer);
    scanPollingTimer = setInterval(async () => {
        try {
            const resp = await fetch(`${API_BASE}/api/quick-scan/status`);
            if (!resp.ok) return;
            const status = await resp.json();

            const btn = document.getElementById("btnQuickScan");
            const label = document.getElementById("scanBtnLabel");

            if (status.running) {
                const pct = status.total > 0 ? Math.round((status.progress / status.total) * 100) : 0;
                label.textContent = `Scanning ${pct}%`;
            } else {
                // Scan finished
                clearInterval(scanPollingTimer);
                scanPollingTimer = null;
                btn.classList.remove("scanning");

                if (status.error) {
                    label.textContent = "Scan Failed";
                    setTimeout(() => { label.textContent = "Scan Now"; }, 3000);
                } else {
                    btn.classList.add("done");
                    const r = status.result || {};
                    label.textContent = `Done! ${r.detected || 0} errors`;

                    // Refresh error analysis data to show new results
                    await fetchErrorAnalysis();
                    render();

                    setTimeout(() => {
                        btn.classList.remove("done");
                        label.textContent = "Scan Now";
                    }, 5000);
                }
            }
        } catch (e) { /* ignore */ }
    }, 2000);
}

// ─────────────────────────────────────────
// Main Render
// ─────────────────────────────────────────
function render() {
    if (!machineData || !machineData.machines) return;

    const machines = machineData.machines.filter(matchesFilter);

    updateHeader(machineData);
    updateSummaryBar(machineData);

    if (currentView === "severity") {
        document.querySelector(".main-content").style.display = "grid";
        document.getElementById("machineGridView").style.display = "none";
        renderSeverityView(machines);
    } else {
        document.querySelector(".main-content").style.display = "none";
        document.getElementById("machineGridView").style.display = "block";
        renderMachineGridView(machines);
    }

    renderSourceFilters(machineData.machines);
}

// ─────────────────────────────────────────
// Header & Summary
// ─────────────────────────────────────────
function updateHeader(data) {
    let ok = 0, warn = 0, crit = 0;
    (data.machines || []).forEach(m => {
        const sev = getSeverityClass(m.status);
        if (sev === "ok") ok++;
        else if (sev === "warning") warn++;
        else crit++;
    });

    document.getElementById("pillOk").textContent = ok;
    document.getElementById("pillWarn").textContent = warn;
    document.getElementById("pillCrit").textContent = crit;
    document.getElementById("lastUpdate").textContent = `Updated: ${data.timestamp || "--"}`;
}

function updateSummaryBar(data) {
    let machCrit = 0, machWarn = 0;
    let yieldCrit = 0, yieldWarn = 0;

    (data.machines || []).forEach(m => {
        const sev = getSeverityClass(m.status);
        if (sev === "critical") {
            if (m.status === "YIELD_FAIL") yieldCrit++;
            else machCrit++;
        } else if (sev === "warning") {
            if (m.yield && m.yield < 95) yieldWarn++;
            else machWarn++;
        }
    });

    // Also count from error analysis data
    if (errorAnalysisData && errorAnalysisData.results) {
        const highCount = errorAnalysisData.results.filter(r => r.severity === "high").length;
        const medCount = errorAnalysisData.results.filter(r => r.severity === "medium").length;
        if (highCount > 0) machCrit = Math.max(machCrit, highCount);
        if (medCount > 0) machWarn = Math.max(machWarn, medCount);
    }

    document.getElementById("sumMachCrit").textContent = `${machCrit} Critical`;
    document.getElementById("sumMachWarn").textContent = `${machWarn} Warning`;
    document.getElementById("sumYieldCrit").textContent = `${yieldCrit} Critical`;
    document.getElementById("sumYieldWarn").textContent = `${yieldWarn} Warning`;
}

// ─────────────────────────────────────────
// Severity View (3-column: Sidebar | Fail Cards | Error Feed)
// ─────────────────────────────────────────
function renderSeverityView(machines) {
    // ── Column 2: Machine Fail Cards ──
    renderFailCards(machines);

    // ── Column 3: Live Machine Error Feed ──
    renderErrorFeed();

    renderSourceFilters(machineData.machines);
}

/**
 * Render fail cards in Column 2 as a 2-column grid.
 * Shows error/fail info from real-time monitor (priority) or batch error analysis.
 */
function renderFailCards(machines) {
    const grid = document.getElementById("failCardsGrid");
    grid.innerHTML = "";

    // Collect all fail items
    const failItems = [];

    // ── Priority 1: Real-time active errors (when monitor is running) ──
    if (monitorRunning && activeErrorsData && activeErrorsData.length > 0) {
        activeErrorsData.forEach(event => {
            const severity = event.severity || "unknown";
            const sevClass = severity === "high" ? "critical" : severity === "medium" ? "warning" : "ok";
            const category = (event.category || "UNKNOWN").replace(/_/g, " ").toUpperCase();

            // Apply filters
            if (selectedMachines.size > 0) {
                const mName = event.machine_id || "";
                const match = Array.from(selectedMachines).some(id => {
                    const machine = (machineData && machineData.machines || []).find(m => m.id === id);
                    return machine && machine.name === mName;
                });
                if (!match) return;
            }
            if (searchQuery) {
                const q = searchQuery.toLowerCase();
                const fields = [
                    event.machine_id, event.error_id, event.category,
                    event.description, event.handler
                ].filter(Boolean).join(" ").toLowerCase();
                if (!fields.includes(q)) return;
            }

            failItems.push({
                machine: event.machine_name || event.machine_id || "Unknown",
                machineId: event.machine_id || "",
                category,
                errorCode: event.error_id,
                lotnumber: "",
                testmode: "",
                sevClass,
                severity: event.severity,
                description: event.description || "",
                handler: event.handler || "",
                method: event.method || "",
                // Real-time fields
                isLive: true,
                firstSeenAt: event.first_seen_at,
                lastSeenAt: event.last_seen_at,
                seenCount: event.seen_count || 0,
                evidenceImage: event.evidence_image || null,
            });
        });
    }

    // ── Priority 2: Batch error analysis data (fallback when monitor not running) ──
    if (failItems.length === 0 && errorAnalysisData && errorAnalysisData.results && errorAnalysisData.results.length > 0) {
        const results = errorAnalysisData.results;

        // Apply machine filter
        const filtered = results.filter(r => {
            if (selectedMachines.size > 0) {
                const mName = r.machine || "";
                const match = Array.from(selectedMachines).some(id => {
                    const machine = (machineData && machineData.machines || []).find(m => m.id === id);
                    return machine && machine.name === mName;
                });
                if (!match) return false;
            }
            if (searchQuery) {
                const q = searchQuery.toLowerCase();
                const fields = [
                    r.machine, r.category, r.filename,
                    ...(r.error_ids || []),
                    ...(r.codes || []),
                    ...((r.classifications || []).map(c => c.desc || ""))
                ].filter(Boolean).join(" ").toLowerCase();
                if (!fields.includes(q)) return false;
            }
            return true;
        });

        filtered.forEach(item => {
            const cls = (item.classifications && item.classifications[0]) || {};
            const category = (cls.cat || item.category || "ALIGNMENT FAIL").replace(/_/g, " ").toUpperCase();
            const errorId = cls.error_id || (item.codes && item.codes[0]) || "E-503";
            const machineName = item.machine || "Unknown";
            const severity = item.severity || "high";
            const sevClass = severity === "high" ? "critical" : severity === "medium" ? "warning" : "ok";

            // Find machine ID
            let machineId = "";
            if (machineData && machineData.machines) {
                const found = machineData.machines.find(m => m.name === machineName);
                if (found) machineId = found.id;
            }

            failItems.push({
                machine: machineName,
                machineId,
                category,
                errorCode: errorId,
                lotnumber: item.lotnumber || item.lot || "3FTBH497",
                testmode: item.testmode || item.test_mode || "S21P",
                sevClass,
                isLive: false,
                // Keep original error analysis data for detail view
                filename: item.filename,
                classifications: item.classifications,
                severity: item.severity,
                errorIds: item.error_ids,
                ocrText: item.ocr_text,
            });
        });
    }

    // 3) Fallback: from machine data (machines with critical/warning status)
    if (failItems.length === 0 && machineData && machineData.machines) {
        machines.filter(m => {
            const sev = getSeverityClass(m.status);
            return sev === "critical" || sev === "warning";
        }).forEach(m => {
            const sev = getSeverityClass(m.status);
            failItems.push({
                machine: m.name,
                machineId: m.id,
                category: getSeverityLabel(m.status),
                errorCode: "E-503",
                lotnumber: "3FTBH497",
                testmode: "S21P",
                sevClass: sev,
                isLive: false,
            });
        });
    }

    // Update count
    document.getElementById("failCount").textContent = failItems.length;

    if (failItems.length === 0) {
        grid.innerHTML = '<div class="loading" style="grid-column:1/-1;">No failures detected</div>';
        return;
    }

    // Sort: critical first (use ?? to avoid falsy-zero bug with ||)
    const sevOrder = { critical: 0, warning: 1, ok: 2 };
    failItems.sort((a, b) => (sevOrder[a.sevClass] ?? 2) - (sevOrder[b.sevClass] ?? 2));

    // Store globally for click handling
    failCardItems = failItems;

    failItems.forEach((item, idx) => {
        const card = document.createElement("div");
        card.className = `fail-card ${item.sevClass === "critical" ? "" : item.sevClass}`;

        // Badge icon: X-circle for critical, warning triangle for warning, check-circle for ok
        const badgeIcon = item.sevClass === "ok"
            ? '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M8 16A8 8 0 108 0a8 8 0 000 16zm3.78-9.72a.75.75 0 00-1.06-1.06L7 8.94 5.28 7.22a.75.75 0 00-1.06 1.06l2.25 2.25a.75.75 0 001.06 0l4.25-4.25z"/></svg>'
            : item.sevClass === "warning"
            ? '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M8.22 1.754a.25.25 0 00-.44 0L1.698 13.132a.25.25 0 00.22.368h12.164a.25.25 0 00.22-.368L8.22 1.754zm-1.763-.707c.659-1.234 2.427-1.234 3.086 0l6.082 11.378A1.75 1.75 0 0114.082 15H1.918a1.75 1.75 0 01-1.543-2.575L6.457 1.047zM9 11a1 1 0 11-2 0 1 1 0 012 0zm-.25-5.25a.75.75 0 00-1.5 0v2.5a.75.75 0 001.5 0v-2.5z"/></svg>'
            : '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M3.72 3.72a.75.75 0 011.06 0L8 6.94l3.22-3.22a.75.75 0 111.06 1.06L9.06 8l3.22 3.22a.75.75 0 11-1.06 1.06L8 9.06l-3.22 3.22a.75.75 0 01-1.06-1.06L6.94 8 3.72 4.78a.75.75 0 010-1.06z"/><path d="M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM0 8a8 8 0 1116 0A8 8 0 010 8z"/></svg>';

        // Live badge + duration for real-time events
        const liveBadgeHtml = item.isLive
            ? `<span class="live-badge"><span class="live-dot"></span>LIVE</span>`
            : '';
        const durationHtml = item.isLive && item.firstSeenAt
            ? `<div class="err-duration">Duration: ${formatDuration(item.firstSeenAt, item.lastSeenAt)} (seen ${item.seenCount}x)</div>`
            : '';
        const evidenceHtml = item.isLive && item.evidenceImage
            ? `<img class="evidence-thumb" src="${API_BASE}/api/evidence/${item.evidenceImage}" alt="Evidence" onerror="this.style.display='none'">`
            : '';

        // Details section differs for live vs batch
        let detailsHtml = '';
        if (item.isLive) {
            detailsHtml = `
                <div class="fc-detail-row">
                    <span class="fc-detail-label">Error Code:</span>
                    <span class="fc-detail-value">${item.errorCode}</span>
                </div>
                <div class="fc-detail-row">
                    <span class="fc-detail-label">Description:</span>
                    <span class="fc-detail-value">${item.description || '-'}</span>
                </div>
                <div class="fc-detail-row">
                    <span class="fc-detail-label">Handler:</span>
                    <span class="fc-detail-value">${item.handler || '-'}</span>
                </div>
            `;
        } else {
            detailsHtml = `
                <div class="fc-detail-row">
                    <span class="fc-detail-label">Error Code:</span>
                    <span class="fc-detail-value">${item.errorCode}</span>
                </div>
                <div class="fc-detail-row">
                    <span class="fc-detail-label">Lotnumber:</span>
                    <span class="fc-detail-value">${item.lotnumber}</span>
                </div>
                <div class="fc-detail-row">
                    <span class="fc-detail-label">Testmode:</span>
                    <span class="fc-detail-value">${item.testmode}</span>
                </div>
            `;
        }

        card.innerHTML = `
            <div class="fc-machine">${item.machine} ${liveBadgeHtml}</div>
            <div class="fc-badge">
                <span class="fc-badge-icon">${badgeIcon}</span>
                ${item.category}
            </div>
            ${durationHtml}
            <div class="fc-details">
                ${detailsHtml}
            </div>
            ${evidenceHtml}
            <button class="fc-link" onclick="openErrorDetail(${idx})">View Details &rarr;</button>
        `;

        grid.appendChild(card);
    });
}

/**
 * Render Machine Fail Status in Column 3.
 * Shows: 1) Live wafer/yield fails from monitor, 2) Batch wafer red cluster analysis.
 * Only fail-related data — errors (ALARM, O-codes) shown separately in Fail Cards.
 */
function renderErrorFeed() {
    const list = document.getElementById("errorFeedList");
    list.innerHTML = "";

    // ── Section 1: Live wafer/yield fails from monitor ──
    // Use dedicated activeFailsData from /api/active-fails (separate fail_events table)
    const liveFails = [];

    if (monitorRunning && activeFailsData && activeFailsData.length > 0) {
        activeFailsData.forEach(event => {
            // Apply machine filter
            if (selectedMachines.size > 0) {
                const mName = event.machine_id || "";
                const match = Array.from(selectedMachines).some(id => {
                    const machine = (machineData && machineData.machines || []).find(m => m.id === id);
                    return machine && machine.name === mName;
                });
                if (!match) return;
            }
            // Apply search filter
            if (searchQuery) {
                const q = searchQuery.toLowerCase();
                const fields = [
                    event.machine_name, event.machine_id, event.error_id || event.fail_id,
                    event.category, event.description
                ].filter(Boolean).join(" ").toLowerCase();
                if (!fields.includes(q)) return;
            }
            liveFails.push(event);
        });
    }

    // Store globally for click handler
    window._liveFailItems = liveFails;

    if (liveFails.length > 0) {
        liveFails.forEach((event, idx) => {
            const sevClass = event.severity === "high" ? "critical" : "warning";
            const machineName = event.machine_name || event.machine_id || "Unknown";
            const statusLabel = event.severity === "high" ? "SEVERE" : "HIGH";

            // Parse method field for fail%/clusters: "visual(fail=12.3%,clusters=2)" or "keyword(score=8)"
            let failPct = "";
            let clusterCount = "";
            const methodStr = event.method || "";
            const failMatch = methodStr.match(/fail=([\d.]+)%/);
            const clusterMatch = methodStr.match(/clusters=(\d+)/);
            if (failMatch) failPct = failMatch[1] + "%";
            if (clusterMatch) clusterCount = clusterMatch[1];

            // Build description line
            let descParts = [];
            descParts.push(event.error_id || event.fail_id);
            if (failPct) descParts.push(`Fail: ${failPct}`);
            if (clusterCount) descParts.push(`${clusterCount} clusters`);
            if (!failPct && !clusterCount) {
                descParts.push(event.description || event.category || "");
            }
            descParts.push(`seen ${event.seen_count || 0}x`);

            const row = document.createElement("div");
            row.className = `error-feed-row ${sevClass === "critical" ? "" : sevClass}`;

            row.innerHTML = `
                <div class="efr-icon">
                    <span class="live-dot" style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#e53e3e;animation:pulse 1.5s infinite;"></span>
                </div>
                <div class="efr-info">
                    <div class="efr-machine">${machineName} <span class="live-badge" style="font-size:10px;padding:1px 6px;"><span class="live-dot"></span>LIVE</span></div>
                    <div class="efr-desc">${descParts.join(' · ')}</div>
                </div>
                <span class="efr-badge">${statusLabel}</span>
                <span class="efr-time">${event.first_seen_at ? formatDuration(event.first_seen_at, event.last_seen_at) : ''}</span>
                <button class="efr-action" onclick="openLiveFailDetail(${idx})">Investigate &gt;</button>
            `;
            list.appendChild(row);
        });
    }

    // Header count = live fails only (real-time)
    document.getElementById("errorCount").textContent = liveFails.length;

    // ── Section 2: Batch wafer fail data (SEVERE only — exclude below-threshold) ──
    let fails = [];
    if (waferFailData && waferFailData.fails && waferFailData.fails.length > 0) {
        // Only show wafers that truly meet fail criteria (concern_level >= 2 = SEVERE)
        fails = waferFailData.fails.filter(f => f.concern_level >= 2);

        // Apply machine filter
        if (selectedMachines.size > 0) {
            fails = fails.filter(f => {
                const mName = f.machine || "";
                return Array.from(selectedMachines).some(id => {
                    const machine = (machineData && machineData.machines || []).find(m => m.id === id);
                    return machine && machine.name === mName;
                });
            });
        }
        // Apply search filter
        if (searchQuery) {
            const q = searchQuery.toLowerCase();
            fails = fails.filter(f => {
                const fields = [f.machine, f.status, f.location, f.filename].filter(Boolean).join(" ").toLowerCase();
                return fields.includes(q);
            });
        }
    }

    waferFailItems = fails;

    if (fails.length > 0) {
        // Separator with batch count
        const sep = document.createElement("div");
        sep.style.cssText = "padding:8px 16px;font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:1px;border-top:1px solid var(--border-color);margin-top:4px;display:flex;justify-content:space-between;align-items:center;";
        sep.innerHTML = `<span>Batch Wafer Analysis</span><span style="background:var(--bg-secondary);padding:2px 8px;border-radius:10px;font-weight:600;">${fails.length}</span>`;
        list.appendChild(sep);

        fails.forEach((item, idx) => {
            const sevClass = item.concern_level >= 2 ? "critical" : "warning";
            const statusLabel = item.status;
            let locationLabel = "Center";
            const loc = (item.location || "center").toLowerCase();
            if (loc === "center") locationLabel = "Center";
            else if (loc.includes("multiple")) locationLabel = "Multiple Edges";
            else if (loc.includes("top")) locationLabel = "Top Edge";
            else if (loc.includes("bottom")) locationLabel = "Bottom Edge";
            else if (loc.includes("left")) locationLabel = "Left Edge";
            else if (loc.includes("right")) locationLabel = "Right Edge";
            else if (loc.includes("edge")) locationLabel = "Edge";

            const clusterPct = item.largest_cluster_ratio.toFixed(1) + "%";
            const redPct = item.red_ratio.toFixed(1) + "%";

            const row = document.createElement("div");
            row.className = `error-feed-row ${sevClass === "critical" ? "" : sevClass}`;

            row.innerHTML = `
                <div class="efr-icon">
                    ${ICONS.wafer ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="4"/></svg>' : ''}
                </div>
                <div class="efr-info">
                    <div class="efr-machine">${item.machine}</div>
                    <div class="efr-desc">Cluster: ${clusterPct} · Red: ${redPct} · ${locationLabel}</div>
                </div>
                <span class="efr-badge">${statusLabel}</span>
                <span class="efr-time">${item.total_clusters} clusters</span>
                <button class="efr-action" onclick="openWaferDetail(${idx})">Investigate &gt;</button>
            `;
            list.appendChild(row);
        });
    }

    if (liveFails.length === 0 && fails.length === 0) {
        list.innerHTML = '<div class="loading" style="padding:30px 0;">No wafer fails detected</div>';
    }
}

/** Open detail modal for a live wafer/yield fail from Machine Fail Status */
function openLiveFailDetail(index) {
    const event = window._liveFailItems[index];
    if (!event) return;

    const overlay = document.getElementById("modalOverlay");
    const title = document.getElementById("modalTitle");
    const image = document.getElementById("modalImage");
    const info = document.getElementById("modalInfo");
    const gallery = document.getElementById("modalGallery");

    const machineName = event.machine_name || event.machine_id || "Unknown";
    const category = (event.category || "UNKNOWN").replace(/_/g, " ").toUpperCase();
    const sevClass = event.severity === "high" ? "critical" : "warning";
    const statusColor = sevClass === "critical" ? "var(--color-critical)" : "var(--color-warning)";
    const statusLabel = event.severity === "high" ? "SEVERE" : "HIGH";

    title.textContent = `Fail Detection — ${machineName}`;
    gallery.innerHTML = "";

    // Evidence image
    const evidenceUrl = event.evidence_image
        ? `${API_BASE}/api/evidence/${encodeURIComponent(event.evidence_image)}?t=${Date.now()}`
        : '';

    if (evidenceUrl) {
        image.src = evidenceUrl;
        image.style.display = "block";
    } else {
        image.src = "";
        image.style.display = "none";
    }

    // Parse method for fail%/clusters
    const methodStr = event.method || "";
    const failMatch = methodStr.match(/fail=([\d.]+)%/);
    const clusterMatch = methodStr.match(/clusters=(\d+)/);
    const failPct = failMatch ? failMatch[1] + "%" : "-";
    const clusterCount = clusterMatch ? clusterMatch[1] : "-";

    info.innerHTML = `
        <div class="info-row">
            <div><span class="info-label">Machine</span><br><span class="info-value">${machineName}</span></div>
            <div><span class="info-label">Status</span><br><span class="info-value" style="color:${statusColor};font-weight:700;">${statusLabel}</span></div>
            <div><span class="info-label">Error Code</span><br><span class="info-value">${event.error_id}</span></div>
            <div><span class="info-label">Severity</span><br><span class="info-value" style="color:${statusColor}">${(event.severity || "unknown").toUpperCase()}</span></div>
            <div><span class="info-label">Fail Ratio</span><br><span class="info-value">${failPct}</span></div>
            <div><span class="info-label">Clusters</span><br><span class="info-value">${clusterCount}</span></div>
        </div>
        <div style="margin-top:12px;padding:10px 14px;background:var(--bg-card);border-radius:8px;border-left:4px solid ${statusColor};">
            <div style="font-weight:600;color:var(--text-primary);margin-bottom:4px;">Live Fail Detail</div>
            <div style="font-size:13px;color:var(--text-secondary);">
                <div>${event.description || '-'}</div>
                <div style="margin-top:4px;">Seen: <strong>${event.seen_count || 0}x</strong></div>
                <div>First: ${event.first_seen_at ? new Date(event.first_seen_at).toLocaleString() : '-'}</div>
                <div>Last: ${event.last_seen_at ? new Date(event.last_seen_at).toLocaleString() : '-'}</div>
                <div>Detection: ${event.method || '-'}</div>
            </div>
        </div>
    `;

    if (evidenceUrl) {
        const thumb = document.createElement("img");
        thumb.src = evidenceUrl;
        thumb.alt = "Evidence";
        thumb.title = "Live Fail Evidence";
        thumb.classList.add("active");
        gallery.appendChild(thumb);
    }

    overlay.classList.add("active");
}

// ─────────────────────────────────────────
// Machine Grid View  (Connection Management)
// ─────────────────────────────────────────

// Track per-machine connection status: { machineId: "connected"|"disconnected"|"unknown"|"testing" }
let machineConnStatus = {};

function renderMachineGridView(machines) {
    // Update topbar count
    const countEl = document.getElementById("mgvCount");
    if (countEl) countEl.textContent = `${machines.length} machine${machines.length !== 1 ? 's' : ''}`;

    // Apply mgv connection-status filter
    const filtered = mgvFilter === "all" ? machines : machines.filter(m => {
        const connSt = machineConnStatus[m.id] || "unknown";
        return connSt === mgvFilter;
    });

    const grid = document.getElementById("machineGrid");
    grid.innerHTML = "";

    if (filtered.length === 0) {
        grid.innerHTML = '<div class="loading">No machines found</div>';
        return;
    }

    filtered.forEach(m => {
        const card = document.createElement("div");
        card.className = "machine-card";
        card.id = `mc-${m.id}`;

        const connSt = machineConnStatus[m.id] || "unknown";
        const connLabel = connSt === "connected" ? "Connected"
            : connSt === "disconnected" ? "Disconnected"
            : connSt === "testing" ? "Testing..."
            : "Unknown";

        const portLabel = m.vnc_port || 5900;
        const ipDisplay = m.ip ? `${m.ip}:${portLabel}` : "—";
        const ipRaw = m.ip || "—";
        const passTag = m.has_password
            ? `<span class="mc-password-tag">🔒 Secured</span>`
            : `<span class="mc-password-tag no-pass">🔓 No Auth</span>`;

        card.innerHTML = `
            <div class="mc-card-top-bar ${connSt}"></div>
            <div class="mc-card-header">
                <div class="mc-card-icon">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>
                </div>
                <div class="mc-card-title">
                    <span class="mc-name">${m.name}</span>
                    <span class="mc-status-badge ${connSt}">
                        <span class="mc-status-dot ${connSt}"></span>
                        ${connLabel}
                    </span>
                </div>
            </div>
            <div class="mc-card-body">
                <div class="mc-info-row">
                    <span class="mc-info-label">IP Address</span>
                    <span class="mc-info-value" title="${ipRaw}">${ipRaw}</span>
                </div>
                <div class="mc-info-row">
                    <span class="mc-info-label">Machine ID</span>
                    <span class="mc-info-value" title="${m.id}">${m.id}</span>
                </div>
                <div class="mc-info-row">
                    <span class="mc-info-label">VNC Port</span>
                    <span class="mc-info-value" title="${portLabel}">${portLabel}</span>
                </div>
                <div class="mc-info-row">
                    <span class="mc-info-label">VNC Folder</span>
                    <span class="mc-info-value" title="${m.vnc_id || '—'}">${m.vnc_id || '—'}</span>
                </div>
                <div class="mc-info-row">
                    <span class="mc-info-label">Auth</span>
                    ${passTag}
                </div>
            </div>
            <div class="mc-card-secondary">
                <span>IP: ${ipRaw}</span>
                <span>ID: ${m.id}</span>
            </div>
            <div class="mc-card-footer">
                <button class="mc-btn-test" onclick="testVncConnection('${m.id}')" title="Test VNC Connection">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 12.55a11 11 0 0114.08 0"/><path d="M1.42 9a16 16 0 0121.16 0"/><path d="M8.53 16.11a6 6 0 016.95 0"/><circle cx="12" cy="20" r="1"/></svg>
                    Test Connection
                </button>
                <button class="mc-btn-edit" onclick="openEditMachine('${m.id}')" title="Edit Properties">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                    Edit
                </button>
                <button class="mc-btn-delete" onclick="confirmDelete('${m.id}', '${m.name.replace(/'/g, "\\'")}')" title="Remove machine">
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M6.5 1.75a.25.25 0 01.25-.25h2.5a.25.25 0 01.25.25V3h-3V1.75zm4.5 0V3h2.25a.75.75 0 010 1.5H2.75a.75.75 0 010-1.5H5V1.75C5 .784 5.784 0 6.75 0h2.5C10.216 0 11 .784 11 1.75zM4.496 6.675a.75.75 0 10-1.492.15l.66 6.6A1.75 1.75 0 005.405 15h5.19a1.75 1.75 0 001.741-1.575l.66-6.6a.75.75 0 00-1.492-.15l-.66 6.6a.25.25 0 01-.249.225h-5.19a.25.25 0 01-.249-.225l-.66-6.6z"/></svg>
                </button>
            </div>
        `;

        grid.appendChild(card);
    });
}

// ─────────────────────────────────────────
// Source Filters
// ─────────────────────────────────────────

/**
 * Toggle a machine filter checkbox from anywhere (fail cards, error feed, etc.)
 */
function toggleMachineFilter(machineId) {
    if (!machineId) return;
    if (selectedMachines.has(machineId)) {
        selectedMachines.delete(machineId);
    } else {
        selectedMachines.add(machineId);
    }
    // Update checkbox UI
    const cb = document.getElementById(`filter_${machineId}`);
    if (cb) cb.checked = selectedMachines.has(machineId);
    render();
}

function renderSourceFilters(machines) {
    const container = document.getElementById("sourceFilters");
    if (!machines) return;

    // Only render once, not on every refresh
    if (container.children.length === machines.length) return;

    container.innerHTML = "";

    machines.forEach(m => {
        const sev = getSeverityClass(m.status);
        const item = document.createElement("div");
        item.className = "source-filter-item";
        item.innerHTML = `
            <input type="checkbox" id="filter_${m.id}" value="${m.id}" 
                   ${selectedMachines.has(m.id) ? "checked" : ""}>
            <label for="filter_${m.id}">${m.name}</label>
            <span class="filter-status dot ${sev}"></span>
        `;
        item.querySelector("input").addEventListener("change", (e) => {
            if (e.target.checked) {
                selectedMachines.add(m.id);
            } else {
                selectedMachines.delete(m.id);
            }
            render();
        });
        container.appendChild(item);
    });
}

// ─────────────────────────────────────────
// Error Summary (sidebar)
// ─────────────────────────────────────────
function renderErrorSummary() {
    if (!errorAnalysisData || !errorAnalysisData.summary) return;

    const section = document.getElementById("errorSummarySection");
    const container = document.getElementById("errorSummary");
    section.style.display = "block";
    container.innerHTML = "";

    const cats = errorAnalysisData.summary.categories || {};
    const sevs = errorAnalysisData.summary.severities || {};

    // Show severity counts
    for (const [sev, count] of Object.entries(sevs)) {
        const item = document.createElement("div");
        item.className = "error-summary-item";
        item.innerHTML = `
            <span class="es-cat">${sev.charAt(0).toUpperCase() + sev.slice(1)} Severity</span>
            <span class="es-count ${sev}">${count}</span>
        `;
        container.appendChild(item);
    }

    // Show category counts
    for (const [cat, count] of Object.entries(cats)) {
        const item = document.createElement("div");
        item.className = "error-summary-item";
        item.innerHTML = `
            <span class="es-cat">${cat.replace(/_/g, " ")}</span>
            <span class="es-count medium">${count}</span>
        `;
        container.appendChild(item);
    }
}

// ─────────────────────────────────────────
// ─────────────────────────────────────────
// Error Detail Modal (from 3_simple.py error analysis)
// ─────────────────────────────────────────
function openErrorDetail(index) {
    const item = failCardItems[index];
    if (!item) return;

    const overlay = document.getElementById("modalOverlay");
    const title = document.getElementById("modalTitle");
    const image = document.getElementById("modalImage");
    const info = document.getElementById("modalInfo");
    const gallery = document.getElementById("modalGallery");

    title.textContent = `${item.machine} — ${item.category}`;
    gallery.innerHTML = "";

    // ── Live monitor event: use /api/evidence/ path ──
    if (item.isLive) {
        const evidenceUrl = item.evidenceImage
            ? `${API_BASE}/api/evidence/${encodeURIComponent(item.evidenceImage)}?t=${Date.now()}`
            : '';

        if (evidenceUrl) {
            image.src = evidenceUrl;
            image.style.display = "block";
        } else {
            image.src = "";
            image.style.display = "none";
        }

        // Info for live events
        info.innerHTML = `
            <div class="info-row">
                <div><span class="info-label">Machine</span><br><span class="info-value">${item.machine}</span></div>
                <div><span class="info-label">Category</span><br><span class="info-value">${item.category}</span></div>
                <div><span class="info-label">Error Code</span><br><span class="info-value">${item.errorCode}</span></div>
                <div><span class="info-label">Severity</span><br><span class="info-value" style="color:${item.sevClass === 'critical' ? 'var(--color-critical)' : item.sevClass === 'warning' ? 'var(--color-warning)' : 'var(--color-ok)'}">${(item.severity || item.sevClass).toUpperCase()}</span></div>
                <div><span class="info-label">Description</span><br><span class="info-value">${item.description || '-'}</span></div>
                <div><span class="info-label">Handler</span><br><span class="info-value">${item.handler || '-'}</span></div>
            </div>
            <div style="margin-top:12px;">
                <div style="font-weight:600;margin-bottom:6px;color:var(--text-primary);">Live Monitor Details</div>
                <div style="padding:8px 12px;background:var(--bg-card);border-radius:6px;border-left:3px solid var(--color-critical);">
                    <div style="font-size:13px;color:var(--text-secondary);">
                        Seen: <strong>${item.seenCount || 0}x</strong> ·
                        First: ${item.firstSeenAt ? new Date(item.firstSeenAt).toLocaleString() : '-'} ·
                        Last: ${item.lastSeenAt ? new Date(item.lastSeenAt).toLocaleString() : '-'}
                    </div>
                </div>
            </div>
        `;

        // Gallery: single evidence image thumb
        if (evidenceUrl) {
            const thumb = document.createElement("img");
            thumb.src = evidenceUrl;
            thumb.alt = "Evidence";
            thumb.title = "Evidence Capture";
            thumb.classList.add("active");
            gallery.appendChild(thumb);
        }

        overlay.classList.add("active");
        return;
    }

    // ── Batch error analysis: use /api/error-analysis/images/ path ──
    const encodedFilename = encodeURIComponent(`annotated_${item.filename}`);
    const imageUrl = `${API_BASE}/api/error-analysis/images/${item.machine}/${encodedFilename}?t=${Date.now()}`;

    image.src = imageUrl;
    image.style.display = "block";

    // Build classifications info
    const cls = (item.classifications || []);
    const clsHtml = cls.map(c => `
        <div style="margin-bottom:8px;padding:8px 12px;background:var(--bg-card);border-radius:6px;border-left:3px solid ${c.sev === 'high' ? 'var(--color-critical)' : c.sev === 'medium' ? 'var(--color-warning)' : 'var(--color-ok)'}">
            <div style="font-weight:600;color:var(--text-primary);">${c.error_id || ''}</div>
            <div style="font-size:13px;color:var(--text-secondary);margin-top:2px;">${c.desc || ''}</div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                Severity: <strong>${(c.sev || '').toUpperCase()}</strong> · Handler: ${c.handler || 'N/A'} · ${c.method || ''}
            </div>
        </div>
    `).join('');

    info.innerHTML = `
        <div class="info-row">
            <div><span class="info-label">Machine</span><br><span class="info-value">${item.machine}</span></div>
            <div><span class="info-label">Category</span><br><span class="info-value">${item.category}</span></div>
            <div><span class="info-label">Error Code</span><br><span class="info-value">${item.errorCode}</span></div>
            <div><span class="info-label">Severity</span><br><span class="info-value" style="color:${item.sevClass === 'critical' ? 'var(--color-critical)' : item.sevClass === 'warning' ? 'var(--color-warning)' : 'var(--color-ok)'}">${(item.severity || item.sevClass).toUpperCase()}</span></div>
            <div><span class="info-label">Lotnumber</span><br><span class="info-value">${item.lotnumber}</span></div>
            <div><span class="info-label">Testmode</span><br><span class="info-value">${item.testmode}</span></div>
        </div>
        <div style="margin-top:12px;">
            <div style="font-weight:600;margin-bottom:6px;color:var(--text-primary);">Classifications</div>
            ${clsHtml || '<div style="color:var(--text-muted)">No classification data</div>'}
        </div>
    `;

    // Also add original image to gallery if available
    const origUrl = `${API_BASE}/api/error-analysis/images/${item.machine}/${encodeURIComponent(item.filename)}?t=${Date.now()}`;
    const thumbAnnotated = document.createElement("img");
    thumbAnnotated.src = imageUrl;
    thumbAnnotated.alt = "Annotated";
    thumbAnnotated.title = "Annotated (Error Detection)";
    thumbAnnotated.classList.add("active");
    thumbAnnotated.onclick = () => {
        image.src = imageUrl;
        gallery.querySelectorAll("img").forEach(i => i.classList.remove("active"));
        thumbAnnotated.classList.add("active");
    };
    gallery.appendChild(thumbAnnotated);

    const thumbOriginal = document.createElement("img");
    thumbOriginal.src = origUrl;
    thumbOriginal.alt = "Original";
    thumbOriginal.title = "Original Screenshot";
    thumbOriginal.onclick = () => {
        image.src = origUrl;
        gallery.querySelectorAll("img").forEach(i => i.classList.remove("active"));
        thumbOriginal.classList.add("active");
    };
    gallery.appendChild(thumbOriginal);

    overlay.classList.add("active");
}

// Detail Modal (Machine VNC)
// ─────────────────────────────────────────

/**
 * Open wafer fail detail — shows the analyzed wafer image from wafer_analysis folder
 * with red cluster analysis data.
 */
function openWaferDetail(index) {
    const item = waferFailItems[index];
    if (!item) return;

    const overlay = document.getElementById("modalOverlay");
    const title = document.getElementById("modalTitle");
    const image = document.getElementById("modalImage");
    const info = document.getElementById("modalInfo");
    const gallery = document.getElementById("modalGallery");

    // Parse location label
    let locationLabel = "Center";
    const loc = (item.location || "center").toLowerCase();
    if (loc.includes("multiple")) locationLabel = "Multiple Edges";
    else if (loc.includes("top")) locationLabel = "Top Edge";
    else if (loc.includes("bottom")) locationLabel = "Bottom Edge";
    else if (loc.includes("left")) locationLabel = "Left Edge";
    else if (loc.includes("right")) locationLabel = "Right Edge";
    else if (loc.includes("edge")) locationLabel = "Edge";
    else locationLabel = "Center";

    const statusLabel = item.status; // SEVERE or HIGH
    const sevClass = item.concern_level >= 2 ? "critical" : "warning";
    const statusColor = sevClass === "critical" ? "var(--color-critical)" : "var(--color-warning)";

    title.textContent = `Wafer Analysis — ${item.machine}`;

    // Build wafer image URL from image_path
    if (item.image_path) {
        const encodedPath = item.image_path.split('/').map(p => encodeURIComponent(p)).join('/');
        image.src = `${API_BASE}/api/wafer-analysis/images/${encodedPath}?t=${Date.now()}`;
        image.style.display = "block";
    } else {
        // Fallback: construct from status + location + filename
        const concern = item.concern_level >= 2 ? "red_severe_concern" : "red_high_concern";
        const locPath = item.location === "center" ? "center" : item.location;
        const subpath = `${concern}/${locPath}/${encodeURIComponent(item.filename)}`;
        image.src = `${API_BASE}/api/wafer-analysis/images/${subpath}?t=${Date.now()}`;
        image.style.display = "block";
    }

    gallery.innerHTML = "";

    // Info section with wafer analysis data
    info.innerHTML = `
        <div class="info-row">
            <div><span class="info-label">Machine</span><br><span class="info-value">${item.machine}</span></div>
            <div><span class="info-label">Status</span><br><span class="info-value" style="color:${statusColor};font-weight:700;">${statusLabel}</span></div>
            <div><span class="info-label">Location</span><br><span class="info-value">${locationLabel}</span></div>
            <div><span class="info-label">Largest Cluster</span><br><span class="info-value">${item.largest_cluster_ratio.toFixed(1)}%</span></div>
            <div><span class="info-label">Red Ratio</span><br><span class="info-value">${item.red_ratio.toFixed(1)}%</span></div>
            <div><span class="info-label">Total Clusters</span><br><span class="info-value">${item.total_clusters}</span></div>
        </div>
        <div style="margin-top:12px;padding:10px 14px;background:var(--bg-card);border-radius:8px;border-left:4px solid ${statusColor};">
            <div style="font-weight:600;color:var(--text-primary);margin-bottom:4px;">Analysis Detail</div>
            <div style="font-size:13px;color:var(--text-secondary);">
                <div>File: <code style="font-size:12px;">${item.filename}</code></div>
                <div style="margin-top:4px;">Classification: Largest red cluster occupies <strong>${item.largest_cluster_ratio.toFixed(1)}%</strong> of wafer area</div>
                <div>Location: Red clusters primarily at <strong>${locationLabel}</strong></div>
                <div>Threshold: ${item.concern_level >= 2 ? 'Cluster > 7% (Severe)' : 'Cluster 1-7% (High Concern)'}</div>
            </div>
        </div>
    `;

    // Find neighboring fails (same machine, prev/next)
    const prevItem = waferFailItems[index - 1];
    const nextItem = waferFailItems[index + 1];

    // Navigation thumbnails
    if (prevItem && prevItem.image_path) {
        const prevThumb = document.createElement("img");
        const prevPath = prevItem.image_path.split('/').map(p => encodeURIComponent(p)).join('/');
        prevThumb.src = `${API_BASE}/api/wafer-analysis/images/${prevPath}?t=${Date.now()}`;
        prevThumb.alt = "Previous";
        prevThumb.title = `Previous: ${prevItem.filename}`;
        prevThumb.style.cursor = "pointer";
        prevThumb.onclick = () => openWaferDetail(index - 1);
        gallery.appendChild(prevThumb);
    }

    // Current (active)
    if (item.image_path) {
        const curThumb = document.createElement("img");
        const curPath = item.image_path.split('/').map(p => encodeURIComponent(p)).join('/');
        curThumb.src = `${API_BASE}/api/wafer-analysis/images/${curPath}?t=${Date.now()}`;
        curThumb.alt = "Current";
        curThumb.title = item.filename;
        curThumb.classList.add("active");
        gallery.appendChild(curThumb);
    }

    if (nextItem && nextItem.image_path) {
        const nextThumb = document.createElement("img");
        const nextPath = nextItem.image_path.split('/').map(p => encodeURIComponent(p)).join('/');
        nextThumb.src = `${API_BASE}/api/wafer-analysis/images/${nextPath}?t=${Date.now()}`;
        nextThumb.alt = "Next";
        nextThumb.title = `Next: ${nextItem.filename}`;
        nextThumb.style.cursor = "pointer";
        nextThumb.onclick = () => openWaferDetail(index + 1);
        gallery.appendChild(nextThumb);
    }

    overlay.classList.add("active");
}

async function openDetail(machineId) {
    const overlay = document.getElementById("modalOverlay");
    const title = document.getElementById("modalTitle");
    const image = document.getElementById("modalImage");
    const info = document.getElementById("modalInfo");
    const gallery = document.getElementById("modalGallery");

    title.textContent = "Loading...";
    image.src = "";
    info.innerHTML = "";
    gallery.innerHTML = "";
    overlay.classList.add("active");

    try {
        const resp = await fetch(`${API_BASE}/api/machines/${machineId}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        title.textContent = `${data.name} (${data.ip})`;

        if (data.hasImage) {
            image.src = `${API_BASE}/api/images/${machineId}/latest?t=${Date.now()}`;
            image.style.display = "block";
        } else {
            image.style.display = "none";
        }

        info.innerHTML = `
            <div class="info-row">
                <div><span class="info-label">Status</span><br><span class="info-value">${data.statusLabel}</span></div>
                <div><span class="info-label">Yield</span><br><span class="info-value">${data.yield ? data.yield.toFixed(1) + "%" : "N/A"}</span></div>
                <div><span class="info-label">Total Captures</span><br><span class="info-value">${data.totalCaptures || 0}</span></div>
                <div><span class="info-label">Good (Green)</span><br><span class="info-value">${data.greenCaptures || 0}</span></div>
                <div><span class="info-label">Last Update</span><br><span class="info-value">${data.lastUpdate || "N/A"}</span></div>
            </div>
        `;

        if (data.recentCaptures && data.recentCaptures.length > 0) {
            data.recentCaptures.slice(0, 15).forEach((cap, idx) => {
                const thumb = document.createElement("img");
                thumb.src = cap.url;
                thumb.alt = cap.filename;
                thumb.title = cap.timestamp;
                if (idx === 0) thumb.classList.add("active");
                thumb.onclick = () => {
                    image.src = cap.url;
                    gallery.querySelectorAll("img").forEach(i => i.classList.remove("active"));
                    thumb.classList.add("active");
                };
                gallery.appendChild(thumb);
            });
        }
    } catch (err) {
        title.textContent = "Error";
        info.innerHTML = `<p style="color: var(--color-critical);">Failed to load: ${err.message}</p>`;
    }
}

// ─────────────────────────────────────────
// Theme Toggle
// ─────────────────────────────────────────
function toggleTheme() {
    const html = document.documentElement;
    const current = html.getAttribute("data-theme") || "light";
    const next = current === "light" ? "dark" : "light";
    html.setAttribute("data-theme", next);
    localStorage.setItem("dashboard-theme", next);
}

function loadTheme() {
    const saved = localStorage.getItem("dashboard-theme");
    if (saved) {
        document.documentElement.setAttribute("data-theme", saved);
    }
}

// ─────────────────────────────────────────
// View Toggle
// ─────────────────────────────────────────
function setView(view) {
    currentView = view;
    document.getElementById("viewSeverity").classList.toggle("active", view === "severity");
    document.getElementById("viewMachines").classList.toggle("active", view === "machines");
    render();
}

// ─────────────────────────────────────────
// Filter Tabs
// ─────────────────────────────────────────
function setFilter(filter) {
    currentFilter = filter;
    document.querySelectorAll(".filter-tab").forEach(tab => {
        tab.classList.toggle("active", tab.dataset.filter === filter);
    });
    render();
}

// ─────────────────────────────────────────
// Auto-refresh
// ─────────────────────────────────────────
function startAutoRefresh(intervalSec = 30) {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(() => {
        fetchErrorAnalysis();
        fetchWaferFails();
        fetchMachines();
    }, intervalSec * 1000);
}

// ─────────────────────────────────────────
// Add Machine
// ─────────────────────────────────────────
function openAddMachine() {
    document.getElementById("addMachineForm").reset();
    document.getElementById("addMachineError").textContent = "";
    document.getElementById("addMachineOverlay").classList.add("active");
    document.getElementById("fmId").focus();
}

function closeAddMachine() {
    document.getElementById("addMachineOverlay").classList.remove("active");
}

async function submitAddMachine(e) {
    e.preventDefault();
    const errEl = document.getElementById("addMachineError");
    errEl.textContent = "";

    const id = document.getElementById("fmId").value.trim();
    const name = document.getElementById("fmName").value.trim();
    const ip = document.getElementById("fmIp").value.trim();
    const port = parseInt(document.getElementById("fmPort").value) || 5900;
    const password = document.getElementById("fmPassword").value.trim();
    const vnc_id = `RealVNC_${id}`;

    if (!id || !name) {
        errEl.textContent = "Machine ID and Display Name are required.";
        return;
    }

    const btn = e.target.querySelector(".btn-primary");
    btn.disabled = true;
    btn.textContent = "Adding...";

    try {
        const resp = await fetch(`${API_BASE}/api/machines`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id, name, ip, vnc_port: port, vnc_password: password, vnc_id }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            errEl.textContent = data.error || "Failed to add machine.";
            return;
        }
        closeAddMachine();
        await fetchMachines();
    } catch (err) {
        errEl.textContent = `Network error: ${err.message}`;
    } finally {
        btn.disabled = false;
        btn.textContent = "Add Machine";
    }
}

// ─────────────────────────────────────────
// Delete Machine
// ─────────────────────────────────────────
let pendingDeleteId = null;

function confirmDelete(machineId, machineName) {
    pendingDeleteId = machineId;
    document.getElementById("deleteTargetName").textContent = machineName;
    document.getElementById("deleteConfirmOverlay").classList.add("active");
}

function closeDeleteConfirm() {
    pendingDeleteId = null;
    document.getElementById("deleteConfirmOverlay").classList.remove("active");
}

async function executeDelete() {
    if (!pendingDeleteId) return;
    const btn = document.getElementById("deleteConfirmBtn");
    btn.disabled = true;
    btn.textContent = "Removing...";

    try {
        const resp = await fetch(`${API_BASE}/api/machines/${pendingDeleteId}`, {
            method: "DELETE",
        });
        if (!resp.ok) {
            const data = await resp.json();
            alert(data.error || "Failed to remove machine.");
            return;
        }
        closeDeleteConfirm();
        await fetchMachines();
    } catch (err) {
        alert(`Network error: ${err.message}`);
    } finally {
        btn.disabled = false;
        btn.textContent = "Remove";
    }
}

// ─────────────────────────────────────────
// Edit Machine
// ─────────────────────────────────────────
function openEditMachine(machineId) {
    // Find machine data from current machineData
    const m = machineData?.machines?.find(x => x.id === machineId);
    if (!m) { alert("Machine not found"); return; }

    document.getElementById("emId").value = machineId;
    document.getElementById("emIdDisplay").value = machineId;
    document.getElementById("emName").value = m.name || "";
    document.getElementById("emIp").value = m.ip || "";
    document.getElementById("emPort").value = m.vnc_port || 5900;
    document.getElementById("emPassword").value = "";  // don't pre-fill password
    document.getElementById("editMachineError").textContent = "";
    // Reset port hint
    const hint = document.getElementById("emPortHint");
    if (hint) { hint.textContent = "Default: 5900"; hint.className = ""; }
    document.getElementById("editMachineSuccess").style.display = "none";
    document.getElementById("editMachineOverlay").classList.add("active");
}

function closeEditMachine() {
    document.getElementById("editMachineOverlay").classList.remove("active");
}

async function submitEditMachine(e) {
    e.preventDefault();
    const errEl = document.getElementById("editMachineError");
    const successEl = document.getElementById("editMachineSuccess");
    errEl.textContent = "";
    successEl.style.display = "none";

    const machineId = document.getElementById("emId").value;
    const name = document.getElementById("emName").value.trim();
    const ip = document.getElementById("emIp").value.trim();
    const port = parseInt(document.getElementById("emPort").value) || 5900;
    const password = document.getElementById("emPassword").value;  // empty = keep existing
    const vnc_id = `RealVNC_${machineId}`;

    if (!name) {
        errEl.textContent = "Display Name is required.";
        return;
    }

    const body = { name, ip, vnc_port: port, vnc_id };
    // Only send password if user typed something
    if (password) {
        body.vnc_password = password;
    }

    const btn = e.target.querySelector("button[type='submit']");
    btn.disabled = true;
    btn.textContent = "Saving...";

    try {
        const resp = await fetch(`${API_BASE}/api/machines/${machineId}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (!resp.ok) {
            errEl.textContent = data.error || "Failed to update machine.";
            return;
        }
        successEl.textContent = "✓ Saved successfully";
        successEl.style.display = "block";
        await fetchMachines();
        // Auto-close after 1s
        setTimeout(() => closeEditMachine(), 800);
    } catch (err) {
        errEl.textContent = `Network error: ${err.message}`;
    } finally {
        btn.disabled = false;
        btn.textContent = "Save Changes";
    }
}

// ─────────────────────────────────────────
// Test VNC Connection
// ─────────────────────────────────────────
async function testVncConnection(machineId) {
    // Update badge to "testing"
    machineConnStatus[machineId] = "testing";
    updateConnectionBadge(machineId, "testing");

    try {
        const resp = await fetch(`${API_BASE}/api/vnc/test`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ machine_id: machineId }),
        });
        const data = await resp.json();
        const status = data.success ? "connected" : "disconnected";
        machineConnStatus[machineId] = status;
        updateConnectionBadge(machineId, status);

        if (!data.success) {
            console.warn(`VNC test failed for ${machineId}:`, data.error);
        }
    } catch (err) {
        machineConnStatus[machineId] = "disconnected";
        updateConnectionBadge(machineId, "disconnected");
        console.error(`VNC test error for ${machineId}:`, err);
    }
}

async function testAllVnc() {
    if (!machineData?.machines) return;

    // Set all to testing
    machineData.machines.forEach(m => {
        machineConnStatus[m.id] = "testing";
        updateConnectionBadge(m.id, "testing");
    });

    try {
        const resp = await fetch(`${API_BASE}/api/vnc/test-all`, { method: "POST" });
        const data = await resp.json();
        if (data.results) {
            for (const [mid, result] of Object.entries(data.results)) {
                const status = result.success ? "connected" : "disconnected";
                machineConnStatus[mid] = status;
                updateConnectionBadge(mid, status);
            }
        }
    } catch (err) {
        console.error("Test all VNC error:", err);
        machineData.machines.forEach(m => {
            machineConnStatus[m.id] = "disconnected";
            updateConnectionBadge(m.id, "disconnected");
        });
    }
}

function updateConnectionBadge(machineId, status) {
    const card = document.getElementById(`mc-${machineId}`);
    if (!card) return;

    const badge = card.querySelector(".mc-status-badge");
    if (!badge) return;

    const label = status === "connected" ? "Connected"
        : status === "disconnected" ? "Disconnected"
        : status === "testing" ? "Testing..."
        : "Unknown";

    badge.className = `mc-status-badge ${status}`;
    badge.innerHTML = `<span class="mc-status-dot ${status}"></span> ${label}`;
}

// Edit modal from Test button inside modal
async function testVncFromEditModal() {
    const machineId = document.getElementById("emId").value;
    const successEl = document.getElementById("editMachineSuccess");
    const errEl = document.getElementById("editMachineError");
    const btn = document.getElementById("emTestBtn");

    errEl.textContent = "";
    successEl.style.display = "none";
    btn.disabled = true;
    btn.textContent = "⏳ Testing...";

    try {
        const resp = await fetch(`${API_BASE}/api/vnc/test`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ machine_id: machineId }),
        });
        const data = await resp.json();
        if (data.success) {
            successEl.textContent = "✓ Connection successful!";
            successEl.style.display = "block";
            machineConnStatus[machineId] = "connected";
            updateConnectionBadge(machineId, "connected");
        } else {
            errEl.textContent = `Connection failed: ${data.error || "Unknown error"}`;
            machineConnStatus[machineId] = "disconnected";
            updateConnectionBadge(machineId, "disconnected");
        }
    } catch (err) {
        errEl.textContent = `Network error: ${err.message}`;
    } finally {
        btn.disabled = false;
        btn.textContent = "🔌 Test Connection";
    }
}

// ─────────────────────────────────────────
// Pipeline Functions
// ─────────────────────────────────────────
let pipelinePollingTimer = null;

function togglePipelinePanel() {
    const panel = document.getElementById("pipelinePanel");
    if (panel.style.display === "none") {
        panel.style.display = "block";
        loadPipelineResults();
    } else {
        panel.style.display = "none";
    }
}

function closePipelinePanel() {
    document.getElementById("pipelinePanel").style.display = "none";
    if (pipelinePollingTimer) {
        clearInterval(pipelinePollingTimer);
        pipelinePollingTimer = null;
    }
}

async function runPipeline() {
    const btn = document.getElementById("btnRunPipeline");
    btn.disabled = true;
    btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0a8 8 0 100 16A8 8 0 008 0z" opacity="0.3"/></svg> Starting...`;

    try {
        const resp = await fetch(`${API_BASE}/api/pipeline/run`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
        });
        const data = await resp.json();

        if (!resp.ok) {
            alert(data.error || "Failed to start pipeline");
            btn.disabled = false;
            btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M4 2l10 6-10 6z"/></svg> Run Full Pipeline`;
            return;
        }

        document.getElementById("pipelineProgress").style.display = "block";
        updatePipelineStatus("running", "Running...");
        pipelinePollingTimer = setInterval(pollPipelineStatus, 2000);

    } catch (err) {
        alert(`Error: ${err.message}`);
        btn.disabled = false;
        btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M4 2l10 6-10 6z"/></svg> Run Full Pipeline`;
    }
}

async function pollPipelineStatus() {
    try {
        const resp = await fetch(`${API_BASE}/api/pipeline/status`);
        const state = await resp.json();

        if (state.running) {
            const pct = state.total > 0 ? Math.round((state.progress / state.total) * 100) : 0;
            document.getElementById("progressFill").style.width = `${pct}%`;
            document.getElementById("progressText").textContent =
                `${state.step} - ${state.current_file} (${state.progress}/${state.total})`;
            updatePipelineStatus("running", state.step);
        } else {
            if (pipelinePollingTimer) {
                clearInterval(pipelinePollingTimer);
                pipelinePollingTimer = null;
            }

            document.getElementById("progressFill").style.width = "100%";
            document.getElementById("progressText").textContent = "Complete!";
            updatePipelineStatus("done", "Complete");

            const btn = document.getElementById("btnRunPipeline");
            btn.disabled = false;
            btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M4 2l10 6-10 6z"/></svg> Run Full Pipeline`;

            setTimeout(() => {
                loadPipelineResults();
                document.getElementById("pipelineProgress").style.display = "none";
                fetchErrorAnalysis();
                fetchMachines();
            }, 1000);
        }
    } catch (err) {
        console.error("Poll error:", err);
    }
}

function updatePipelineStatus(state, text) {
    const el = document.getElementById("pipelineStatus");
    const dotClass = state === "running" ? "running" : state === "done" ? "done" : "idle";
    el.innerHTML = `<span class="pipeline-dot ${dotClass}"></span> ${text}`;
}

async function loadPipelineResults() {
    // Load ROI results
    try {
        const roiResp = await fetch(`${API_BASE}/api/roi-results`);
        if (roiResp.ok) {
            const roi = await roiResp.json();
            document.getElementById("roiGood").textContent = roi.good;
            document.getElementById("roiBad").textContent = roi.bad;
            document.getElementById("roiTotal").textContent = roi.total;

            if (roi.total > 0) {
                const goodPct = (roi.good / roi.total) * 100;
                document.getElementById("roiBar").innerHTML = `
                    <div class="bar-segment bar-good" style="width:${goodPct}%"></div>
                    <div class="bar-segment bar-bad" style="width:${100 - goodPct}%"></div>
                `;
            }
        }
    } catch (e) { /* ignore */ }

    // Load wafer analysis
    try {
        const waferResp = await fetch(`${API_BASE}/api/wafer-analysis`);
        if (waferResp.ok) {
            const data = await waferResp.json();
            const cats = data.categories || {};

            document.getElementById("redNormal").textContent = cats.normal || 0;
            document.getElementById("redHigh").textContent = cats.high || 0;
            document.getElementById("redSevere").textContent = cats.severe || 0;

            const score = data.health_score || 0;
            const scoreEl = document.getElementById("healthScore");
            scoreEl.textContent = `${score}%`;
            if (score >= 90) scoreEl.style.color = "var(--color-ok)";
            else if (score >= 70) scoreEl.style.color = "var(--color-warning)";
            else scoreEl.style.color = "var(--color-critical)";

            document.getElementById("healthLabel").textContent =
                score >= 90 ? "Healthy" : score >= 70 ? "Warning" : "Critical";
            document.getElementById("healthTime").textContent =
                data.timestamp ? `Last: ${data.timestamp}` : "";

            const total = (cats.normal || 0) + (cats.high || 0) + (cats.severe || 0);
            if (total > 0) {
                const normPct = (cats.normal / total) * 100;
                const highPct = (cats.high / total) * 100;
                const sevPct = (cats.severe / total) * 100;
                document.getElementById("redBar").innerHTML = `
                    <div class="bar-segment bar-good" style="width:${normPct}%"></div>
                    <div class="bar-segment bar-warn" style="width:${highPct}%"></div>
                    <div class="bar-segment bar-bad" style="width:${sevPct}%"></div>
                `;
            }

            if (data.red_counts) {
                const section = document.getElementById("pipelineDetailSection");
                const grid = document.getElementById("redDetailGrid");
                section.style.display = "block";
                grid.innerHTML = "";

                const labels = {
                    normal: "Normal", high_center: "High/Center",
                    high_edge_top: "High/Top", high_edge_bottom: "High/Bottom",
                    high_edge_left: "High/Left", high_edge_right: "High/Right",
                    high_edge_multiple: "High/Multi-Edge",
                    severe_center: "Severe/Center",
                    severe_edge_top: "Severe/Top", severe_edge_bottom: "Severe/Bottom",
                    severe_edge_left: "Severe/Left", severe_edge_right: "Severe/Right",
                    severe_edge_multiple: "Severe/Multi-Edge",
                };

                for (const [key, label] of Object.entries(labels)) {
                    const count = data.red_counts[key] || 0;
                    if (count === 0 && key !== "normal") continue;

                    let colorClass = "";
                    if (key === "normal") colorClass = "ok";
                    else if (key.startsWith("high")) colorClass = "warn";
                    else colorClass = "bad";

                    const item = document.createElement("div");
                    item.className = "detail-item";
                    item.innerHTML = `
                        <div class="di-count ${colorClass}">${count}</div>
                        <div class="di-label">${label}</div>
                    `;
                    grid.appendChild(item);
                }
            }
        }
    } catch (e) { /* ignore */ }
}

// ─────────────────────────────────────────
// Close Modals
// ─────────────────────────────────────────
function closeModal(overlayId) {
    document.getElementById(overlayId).classList.remove("active");
}

// ─────────────────────────────────────────
// Auto-detect VNC Port
// ─────────────────────────────────────────
/**
 * Call backend to scan the IP and find an open VNC port.
 * @param {string} ip        - IP address to scan
 * @param {HTMLInputElement} portInput - the port <input> to fill in
 * @param {HTMLElement} hintEl - the <small> hint element to show status
 */
async function detectVncPort(ip, portInput, hintEl) {
    if (!ip) {
        if (hintEl) { hintEl.textContent = "กรอก IP Address ก่อน"; hintEl.className = "port-hint-err"; }
        return;
    }

    const btn = portInput?.closest(".port-input-group")?.querySelector(".btn-detect-port");
    if (btn) { btn.disabled = true; btn.querySelector("svg") && (btn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10" stroke-dasharray="60" stroke-dashoffset="0"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.8s" repeatCount="indefinite"/></circle></svg> Scanning...`); }
    if (hintEl) { hintEl.textContent = "กำลังสแกน port..."; hintEl.className = ""; }

    try {
        const resp = await fetch(`${API_BASE}/api/vnc/detect-port`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ip }),
        });
        const data = await resp.json();

        if (data.success && data.port) {
            if (portInput) portInput.value = data.port;
            if (hintEl) {
                hintEl.textContent = `✓ พบ VNC port ${data.port} บน ${ip}`;
                hintEl.className = "port-hint-ok";
            }
        } else {
            if (hintEl) {
                hintEl.textContent = `ไม่พบ VNC port บน ${ip} (${data.error || "ไม่มีการตอบสนอง"})`;
                hintEl.className = "port-hint-err";
            }
        }
    } catch (err) {
        if (hintEl) { hintEl.textContent = `Network error: ${err.message}`; hintEl.className = "port-hint-err"; }
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg> Auto-detect`; }
    }
}

// ─────────────────────────────────────────
// Init
// ─────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
    // Load saved theme
    loadTheme();

    // Theme toggle
    document.getElementById("btnThemeToggle").addEventListener("click", toggleTheme);

    // View toggle
    document.getElementById("viewSeverity").addEventListener("click", () => setView("severity"));
    document.getElementById("viewMachines").addEventListener("click", () => setView("machines"));

    // Filter tabs
    document.querySelectorAll(".filter-tab").forEach(tab => {
        tab.addEventListener("click", () => setFilter(tab.dataset.filter));
    });

    // Machine Grid View filter tabs
    document.getElementById("mgvTabs")?.addEventListener("click", (e) => {
        const tab = e.target.closest(".mgv-tab");
        if (!tab) return;
        mgvFilter = tab.dataset.mgv || "all";
        document.querySelectorAll(".mgv-tab").forEach(t => t.classList.remove("active"));
        tab.classList.add("active");
        if (currentView === "machines") renderMachineGridView(machineData?.machines || []);
    });

    // Search
    document.getElementById("searchInput").addEventListener("input", (e) => {
        searchQuery = e.target.value;
        render();
    });

    // Quick links
    document.getElementById("linkRunPipeline").addEventListener("click", (e) => {
        e.preventDefault();
        togglePipelinePanel();
    });

    document.getElementById("linkViewMap").addEventListener("click", (e) => {
        e.preventDefault();
        // Open first machine detail or toggle pipeline
        if (machineData && machineData.machines && machineData.machines.length > 0) {
            openDetail(machineData.machines[0].id);
        }
    });

    document.getElementById("linkFullAnalysis").addEventListener("click", (e) => {
        e.preventDefault();
        togglePipelinePanel();
    });

    // Manage Machines (opens add machine modal)
    document.getElementById("btnManageMachines").addEventListener("click", openAddMachine);

    // Manage Error Codes
    document.getElementById("btnManageErrorCodes").addEventListener("click", () => {
        window.location.href = "/error-codes";
    });

    // Real-Time Monitor Toggle
    document.getElementById("btnMonitorToggle").addEventListener("click", toggleMonitor);

    // Video Test Button
    document.getElementById("btnVideoTest").addEventListener("click", startVideoTest);

    // Pipeline
    document.getElementById("btnPipeline").addEventListener("click", togglePipelinePanel);
    document.getElementById("pipelinePanelClose").addEventListener("click", closePipelinePanel);
    document.getElementById("btnRunPipeline").addEventListener("click", runPipeline);

    // Add Machine modal
    document.getElementById("addMachineClose").addEventListener("click", closeAddMachine);
    document.getElementById("addMachineCancelBtn").addEventListener("click", closeAddMachine);
    document.getElementById("addMachineForm").addEventListener("submit", submitAddMachine);
    document.getElementById("addMachineOverlay").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeAddMachine();
    });
    // Auto-detect port button — Add modal
    document.getElementById("fmDetectPortBtn").addEventListener("click", () =>
        detectVncPort(
            document.getElementById("fmIp").value.trim(),
            document.getElementById("fmPort"),
            document.getElementById("fmPortHint")
        )
    );

    // Delete Confirm modal
    document.getElementById("deleteConfirmClose").addEventListener("click", closeDeleteConfirm);
    document.getElementById("deleteCancelBtn").addEventListener("click", closeDeleteConfirm);
    document.getElementById("deleteConfirmBtn").addEventListener("click", executeDelete);
    document.getElementById("deleteConfirmOverlay").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeDeleteConfirm();
    });

    // Edit Machine modal
    document.getElementById("editMachineClose").addEventListener("click", closeEditMachine);
    document.getElementById("editMachineCancelBtn").addEventListener("click", closeEditMachine);
    document.getElementById("editMachineForm").addEventListener("submit", submitEditMachine);
    document.getElementById("emTestBtn").addEventListener("click", testVncFromEditModal);
    document.getElementById("editMachineOverlay").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeEditMachine();
    });
    // Auto-detect port button — Edit modal
    document.getElementById("emDetectPortBtn").addEventListener("click", () =>
        detectVncPort(
            document.getElementById("emIp").value.trim(),
            document.getElementById("emPort"),
            document.getElementById("emPortHint")
        )
    );

    // Detail modal
    document.getElementById("modalClose").addEventListener("click", () => closeModal("modalOverlay"));
    document.getElementById("modalOverlay").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeModal("modalOverlay");
    });

    // Escape key
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            closeModal("modalOverlay");
            closeAddMachine();
            closeDeleteConfirm();
            closeEditMachine();
        }
    });

    // Load initial data - fetch error analysis + wafer fails FIRST so they're available for render
    await fetchErrorAnalysis();
    await fetchWaferFails();
    const data = await fetchMachines();
    const interval = data?.refreshInterval || 30;
    startAutoRefresh(interval);

    // Load pipeline results
    loadPipelineResults();

    // Check if monitor is already running (e.g. after page reload)
    await fetchMonitorStatus();
    if (monitorRunning) {
        startMonitorPolling();
    }
});
