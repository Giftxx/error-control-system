/**
 * Error Management Console - JavaScript
 * CRUD operations for error codes with search, sort, pagination
 */

const API_BASE = window.location.origin;

// ─────────────────────────────────────────
// State
// ─────────────────────────────────────────
let allErrorCodes = [];
let filteredCodes = [];
let currentPage = 1;
let pageSize = 10;
let sortField = "severity";
let sortDir = "desc";
let searchQuery = "";
let editingCode = null; // null = add mode, string = editing error_id
let importData = null;

// Severity order for sorting
const SEV_ORDER = { high: 0, medium: 1, low: 2 };

// ─────────────────────────────────────────
// Auto-Scan Status Polling (after CRUD operations)
// ─────────────────────────────────────────
let _scanPollTimer = null;
function _pollScanStatus() {
    if (_scanPollTimer) clearInterval(_scanPollTimer);
    _scanPollTimer = setInterval(async () => {
        try {
            const resp = await fetch(`${API_BASE}/api/quick-scan/status`);
            if (!resp.ok) return;
            const status = await resp.json();
            if (!status.running) {
                clearInterval(_scanPollTimer);
                _scanPollTimer = null;
                if (status.error) {
                    showToast(`Scan failed: ${status.error}`, "error");
                } else if (status.result) {
                    showToast(`Scan complete: ${status.result.detected || 0} errors detected, ${status.result.classified || 0} classified`, "success");
                }
            }
        } catch (e) { /* ignore */ }
    }, 3000);
}

// ─────────────────────────────────────────
// Data Fetching
// ─────────────────────────────────────────
async function fetchErrorCodes() {
    showLoading();
    try {
        const resp = await fetch(`${API_BASE}/api/error-codes`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        allErrorCodes = (data.codes || []).map((c, i) => ({
            ...c,
            _index: i,
        }));
        applyFilters();
        render();
    } catch (err) {
        console.error("Failed to fetch error codes:", err);
        document.getElementById("ecTableBody").innerHTML =
            `<tr><td colspan="6" class="ec-empty">Failed to load error codes<br><small>${err.message}</small></td></tr>`;
    }
}

/**
 * Convert an ISO/datetime string to a human-readable relative time.
 * Returns the original string if parsing fails.
 */
function formatTimeAgo(dateStr) {
    if (!dateStr) return "—";
    const date = new Date(dateStr.replace(" ", "T"));
    if (isNaN(date.getTime())) return dateStr;

    const now = new Date();
    const diffMs = now - date;
    const diffSec = Math.floor(diffMs / 1000);
    const diffMin = Math.floor(diffSec / 60);
    const diffHr  = Math.floor(diffMin / 60);
    const diffDay = Math.floor(diffHr / 24);

    if (diffSec < 60) return "just now";
    if (diffMin < 60) return `${diffMin} min ago`;
    if (diffHr  < 24) return `${diffHr} hour${diffHr > 1 ? "s" : ""} ago`;
    if (diffDay === 1) return "yesterday";
    if (diffDay < 7)  return `${diffDay} days ago`;
    if (diffDay < 30) {
        const w = Math.floor(diffDay / 7);
        return `${w} week${w > 1 ? "s" : ""} ago`;
    }
    if (diffDay < 365) {
        const m = Math.floor(diffDay / 30);
        return `${m} month${m > 1 ? "s" : ""} ago`;
    }
    const y = Math.floor(diffDay / 365);
    return `${y} year${y > 1 ? "s" : ""} ago`;
}

// ─────────────────────────────────────────
// Filter, Sort, Paginate
// ─────────────────────────────────────────
function applyFilters() {
    let codes = [...allErrorCodes];

    // Search
    if (searchQuery) {
        const q = searchQuery.toLowerCase();
        codes = codes.filter(c =>
            (c.error_id || "").toLowerCase().includes(q) ||
            (c.description || "").toLowerCase().includes(q) ||
            (c.category || "").toLowerCase().includes(q) ||
            (c.severity || "").toLowerCase().includes(q) ||
            (c.handler || "").toLowerCase().includes(q)
        );
    }

    // Sort
    codes.sort((a, b) => {
        let va, vb;
        switch (sortField) {
            case "code":
                va = (a.error_id || "").toLowerCase();
                vb = (b.error_id || "").toLowerCase();
                break;
            case "title":
                va = (a.description || "").toLowerCase();
                vb = (b.description || "").toLowerCase();
                break;
            case "severity":
                va = SEV_ORDER[a.severity] ?? 99;
                vb = SEV_ORDER[b.severity] ?? 99;
                break;
            case "updated":
                va = a.updated_at || "";
                vb = b.updated_at || "";
                break;
            default:
                va = 0; vb = 0;
        }
        if (va < vb) return sortDir === "asc" ? -1 : 1;
        if (va > vb) return sortDir === "asc" ? 1 : -1;
        return 0;
    });

    filteredCodes = codes;
}

function getPageData() {
    const start = (currentPage - 1) * pageSize;
    const end = start + pageSize;
    return filteredCodes.slice(start, end);
}

function getTotalPages() {
    return Math.max(1, Math.ceil(filteredCodes.length / pageSize));
}

// ─────────────────────────────────────────
// Rendering
// ─────────────────────────────────────────
function render() {
    renderTable();
    renderPagination();
    renderInfo();
}

function showLoading() {
    document.getElementById("ecTableBody").innerHTML =
        `<tr><td colspan="6" class="ec-loading"><div class="loading-spinner"></div>Loading error codes...</td></tr>`;
}

function renderTable() {
    const tbody = document.getElementById("ecTableBody");
    const data = getPageData();

    if (data.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="ec-empty">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <circle cx="12" cy="12" r="10"/><path d="M8 15h8M9 9h.01M15 9h.01"/>
            </svg>
            No error codes found
        </td></tr>`;
        return;
    }

    tbody.innerHTML = data.map(code => {
        const sevClass = getSeverityClass(code.severity);
        const sevLabel = getSeverityLabel(code.severity);
        const sevIcon = getSeverityIcon(code.severity);
        const isWarningCode = (code.error_id || "").startsWith("W");
        const codeClass = isWarningCode ? "warning-code" : "";

        return `<tr data-id="${escapeHtml(code.error_id)}">
            <td class="ec-td-check"><input type="checkbox" class="ec-row-check" data-id="${escapeHtml(code.error_id)}"></td>
            <td><span class="ec-code ${codeClass}">${escapeHtml(code.error_id)}</span></td>
            <td class="ec-title-cell">${escapeHtml(code.description)}</td>
            <td>
                <span class="ec-severity ${sevClass}">
                    <span class="ec-severity-icon">${sevIcon}</span>
                    ${sevLabel}
                </span>
            </td>
            <td class="ec-updated" title="${escapeHtml(code.updated_at || '')}">${formatTimeAgo(code.updated_at)}</td>
            <td>
                <div class="ec-actions">
                    <button class="ec-action-btn edit" onclick="openEditModal('${escapeAttr(code.error_id)}')" title="Edit">
                        <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M11.013 1.427a1.75 1.75 0 012.474 0l1.086 1.086a1.75 1.75 0 010 2.474l-8.61 8.61c-.21.21-.47.364-.756.445l-3.251.93a.75.75 0 01-.927-.928l.929-3.25a1.75 1.75 0 01.445-.758l8.61-8.61zm1.414 1.06a.25.25 0 00-.354 0L3.463 11.098a.25.25 0 00-.064.108l-.631 2.208 2.208-.63a.25.25 0 00.108-.064l8.61-8.61a.25.25 0 000-.355l-1.086-1.086z"/></svg>
                    </button>
                    <button class="ec-action-btn delete" onclick="openDeleteModal('${escapeAttr(code.error_id)}')" title="Delete">
                        <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M6.5 1.75a.25.25 0 01.25-.25h2.5a.25.25 0 01.25.25V3h-3V1.75zm4.5 0V3h2.25a.75.75 0 010 1.5H2.75a.75.75 0 010-1.5H5V1.75C5 .784 5.784 0 6.75 0h2.5C10.216 0 11 .784 11 1.75zM4.496 6.675a.75.75 0 10-1.492.15l.66 6.6A1.75 1.75 0 005.405 15h5.19a1.75 1.75 0 001.741-1.575l.66-6.6a.75.75 0 00-1.492-.15l-.66 6.6a.25.25 0 01-.249.225h-5.19a.25.25 0 01-.249-.225l-.66-6.6z"/></svg>
                    </button>
                </div>
            </td>
        </tr>`;
    }).join("");
}

function renderPagination() {
    const totalPages = getTotalPages();
    const paginationHTML = buildPaginationHTML(totalPages);
    document.getElementById("ecPagination").innerHTML = paginationHTML;
    document.getElementById("ecPaginationTop").innerHTML = buildPaginationTopHTML(totalPages);
}

function buildPaginationHTML(totalPages) {
    if (totalPages <= 1) return "";

    let html = "";
    html += `<button class="ec-page-btn" onclick="goToPage(${currentPage - 1})" ${currentPage === 1 ? "disabled" : ""}>&lt;</button>`;

    const pages = getPaginationRange(currentPage, totalPages);
    for (const p of pages) {
        if (p === "...") {
            html += `<span class="ec-page-ellipsis">...</span>`;
        } else {
            html += `<button class="ec-page-btn ${p === currentPage ? "active" : ""}" onclick="goToPage(${p})">${p}</button>`;
        }
    }

    html += `<button class="ec-page-btn" onclick="goToPage(${currentPage + 1})" ${currentPage === totalPages ? "disabled" : ""}>&gt;</button>`;
    return html;
}

function buildPaginationTopHTML(totalPages) {
    if (totalPages <= 1) return "";
    let html = `<button class="ec-page-btn" onclick="goToPage(${currentPage - 1})" ${currentPage === 1 ? "disabled" : ""}>&lt;</button>`;
    // Show max 3 pages for top
    for (let i = 1; i <= Math.min(3, totalPages); i++) {
        html += `<button class="ec-page-btn ${i === currentPage ? "active" : ""}" onclick="goToPage(${i})">${i}</button>`;
    }
    html += `<button class="ec-page-btn" onclick="goToPage(${currentPage + 1})" ${currentPage === totalPages ? "disabled" : ""}>&gt;</button>`;
    return html;
}

function getPaginationRange(current, total) {
    if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);

    const pages = [];
    if (current <= 3) {
        pages.push(1, 2, 3, "...", total);
    } else if (current >= total - 2) {
        pages.push(1, "...", total - 2, total - 1, total);
    } else {
        pages.push(1, "...", current - 1, current, current + 1, "...", total);
    }
    return pages;
}

function renderInfo() {
    const total = filteredCodes.length;
    const start = total === 0 ? 0 : (currentPage - 1) * pageSize + 1;
    const end = Math.min(currentPage * pageSize, total);
    const text = `Showing ${start} to ${end} of ${total}`;
    document.getElementById("ecShowing").textContent = text;
    document.getElementById("ecShowingBottom").textContent = text;
}

// ─────────────────────────────────────────
// Severity helpers
// ─────────────────────────────────────────
function getSeverityClass(sev) {
    switch ((sev || "").toLowerCase()) {
        case "high": return "critical";
        case "medium": return "warning";
        case "low": return "low";
        default: return "low";
    }
}

function getSeverityLabel(sev) {
    switch ((sev || "").toLowerCase()) {
        case "high": return "Critical";
        case "medium": return "Warning";
        case "low": return "Low";
        default: return sev || "Unknown";
    }
}

function getSeverityIcon(sev) {
    const cls = getSeverityClass(sev);
    if (cls === "critical") {
        return `<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M2.343 13.515A8 8 0 1113.656 2.486 8 8 0 012.343 13.515zM7.25 5v4.5a.75.75 0 001.5 0V5a.75.75 0 00-1.5 0zM8 12a1 1 0 100-2 1 1 0 000 2z"/></svg>`;
    } else if (cls === "warning") {
        return `<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M6.457 1.047c.659-1.234 2.427-1.234 3.086 0l6.09 11.418c.636 1.192-.179 2.635-1.543 2.635H1.91C.546 15.1-.27 13.657.367 12.465l6.09-11.418zM8 5a.75.75 0 00-.75.75v2.5a.75.75 0 001.5 0v-2.5A.75.75 0 008 5zm0 7a1 1 0 100-2 1 1 0 000 2z"/></svg>`;
    }
    return `<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M8 16A8 8 0 108 0a8 8 0 000 16zm3.78-9.72a.75.75 0 00-1.06-1.06L7 8.94 5.28 7.22a.75.75 0 00-1.06 1.06l2.25 2.25a.75.75 0 001.06 0l4.25-4.25z"/></svg>`;
}

// ─────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────
// ─────────────────────────────────────────
// Auto-generate keywords from code + description
// ─────────────────────────────────────────
function generateKeywords(errorId, description) {
    const parts = [];

    // --- From error_id ---
    const code = (errorId || "").trim();
    if (code) {
        // Add the raw code (lowered), e.g. "e 9393" -> "e.?9393"
        const codeClean = code.replace(/\s+/g, ".?").toLowerCase();
        parts.push(codeClean);

        // Extract pure number part, e.g. "E 9393" -> "9393"
        const numMatch = code.match(/\d+/);
        if (numMatch) parts.push(numMatch[0]);

        // If code contains underscore-style like YIELD_VIOLATION -> "yield.*violation"
        if (code.includes("_")) {
            const underscoreParts = code.split("_").map(p => p.toLowerCase().trim()).filter(Boolean);
            parts.push(underscoreParts.join(".*"));
            underscoreParts.forEach(p => { if (p.length > 2) parts.push(p); });
        }
    }

    // --- From description ---
    const desc = (description || "").trim();
    if (desc) {
        // Split into meaningful words (>= 3 chars), ignoring common stop words
        const stopWords = new Set([
            "the", "and", "for", "with", "not", "from", "that", "this",
            "are", "was", "has", "have", "been", "will", "can",
            "all", "but", "its", "may", "did", "get", "set",
        ]);
        const words = desc
            .replace(/[()\[\]{},.:;!?'"]/g, " ")
            .split(/\s+/)
            .map(w => w.toLowerCase().trim())
            .filter(w => w.length >= 3 && !stopWords.has(w));

        // Add individual significant words
        words.forEach(w => parts.push(w));

        // Add 2-word consecutive combos as "word1.*word2"
        for (let i = 0; i < words.length - 1; i++) {
            parts.push(words[i] + ".*" + words[i + 1]);
        }

        // Full description lowered, spaces -> .*
        const descPattern = words.join(".*");
        if (descPattern && words.length <= 5) parts.push(descPattern);
    }

    // Deduplicate while preserving order
    const seen = new Set();
    const unique = [];
    for (const p of parts) {
        if (p && !seen.has(p)) {
            seen.add(p);
            unique.push(p);
        }
    }

    return unique.join("|");
}

function escapeHtml(str) {
    if (!str) return "";
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function escapeAttr(str) {
    return escapeHtml(str).replace(/'/g, "&#39;");
}

function showToast(message, type = "success") {
    // Remove existing toast
    const existing = document.querySelector(".ec-toast");
    if (existing) existing.remove();

    const toast = document.createElement("div");
    toast.className = `ec-toast ${type}`;
    toast.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor">
            ${type === "success"
                ? '<path d="M8 16A8 8 0 108 0a8 8 0 000 16zm3.78-9.72a.75.75 0 00-1.06-1.06L7 8.94 5.28 7.22a.75.75 0 00-1.06 1.06l2.25 2.25a.75.75 0 001.06 0l4.25-4.25z"/>'
                : '<path d="M2.343 13.515A8 8 0 1113.656 2.486 8 8 0 012.343 13.515zM7.25 5v4.5a.75.75 0 001.5 0V5a.75.75 0 00-1.5 0zM8 12a1 1 0 100-2 1 1 0 000 2z"/>'}
        </svg>
        <span>${message}</span>
    `;
    document.body.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add("show"));
    setTimeout(() => {
        toast.classList.remove("show");
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ─────────────────────────────────────────
// CRUD Operations
// ─────────────────────────────────────────
async function addErrorCode(data) {
    try {
        const resp = await fetch(`${API_BASE}/api/error-codes`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data),
        });
        const result = await resp.json();
        if (!resp.ok) throw new Error(result.error || "Failed to add error code");
        showToast(`Error code "${data.error_id}" added — auto re-scanning images...`, "info");
        await fetchErrorCodes();
        _pollScanStatus();
        return true;
    } catch (err) {
        showToast(err.message, "error");
        return false;
    }
}

async function updateErrorCode(originalId, data) {
    try {
        const resp = await fetch(`${API_BASE}/api/error-codes/${encodeURIComponent(originalId)}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data),
        });
        const result = await resp.json();
        if (!resp.ok) throw new Error(result.error || "Failed to update error code");
        showToast(`Error code "${originalId}" updated — auto re-scanning images...`, "info");
        await fetchErrorCodes();
        _pollScanStatus();
        return true;
    } catch (err) {
        showToast(err.message, "error");
        return false;
    }
}

async function deleteErrorCode(errorId) {
    try {
        const resp = await fetch(`${API_BASE}/api/error-codes/${encodeURIComponent(errorId)}`, {
            method: "DELETE",
        });
        const result = await resp.json();
        if (!resp.ok) throw new Error(result.error || "Failed to delete error code");
        showToast(`Error code "${errorId}" deleted — auto re-scanning images...`, "info");
        await fetchErrorCodes();
        _pollScanStatus();
        return true;
    } catch (err) {
        showToast(err.message, "error");
        return false;
    }
}

async function importErrorCodes(codes) {
    try {
        const resp = await fetch(`${API_BASE}/api/error-codes/import`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ codes }),
        });
        const result = await resp.json();
        if (!resp.ok) throw new Error(result.error || "Failed to import");
        showToast(`Imported ${result.imported || codes.length} error codes`);
        await fetchErrorCodes();
        return true;
    } catch (err) {
        showToast(err.message, "error");
        return false;
    }
}

// ─────────────────────────────────────────
// Category dropdown (dynamic from real data)
// ─────────────────────────────────────────
function populateCategoryDropdown(selectedValue) {
    const select = document.getElementById("ecFmCategory");
    const customInput = document.getElementById("ecFmCategoryCustom");

    // Collect unique categories from loaded data
    const cats = new Set();
    allErrorCodes.forEach(c => {
        if (c.category && c.category.trim()) cats.add(c.category.trim());
    });
    const sorted = [...cats].sort();

    // Rebuild options
    select.innerHTML = '<option value="">-- Select Category --</option>';
    sorted.forEach(cat => {
        const opt = document.createElement("option");
        opt.value = cat;
        // Display-friendly label: replace _ with space
        opt.textContent = cat.replace(/_/g, " ");
        select.appendChild(opt);
    });
    // "+ Add New" option at bottom
    const addOpt = document.createElement("option");
    addOpt.value = "__NEW__";
    addOpt.textContent = "+ Add New Category";
    select.appendChild(addOpt);

    // Set selected value
    if (selectedValue) {
        // If the value exists in options, select it
        const exists = sorted.includes(selectedValue);
        if (exists) {
            select.value = selectedValue;
            customInput.style.display = "none";
        } else {
            // It's a custom value not in DB yet — show custom input
            select.value = "__NEW__";
            customInput.style.display = "block";
            customInput.value = selectedValue;
        }
    } else {
        select.value = "";
        customInput.style.display = "none";
        customInput.value = "";
    }
}

function getSelectedCategory() {
    const select = document.getElementById("ecFmCategory");
    if (select.value === "__NEW__") {
        return document.getElementById("ecFmCategoryCustom").value.trim();
    }
    return select.value;
}

// ─────────────────────────────────────────
// Modals
// ─────────────────────────────────────────
function openAddModal() {
    editingCode = null;
    document.getElementById("ecModalTitle").textContent = "Add Error Code";
    document.getElementById("ecModalSubmit").textContent = "Add Error";
    document.getElementById("ecForm").reset();
    document.getElementById("ecFmCode").disabled = false;
    document.getElementById("ecFormError").textContent = "";
    populateCategoryDropdown("");
    document.getElementById("ecModalOverlay").classList.add("active");
}

function openEditModal(errorId) {
    const code = allErrorCodes.find(c => c.error_id === errorId);
    if (!code) return;

    editingCode = errorId;
    document.getElementById("ecModalTitle").textContent = "Edit Error Code";
    document.getElementById("ecModalSubmit").textContent = "Save Changes";
    document.getElementById("ecFmCode").value = code.error_id;
    document.getElementById("ecFmCode").disabled = true;
    document.getElementById("ecFmTitle").value = code.description || "";
    populateCategoryDropdown(code.category || "");
    document.getElementById("ecFmSeverity").value = code.severity || "medium";
    document.getElementById("ecFmHandler").value = code.handler || "";
    document.getElementById("ecFormError").textContent = "";
    document.getElementById("ecModalOverlay").classList.add("active");
}

function closeModal() {
    document.getElementById("ecModalOverlay").classList.remove("active");
    editingCode = null;
}

function openDeleteModal(errorId) {
    document.getElementById("ecDeleteTarget").textContent = errorId;
    document.getElementById("ecDeleteOverlay").classList.add("active");
}

function closeDeleteModal() {
    document.getElementById("ecDeleteOverlay").classList.remove("active");
}

function openImportModal() {
    importData = null;
    document.getElementById("ecImportPreview").style.display = "none";
    document.getElementById("ecImportConfirm").disabled = true;
    document.getElementById("ecFileInput").value = "";
    document.getElementById("ecImportOverlay").classList.add("active");
}

function closeImportModal() {
    document.getElementById("ecImportOverlay").classList.remove("active");
}

// ─────────────────────────────────────────
// Export
// ─────────────────────────────────────────
function exportCSV() {
    const headers = ["error_id", "cat", "desc", "sev", "handler", "kw", "updated_at"];
    const rows = allErrorCodes.map(c => [
        c.error_id, c.category, c.description, c.severity, c.handler, c.keywords || "", c.updated_at || ""
    ]);

    let csv = headers.join(",") + "\n";
    rows.forEach(row => {
        csv += row.map(v => `"${(v || "").replace(/"/g, '""')}"`).join(",") + "\n";
    });

    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `error_codes_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    showToast("CSV exported successfully", "info");
}

// ─────────────────────────────────────────
// CSV Import parsing
// ─────────────────────────────────────────
function parseCSV(text) {
    const lines = text.trim().split("\n");
    if (lines.length < 2) return [];

    const headers = lines[0].split(",").map(h => h.trim().replace(/^"|"$/g, ""));
    const codes = [];

    for (let i = 1; i < lines.length; i++) {
        const values = parseCSVLine(lines[i]);
        if (values.length < 2) continue;

        const row = {};
        headers.forEach((h, idx) => {
            row[h] = (values[idx] || "").trim();
        });

        codes.push({
            error_id: row.error_id || row.code || "",
            category: row.cat || row.category || "",
            description: row.desc || row.description || row.title || "",
            severity: row.sev || row.severity || "medium",
            handler: row.handler || "",
            keywords: row.kw || row.keywords || "",
        });
    }

    return codes.filter(c => c.error_id);
}

function parseCSVLine(line) {
    const result = [];
    let current = "";
    let inQuotes = false;

    for (let i = 0; i < line.length; i++) {
        const ch = line[i];
        if (inQuotes) {
            if (ch === '"' && line[i + 1] === '"') {
                current += '"';
                i++;
            } else if (ch === '"') {
                inQuotes = false;
            } else {
                current += ch;
            }
        } else {
            if (ch === '"') {
                inQuotes = true;
            } else if (ch === ',') {
                result.push(current);
                current = "";
            } else {
                current += ch;
            }
        }
    }
    result.push(current);
    return result;
}

// ─────────────────────────────────────────
// Navigation
// ─────────────────────────────────────────
function goToPage(page) {
    const totalPages = getTotalPages();
    if (page < 1 || page > totalPages) return;
    currentPage = page;
    render();
    // Scroll table into view smoothly
    document.querySelector(".ec-table-wrapper").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ─────────────────────────────────────────
// Theme Toggle
// ─────────────────────────────────────────
function initTheme() {
    const saved = localStorage.getItem("theme");
    if (saved) {
        document.documentElement.setAttribute("data-theme", saved);
    }
}

function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
}

// ─────────────────────────────────────────
// Event Listeners
// ─────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    initTheme();

    // Fetch data
    fetchErrorCodes();

    // Category dropdown: toggle custom input on "+ Add New"
    document.getElementById("ecFmCategory").addEventListener("change", (e) => {
        const customInput = document.getElementById("ecFmCategoryCustom");
        if (e.target.value === "__NEW__") {
            customInput.style.display = "block";
            customInput.focus();
        } else {
            customInput.style.display = "none";
            customInput.value = "";
        }
    });

    // Search
    let searchTimeout;
    document.getElementById("ecSearchInput").addEventListener("input", (e) => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => {
            searchQuery = e.target.value.trim();
            currentPage = 1;
            applyFilters();
            render();
        }, 300);
    });

    // Sort select
    document.getElementById("ecSortSelect").addEventListener("change", (e) => {
        sortField = e.target.value;
        currentPage = 1;
        applyFilters();
        render();
    });

    // Page size
    document.getElementById("ecPageSize").addEventListener("change", (e) => {
        pageSize = parseInt(e.target.value) || 10;
        currentPage = 1;
        render();
    });

    // Column sort headers
    document.querySelectorAll(".ec-th-sortable").forEach(th => {
        th.addEventListener("click", () => {
            const field = th.dataset.sort;
            if (sortField === field) {
                sortDir = sortDir === "asc" ? "desc" : "asc";
            } else {
                sortField = field;
                sortDir = "asc";
            }
            // Update select
            document.getElementById("ecSortSelect").value = sortField;
            // Update header styles
            document.querySelectorAll(".ec-th-sortable").forEach(h => h.classList.remove("asc", "desc"));
            th.classList.add(sortDir);

            currentPage = 1;
            applyFilters();
            render();
        });
    });

    // Sort ascending button
    document.getElementById("btnSortAsc").addEventListener("click", () => {
        sortDir = sortDir === "asc" ? "desc" : "asc";
        applyFilters();
        render();
    });

    // Select all checkbox
    document.getElementById("ecSelectAll").addEventListener("change", (e) => {
        document.querySelectorAll(".ec-row-check").forEach(cb => {
            cb.checked = e.target.checked;
        });
    });

    // Theme toggle
    document.getElementById("btnThemeToggle").addEventListener("click", toggleTheme);

    // Add Error buttons
    document.getElementById("btnAddError").addEventListener("click", openAddModal);
    document.getElementById("btnAddErrorHeader").addEventListener("click", openAddModal);

    // Import CSV
    document.getElementById("btnImportCSV").addEventListener("click", openImportModal);

    // Export
    document.getElementById("btnExport").addEventListener("click", exportCSV);

    // Modal - Form submit
    document.getElementById("ecForm").addEventListener("submit", async (e) => {
        e.preventDefault();
        const errorId = document.getElementById("ecFmCode").value.trim();
        const description = document.getElementById("ecFmTitle").value.trim();
        const data = {
            error_id: errorId,
            category: getSelectedCategory(),
            description: description,
            severity: document.getElementById("ecFmSeverity").value,
            handler: document.getElementById("ecFmHandler").value.trim(),
            keywords: generateKeywords(errorId, description),
        };

        if (!data.error_id || !data.description) {
            document.getElementById("ecFormError").textContent = "Code and Title are required";
            return;
        }

        let success;
        if (editingCode) {
            success = await updateErrorCode(editingCode, data);
        } else {
            success = await addErrorCode(data);
        }

        if (success) closeModal();
    });

    // Modal - Close
    document.getElementById("ecModalClose").addEventListener("click", closeModal);
    document.getElementById("ecModalCancel").addEventListener("click", closeModal);
    document.getElementById("ecModalOverlay").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeModal();
    });

    // Delete modal
    document.getElementById("ecDeleteClose").addEventListener("click", closeDeleteModal);
    document.getElementById("ecDeleteCancel").addEventListener("click", closeDeleteModal);
    document.getElementById("ecDeleteOverlay").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeDeleteModal();
    });
    document.getElementById("ecDeleteConfirm").addEventListener("click", async () => {
        const errorId = document.getElementById("ecDeleteTarget").textContent;
        await deleteErrorCode(errorId);
        closeDeleteModal();
    });

    // Import modal
    document.getElementById("ecImportClose").addEventListener("click", closeImportModal);
    document.getElementById("ecImportCancel").addEventListener("click", closeImportModal);
    document.getElementById("ecImportOverlay").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeImportModal();
    });

    // Drop zone
    const dropZone = document.getElementById("ecDropZone");
    dropZone.addEventListener("click", () => document.getElementById("ecFileInput").click());
    dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("dragover");
        if (e.dataTransfer.files.length) handleImportFile(e.dataTransfer.files[0]);
    });

    document.getElementById("ecFileInput").addEventListener("change", (e) => {
        if (e.target.files.length) handleImportFile(e.target.files[0]);
    });

    document.getElementById("ecImportConfirm").addEventListener("click", async () => {
        if (importData && importData.length > 0) {
            await importErrorCodes(importData);
            closeImportModal();
        }
    });
});

function handleImportFile(file) {
    if (!file.name.endsWith(".csv")) {
        showToast("Please select a CSV file", "error");
        return;
    }

    const reader = new FileReader();
    reader.onload = (e) => {
        importData = parseCSV(e.target.result);
        if (importData.length === 0) {
            showToast("No valid error codes found in file", "error");
            return;
        }
        document.getElementById("ecImportPreview").style.display = "block";
        document.getElementById("ecImportInfo").textContent =
            `Found ${importData.length} error codes ready to import from "${file.name}"`;
        document.getElementById("ecImportConfirm").disabled = false;
    };
    reader.readAsText(file);
}
