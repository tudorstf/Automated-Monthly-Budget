/* ==========================================================================
   Receipt Budget Automator — Frontend Controller
   ========================================================================== */

(() => {
  "use strict";

  const ALLOWED_EXTENSIONS = new Set([
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".heif",
    ".pdf",
  ]);

  const DEFAULT_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
  ];

  const DEFAULT_MAIN_CATEGORIES = [
    "Groceries",
    "Household",
    "Personal Care",
    "Dining & Drinks",
    "Health & Pharmacy",
    "Pets",
    "Electronics & Home",
    "Other",
  ];

  const DEFAULT_SUBCATEGORIES = [
    "Dairy & Eggs",
    "Meat & Fish",
    "Produce (Fruits & Veg)",
    "Bakery & Bread",
    "Pantry & Staples",
    "Snacks & Sweets",
    "Beverages & Coffee",
    "Alcohol & Tobacco",
    "Bottle Deposit (SGR)",
    "Cleaning & Laundry",
    "Kitchen & Paper Goods",
    "Toiletries & Hygiene",
    "Medicine & Pharmacy",
    "Restaurants & Takeout",
    "Pet Food & Supplies",
    "Home & Misc",
    "Unreconciled Adjustment",
  ];

  const DEFAULT_SUBCATEGORY_TO_MAIN = {
    "Dairy & Eggs": "Groceries",
    "Meat & Fish": "Groceries",
    "Produce (Fruits & Veg)": "Groceries",
    "Bakery & Bread": "Groceries",
    "Pantry & Staples": "Groceries",
    "Snacks & Sweets": "Groceries",
    "Beverages & Coffee": "Groceries",
    "Alcohol & Tobacco": "Groceries",
    "Bottle Deposit (SGR)": "Groceries",
    "Cleaning & Laundry": "Household",
    "Kitchen & Paper Goods": "Household",
    "Toiletries & Hygiene": "Personal Care",
    "Medicine & Pharmacy": "Health & Pharmacy",
    "Restaurants & Takeout": "Dining & Drinks",
    "Pet Food & Supplies": "Pets",
    "Home & Misc": "Electronics & Home",
    "Unreconciled Adjustment": "Other",
  };

  const CATEGORY_COLORS = {
    Groceries: "#0f766e",
    Household: "#2563eb",
    "Personal Care": "#7c3aed",
    "Dining & Drinks": "#d97706",
    "Health & Pharmacy": "#dc2626",
    Pets: "#059669",
    "Electronics & Home": "#4f46e5",
    Other: "#64748b",
  };

  const state = {
    status: null,
    dashboard: null,
    selectedMonth: "all",
    breakdownMode: "main",
    activeTab: "items",
    searchQuery: "",
    categoryFilter: "all",
    statusFilter: "all",
    hasAutoOpenedSetup: false,
    isPollingStatus: false,
    lastProcessedTotal: null,
    lastSyncAt: null,
    isUploading: false,
    pendingDeleteReceiptId: null,
    lastDriveProfile: null,
  };

  // =========================================================================
  // Security & Formatting Helpers
  // =========================================================================

  function escapeHtml(value) {
    if (value === null || value === undefined) {
      return "";
    }
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;")
      .replace(/`/g, "&#96;");
  }

  function isSafeHttpUrl(url) {
    if (typeof url !== "string" || !url.trim()) {
      return false;
    }
    try {
      const parsed = new URL(url);
      return parsed.protocol === "https:" || parsed.protocol === "http:";
    } catch {
      return false;
    }
  }

  function formatMoney(value) {
    const num = Number(value);
    const safe = Number.isFinite(num) ? num : 0;
    return safe.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function formatInputDecimal(value, decimals = 2) {
    const num = Number(value);
    if (!Number.isFinite(num)) {
      return (0).toFixed(decimals);
    }
    return num.toFixed(decimals);
  }

  function formatQty(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) {
      return "1";
    }
    if (Number.isInteger(num)) {
      return String(num);
    }
    return num.toFixed(3).replace(/\.?0+$/, "");
  }

  function getTaxonomy() {
    const t = state.status && state.status.taxonomy ? state.status.taxonomy : {};
    return {
      main_categories:
        Array.isArray(t.main_categories) && t.main_categories.length
          ? t.main_categories
          : DEFAULT_MAIN_CATEGORIES,
      subcategories:
        Array.isArray(t.subcategories) && t.subcategories.length
          ? t.subcategories
          : DEFAULT_SUBCATEGORIES,
      subcategory_to_main:
        t.subcategory_to_main && Object.keys(t.subcategory_to_main).length
          ? t.subcategory_to_main
          : DEFAULT_SUBCATEGORY_TO_MAIN,
    };
  }

  function $(id) {
    return document.getElementById(id);
  }

  // =========================================================================
  // Toast & Inline Feedback Helpers
  // =========================================================================

  function showToast(message, level = "info", durationMs = 5000) {
    const container = $("toast-container");
    if (!container) return;

    const toast = document.createElement("div");
    toast.className = `toast toast--${level}`;
    toast.setAttribute("role", "status");

    const msgEl = document.createElement("span");
    msgEl.textContent = message;

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "toast-close";
    closeBtn.setAttribute("aria-label", "Dismiss notification");
    closeBtn.innerHTML = "&times;";
    closeBtn.addEventListener("click", () => toast.remove());

    toast.appendChild(msgEl);
    toast.appendChild(closeBtn);
    container.appendChild(toast);

    if (durationMs > 0) {
      window.setTimeout(() => {
        if (toast.parentNode) {
          toast.remove();
        }
      }, durationMs);
    }
  }

  function setInlineFeedback(elOrId, message, type = "success") {
    const el = typeof elOrId === "string" ? $(elOrId) : elOrId;
    if (!el) return;
    if (!message) {
      el.textContent = "";
      el.className = "inline-feedback hidden";
      return;
    }
    el.textContent = message;
    el.className = `inline-feedback inline-feedback--${type}`;
  }

  // =========================================================================
  // Network Layer
  // =========================================================================

  async function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (!headers.has("Accept")) {
      headers.set("Accept", "application/json");
    }

    const response = await fetch(url, { ...options, headers });
    let data = null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      try {
        data = await response.json();
      } catch {
        data = null;
      }
    } else {
      const text = await response.text();
      if (text) {
        try {
          data = JSON.parse(text);
        } catch {
          data = { detail: text };
        }
      }
    }

    if (!response.ok) {
      const detail =
        (data && (data.detail || data.message || data.error)) ||
        response.statusText ||
        `HTTP ${response.status}`;
      const err = new Error(detail);
      err.status = response.status;
      err.data = data;

      if (response.status === 423) {
        // Refresh status so lock warning banner appears immediately
        fetchStatus(false).catch(() => {});
      }
      throw err;
    }

    return data;
  }

  async function fetchStatus(refreshDrive = false) {
    const query = refreshDrive ? "?refresh_drive=true" : "?refresh_drive=false";
    const data = await apiFetch(`/api/status${query}`);
    state.status = data;
    renderStatus();
    return data;
  }

  async function fetchDashboard(month = state.selectedMonth) {
    const q = month && month !== "all" ? `?month=${encodeURIComponent(month)}` : "";
    const data = await apiFetch(`/api/dashboard${q}`);
    if (
      month &&
      month !== "all" &&
      Array.isArray(data.months) &&
      !data.months.includes(month)
    ) {
      state.selectedMonth = "all";
      return fetchDashboard("all");
    }
    state.dashboard = data;
    renderDashboard();
    return data;
  }

  // =========================================================================
  // Render: Header, Banners & Watcher Activity
  // =========================================================================

  function renderStatus() {
    const st = state.status;
    if (!st) return;

    const watcher = st.watcher || {};
    const excel = st.excel || {};
    const gemini = st.gemini || {};
    const drive = st.drive || {};

    // Header watcher status pill
    const pill = $("watcher-status-pill");
    const pillText = $("watcher-status-text");
    pill.className = "status-pill";

    if (excel.locked) {
      pill.classList.add("status-pill--warning");
      pillText.textContent = "Excel Locked";
    } else if (!st.setup_complete) {
      pill.classList.add("status-pill--warning");
      pillText.textContent = "Setup Incomplete";
    } else if (watcher.syncing) {
      pill.classList.add("status-pill--syncing");
      pillText.textContent = "Syncing Drive...";
    } else if (watcher.running) {
      pill.classList.add("status-pill--running");
      const interval = watcher.poll_interval_seconds || 20;
      pillText.textContent = `Watcher Running (${interval}s)`;
    } else {
      pill.classList.add("status-pill--paused");
      pillText.textContent = "Watcher Paused";
    }

    // Pause / Resume button
    const toggleLabel = $("toggle-watcher-label");
    toggleLabel.textContent = watcher.running ? "Pause" : "Resume";

    // Sync Now button disabled while syncing
    const syncBtn = $("btn-sync-now");
    if (syncBtn && !syncBtn.dataset.manualLoading) {
      syncBtn.disabled = Boolean(watcher.syncing);
    }

    // Setup badge
    const setupBadge = $("setup-incomplete-badge");
    setupBadge.classList.toggle("hidden", Boolean(st.setup_complete));

    // Excel lock banner
    const lockBanner = $("excel-lock-banner");
    const lockMsg = $("excel-lock-message");
    if (excel.locked) {
      const filename = excel.filename || "budget_tracker.xlsx";
      lockMsg.textContent = `${filename} is currently open in LibreOffice/Excel. Close it so pending receipts and edits can be saved.`;
      lockBanner.classList.remove("hidden");
    } else {
      lockBanner.classList.add("hidden");
    }

    // Populate category dropdown in explorer & edit item modal options if taxonomy loaded
    populateCategoryFilterOptions();
    populateSetupStatusBadges(gemini, drive);
    renderActivityFeed(watcher);
  }

  function populateSetupStatusBadges(gemini, drive) {
    // Step 1 badge & placeholder
    const geminiBadge = $("gemini-status-badge");
    const geminiKeyInput = $("gemini-api-key");
    if (gemini.configured) {
      const masked = gemini.masked_key || "Configured";
      geminiBadge.textContent = `Connected: ${masked}`;
      geminiBadge.className = "badge badge--success";
      geminiKeyInput.placeholder = `Current key: ${masked} (leave blank to keep)`;
    } else {
      geminiBadge.textContent = "Action Needed";
      geminiBadge.className = "badge badge--warning";
      geminiKeyInput.placeholder = "Paste your Gemini API key (e.g. AIza... or AQ...)";
    }

    // Step 2 Drive badges & cards
    const driveBadge = $("drive-status-badge");
    const connectedCard = $("drive-connected-card");
    const disconnectedCard = $("drive-disconnected-card");
    const folderName = drive.folder_name || "receipts";

    if (drive.connected) {
      if (drive.email || drive.display_name || drive.folder_url) {
        state.lastDriveProfile = {
          display_name: drive.display_name || "",
          email: drive.email || "",
          folder_url: drive.folder_url || "",
          folder_name: drive.folder_name || folderName,
        };
      }
      const profile = state.lastDriveProfile || {};
      const displayName = drive.display_name || profile.display_name || "Google Account";
      const email = drive.email || profile.email || "";
      const folderUrl = drive.folder_url || profile.folder_url || "";

      driveBadge.textContent = "Connected";
      driveBadge.className = "badge badge--success";
      connectedCard.classList.remove("hidden");
      disconnectedCard.classList.add("hidden");

      $("drive-user-name").textContent = displayName;
      $("drive-user-email").textContent = email ? `(${email})` : "";
      $("drive-folder-badge").textContent = folderName;
      $("drive-open-folder-name").textContent = folderName;

      const folderLink = $("drive-open-folder-link");
      if (isSafeHttpUrl(folderUrl)) {
        folderLink.href = folderUrl;
        folderLink.classList.remove("hidden");
      } else {
        folderLink.removeAttribute("href");
        folderLink.classList.add("hidden");
      }
    } else {
      state.lastDriveProfile = null;
      driveBadge.textContent = "Action Needed";
      driveBadge.className = "badge badge--warning";
      connectedCard.classList.add("hidden");
      disconnectedCard.classList.remove("hidden");

      const errorNote = $("drive-error-note");
      if (drive.error) {
        errorNote.textContent = drive.error;
        errorNote.classList.remove("hidden");
      } else {
        errorNote.textContent = "";
        errorNote.classList.add("hidden");
      }
    }

    // OAuth source badge
    const sourceBadge = $("oauth-source-badge");
    sourceBadge.textContent = drive.oauth_source || "builtin_default";
  }

  function syncSetupFormFields() {
    const st = state.status;
    if (!st) return;

    const gemini = st.gemini || {};
    const watcher = st.watcher || {};
    const drive = st.drive || {};

    // Populate available Gemini models
    const modelSelect = $("gemini-model-select");
    const models =
      Array.isArray(gemini.available_models) && gemini.available_models.length
        ? gemini.available_models
        : DEFAULT_MODELS;

    const activeModel = gemini.model || "gemini-3.8-flash";
    const uniqueModels = models.includes(activeModel) ? models : [activeModel, ...models];
    modelSelect.innerHTML = uniqueModels
      .map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`)
      .join("");
    modelSelect.value = activeModel;

    // Populate Step 3 general fields
    $("general-folder-name").value =
      watcher.drive_folder_name || drive.folder_name || "receipts";
    $("general-poll-interval").value = String(watcher.poll_interval_seconds || 20);
  }

  function renderActivityFeed(watcher) {
    const feed = $("activity-feed");
    const totalsEl = $("watcher-totals");
    const lastSyncEl = $("watcher-last-sync");

    const totals = watcher.totals || { processed: 0, duplicates: 0, failed: 0 };
    totalsEl.innerHTML = `
      <span class="mini-stat"><strong>${escapeHtml(totals.processed ?? 0)}</strong> added</span>
      <span class="mini-stat"><strong>${escapeHtml(totals.duplicates ?? 0)}</strong> dup</span>
      <span class="mini-stat"><strong>${escapeHtml(totals.failed ?? 0)}</strong> err</span>
    `;

    if (watcher.last_error) {
      lastSyncEl.textContent = `Last error: ${watcher.last_error}`;
    } else if (watcher.last_sync_at) {
      lastSyncEl.textContent = `Last Drive sync: ${watcher.last_sync_at}`;
    } else if (watcher.running) {
      lastSyncEl.textContent = `Polling every ${watcher.poll_interval_seconds || 20}s for "${watcher.drive_folder_name || "receipts"}"`;
    } else {
      lastSyncEl.textContent = "Watcher is currently paused";
    }

    // watcher.events is stored newest-first on the backend (deque.appendleft)
    const events = Array.isArray(watcher.events) ? watcher.events : [];
    if (!events.length) {
      feed.innerHTML = `<div class="empty-state-compact">No watcher events recorded yet.</div>`;
      return;
    }

    const levelBadgeMap = {
      success: "badge--success",
      warning: "badge--warning",
      error: "badge--error",
      info: "badge--info",
    };

    feed.innerHTML = events
      .map((ev) => {
        const level = String(ev.level || "info").toLowerCase();
        const badgeClass = levelBadgeMap[level] || "badge--neutral";
        return `
          <div class="activity-item">
            <span class="badge ${escapeHtml(badgeClass)}">${escapeHtml(level)}</span>
            <span class="activity-time">${escapeHtml(ev.timestamp || "")}</span>
            <span class="activity-msg">${escapeHtml(ev.message || "")}</span>
          </div>
        `;
      })
      .join("");
  }

  // =========================================================================
  // Render: Dashboard KPIs, Analytics & Tables
  // =========================================================================

  function renderDashboard() {
    const dash = state.dashboard;
    if (!dash) return;

    renderMonthFilterOptions(dash.months || []);
    renderKpis(dash.kpis || {});
    renderCategoryBreakdown();
    renderMonthlyTrend(dash.by_month || []);
    renderTopStores(dash.by_store || []);
    renderExplorer();

    const receipts = Array.isArray(dash.receipts) ? dash.receipts : [];
    const items = Array.isArray(dash.items) ? dash.items : [];
    const periodText = state.selectedMonth === "all" ? "All Months" : state.selectedMonth;
    $("dashboard-meta-info").textContent = `${periodText} · ${receipts.length} receipts · ${items.length} line items`;
  }

  function renderMonthFilterOptions(months) {
    const select = $("month-filter");
    const sortedMonths = [...months].sort().reverse();
    const key = JSON.stringify(sortedMonths);
    if (select.dataset.renderedMonths === key) {
      if (state.selectedMonth !== "all" && !months.includes(state.selectedMonth)) {
        state.selectedMonth = "all";
      }
      select.value = state.selectedMonth;
      return;
    }

    const prevValue = state.selectedMonth;
    const options = [`<option value="all">All Months</option>`];
    for (const m of sortedMonths) {
      options.push(`<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`);
    }
    select.innerHTML = options.join("");
    select.dataset.renderedMonths = key;

    if (prevValue !== "all" && !months.includes(prevValue)) {
      state.selectedMonth = "all";
    }
    select.value = state.selectedMonth;
  }

  function renderKpis(kpis) {
    $("kpi-total-spent").textContent = formatMoney(kpis.total_spent);
    $("kpi-month-spent").textContent = formatMoney(kpis.current_month_spent);
    $("kpi-month-label").textContent = kpis.current_month
      ? `Active period: ${kpis.current_month}`
      : "No monthly spend yet";
    $("kpi-discounts-saved").textContent = formatMoney(kpis.total_discounts);
    $("kpi-sgr-deposits").textContent = formatMoney(kpis.total_sgr);

    const receiptCount = Number(kpis.receipt_count || 0);
    const itemsCount = Number(kpis.items_count || 0);
    const needsReviewCount = Number(kpis.needs_review_count || 0);

    $("kpi-receipts-count").textContent = String(receiptCount);
    $("kpi-items-count-pill").textContent = `${itemsCount} ${itemsCount === 1 ? "item" : "items"}`;

    const nrBtn = $("kpi-needs-review-btn");
    const nrText = $("kpi-needs-review-text");
    if (needsReviewCount > 0) {
      nrBtn.className = "needs-review-pill needs-review-pill--alert";
      nrText.textContent = `${needsReviewCount} Needs Review`;
    } else {
      nrBtn.className = "needs-review-pill needs-review-pill--ok";
      nrText.textContent = "0 Needs Review";
    }
  }

  function renderCategoryBreakdown() {
    const container = $("category-breakdown-list");
    const dash = state.dashboard;
    if (!dash) return;

    if (state.breakdownMode === "main") {
      const rows = Array.isArray(dash.by_main_category) ? dash.by_main_category : [];
      if (!rows.length) {
        container.innerHTML = `<div class="empty-state-compact">No category spending data for this period.</div>`;
        return;
      }

      container.innerHTML = rows
        .map((row) => {
          const cat = row.main_category || "Other";
          const total = Number(row.total || 0);
          const rawShare = Number(row.share || 0);
          const pct = rawShare <= 1 ? rawShare * 100 : rawShare;
          const barWidth = total > 0 ? Math.max(2, Math.min(100, pct)) : 0;
          const color = CATEGORY_COLORS[cat] || "#0f766e";

          return `
            <div class="breakdown-row">
              <div class="breakdown-row-head">
                <span class="breakdown-label">${escapeHtml(cat)}</span>
                <span class="breakdown-numbers">
                  <strong>${escapeHtml(formatMoney(total))} RON</strong> · ${escapeHtml(pct.toFixed(1))}%
                </span>
              </div>
              <div class="breakdown-track">
                <div class="breakdown-fill" style="width: ${barWidth.toFixed(1)}%; background-color: ${escapeHtml(color)};"></div>
              </div>
            </div>
          `;
        })
        .join("");
      return;
    }

    // Subcategories mode
    const subRows = Array.isArray(dash.by_subcategory) ? dash.by_subcategory : [];
    if (!subRows.length) {
      container.innerHTML = `<div class="empty-state-compact">No subcategory spending data for this period.</div>`;
      return;
    }

    const subGrandTotal = subRows.reduce((acc, r) => acc + Math.max(0, Number(r.total || 0)), 0);
    container.innerHTML = subRows
      .map((row) => {
        const sub = row.subcategory || "Home & Misc";
        const main = row.main_category || "Other";
        const total = Number(row.total || 0);
        const pct =
          typeof row.share === "number"
            ? row.share <= 1
              ? row.share * 100
              : row.share
            : subGrandTotal > 0
              ? (Math.max(0, total) / subGrandTotal) * 100
              : 0;
        const barWidth = total > 0 ? Math.max(2, Math.min(100, pct)) : 0;
        const color = CATEGORY_COLORS[main] || "#0f766e";

        return `
          <div class="breakdown-row">
            <div class="breakdown-row-head">
              <span class="breakdown-label">
                ${escapeHtml(sub)}
                <span class="breakdown-parent-tag">${escapeHtml(main)}</span>
              </span>
              <span class="breakdown-numbers">
                <strong>${escapeHtml(formatMoney(total))} RON</strong> · ${escapeHtml(pct.toFixed(1))}%
              </span>
            </div>
            <div class="breakdown-track">
              <div class="breakdown-fill" style="width: ${barWidth.toFixed(1)}%; background-color: ${escapeHtml(color)};"></div>
            </div>
          </div>
        `;
      })
      .join("");
  }

  function renderMonthlyTrend(byMonth) {
    const chartEl = $("monthly-trend-chart");
    const badgeEl = $("trend-months-badge");
    badgeEl.textContent = `${byMonth.length} ${byMonth.length === 1 ? "month" : "months"}`;

    if (!byMonth.length) {
      chartEl.innerHTML = `<div class="empty-state-compact">No monthly trend data yet.</div>`;
      return;
    }

    const maxSpent = Math.max(...byMonth.map((m) => Number(m.total_spent || 0)), 1);
    chartEl.innerHTML = byMonth
      .map((m) => {
        const month = m.month || "";
        const spent = Number(m.total_spent || 0);
        const count = Number(m.receipt_count || 0);
        const heightPct = spent > 0 ? Math.max(6, Math.round((spent / maxSpent) * 100)) : 4;
        const isSelected = state.selectedMonth === month;

        return `
          <button
            type="button"
            class="month-bar-col ${isSelected ? "is-selected" : ""}"
            data-month="${escapeHtml(month)}"
            title="${escapeHtml(month)}: ${escapeHtml(formatMoney(spent))} RON (${escapeHtml(count)} receipts)"
          >
            <span class="month-bar-value">${escapeHtml(formatMoney(spent))}</span>
            <span class="month-bar-track">
              <span class="month-bar" style="height: ${heightPct}%;"></span>
            </span>
            <span class="month-bar-label">${escapeHtml(month)}</span>
            <span class="month-bar-sub">${escapeHtml(count)} rcpt</span>
          </button>
        `;
      })
      .join("");
  }

  function renderTopStores(byStore) {
    const tbody = $("top-stores-tbody");
    if (!byStore.length) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty-cell">No stores logged yet.</td></tr>`;
      return;
    }

    const topStores = byStore.slice(0, 8);
    tbody.innerHTML = topStores
      .map((s) => {
        return `
          <tr>
            <td><strong>${escapeHtml(s.store_name || "Unknown Store")}</strong></td>
            <td class="num">${escapeHtml(s.receipt_count ?? 0)}</td>
            <td class="num">${escapeHtml(formatMoney(s.average_trip))}</td>
            <td class="num"><strong>${escapeHtml(formatMoney(s.total_spent))}</strong></td>
          </tr>
        `;
      })
      .join("");
  }

  function populateCategoryFilterOptions() {
    const select = $("explorer-category-filter");
    const taxonomy = getTaxonomy();
    const key = JSON.stringify(taxonomy.main_categories);
    if (select.dataset.renderedCats === key) {
      return;
    }

    const prev = state.categoryFilter;
    const opts = [`<option value="all">All Categories</option>`];
    for (const cat of taxonomy.main_categories) {
      opts.push(`<option value="${escapeHtml(cat)}">${escapeHtml(cat)}</option>`);
    }
    select.innerHTML = opts.join("");
    select.dataset.renderedCats = key;

    if (prev !== "all" && taxonomy.main_categories.includes(prev)) {
      select.value = prev;
    } else {
      select.value = "all";
      state.categoryFilter = "all";
    }
  }

  function renderExplorer() {
    const dash = state.dashboard;
    if (!dash) return;

    const receipts = Array.isArray(dash.receipts) ? dash.receipts : [];
    const items = Array.isArray(dash.items) ? dash.items : [];

    $("tab-items-count").textContent = String(items.length);
    $("tab-receipts-count").textContent = String(receipts.length);

    const receiptStatusMap = new Map();
    for (const r of receipts) {
      receiptStatusMap.set(r.receipt_id, r.status || "OK");
    }

    const receiptCategorySetMap = new Map();
    for (const it of items) {
      if (!receiptCategorySetMap.has(it.receipt_id)) {
        receiptCategorySetMap.set(it.receipt_id, new Set());
      }
      receiptCategorySetMap.get(it.receipt_id).add(it.main_category || "Other");
    }

    const q = state.searchQuery.trim().toLowerCase();
    const catFilter = state.categoryFilter;
    const statusFilter = state.statusFilter;

    const hasActiveFilters = Boolean(q || catFilter !== "all" || statusFilter !== "all");
    $("btn-clear-explorer-filters").classList.toggle("hidden", !hasActiveFilters);

    const filteredItems = items.filter((item) => {
      if (catFilter !== "all" && item.main_category !== catFilter) {
        return false;
      }
      const parentStatus = receiptStatusMap.get(item.receipt_id) || "OK";
      if (statusFilter !== "all" && parentStatus !== statusFilter) {
        return false;
      }
      if (q) {
        const haystack = [
          item.receipt_id,
          item.store_name,
          item.clean_name,
          item.raw_name,
          item.main_category,
          item.subcategory,
          item.date,
        ]
          .join(" ")
          .toLowerCase();
        if (!haystack.includes(q)) {
          return false;
        }
      }
      return true;
    });

    const filteredReceipts = receipts.filter((receipt) => {
      if (statusFilter !== "all" && (receipt.status || "OK") !== statusFilter) {
        return false;
      }
      if (catFilter !== "all") {
        const cats = receiptCategorySetMap.get(receipt.receipt_id);
        if (!cats || !cats.has(catFilter)) {
          return false;
        }
      }
      if (q) {
        const haystack = [
          receipt.receipt_id,
          receipt.store_name,
          receipt.date,
          receipt.payment_method,
          receipt.status,
          receipt.source_file,
        ]
          .join(" ")
          .toLowerCase();
        if (!haystack.includes(q)) {
          return false;
        }
      }
      return true;
    });

    // Toggle tab visibility
    const showItems = state.activeTab === "items";
    $("items-table-wrapper").classList.toggle("hidden", !showItems);
    $("receipts-table-wrapper").classList.toggle("hidden", showItems);
    $("tab-btn-items").classList.toggle("is-active", showItems);
    $("tab-btn-items").setAttribute("aria-selected", String(showItems));
    $("tab-btn-receipts").classList.toggle("is-active", !showItems);
    $("tab-btn-receipts").setAttribute("aria-selected", String(!showItems));

    if (showItems) {
      $("explorer-showing-count").textContent = `Showing ${filteredItems.length} of ${items.length} line items`;
    } else {
      $("explorer-showing-count").textContent = `Showing ${filteredReceipts.length} of ${receipts.length} receipts`;
    }

    renderItemsTable(filteredItems);
    renderReceiptsTable(filteredReceipts);
  }

  function renderItemsTable(items) {
    const tbody = $("items-tbody");
    if (!items.length) {
      tbody.innerHTML = `<tr><td colspan="11" class="empty-cell">No matching line items found.</td></tr>`;
      return;
    }

    tbody.innerHTML = items
      .map((item) => {
        const isAdjustment = item.subcategory === "Unreconciled Adjustment";
        const rowClass = isAdjustment ? "row-warning" : "";
        return `
          <tr class="${rowClass}">
            <td><span class="mono-id">${escapeHtml(item.receipt_id)}</span></td>
            <td>${escapeHtml(item.date || "")}</td>
            <td>${escapeHtml(item.store_name || "")}</td>
            <td>
              <div class="product-cell-clean">${escapeHtml(item.clean_name || "")}</div>
              <div class="product-cell-raw">${escapeHtml(item.raw_name || "")}</div>
            </td>
            <td><span class="badge badge--neutral">${escapeHtml(item.main_category || "Other")}</span></td>
            <td>${escapeHtml(item.subcategory || "")}</td>
            <td class="num">${escapeHtml(formatQty(item.quantity))} ${escapeHtml(item.unit || "buc")}</td>
            <td class="num">${escapeHtml(formatMoney(item.unit_price))}</td>
            <td class="num">${escapeHtml(formatMoney(item.discount_amount))}</td>
            <td class="num"><strong>${escapeHtml(formatMoney(item.net_total))}</strong></td>
            <td class="actions-col">
              <button
                type="button"
                class="btn btn-secondary btn-xs"
                data-action="edit-item"
                data-receipt-id="${escapeHtml(item.receipt_id)}"
                data-item-index="${escapeHtml(item.item_index)}"
              >
                Edit
              </button>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  function renderReceiptsTable(receipts) {
    const tbody = $("receipts-tbody");
    if (!receipts.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="empty-cell">No matching receipts found.</td></tr>`;
      return;
    }

    tbody.innerHTML = receipts
      .map((r) => {
        const status = r.status || "OK";
        const isReview = status === "Needs Review";
        const statusBadge = isReview ? "badge--warning" : "badge--success";
        const rowClass = isReview ? "row-warning" : "";
        const dateTime = r.time ? `${r.date || ""} ${r.time}` : r.date || "";

        return `
          <tr class="${rowClass}">
            <td><span class="mono-id">${escapeHtml(r.receipt_id)}</span></td>
            <td>${escapeHtml(dateTime)}</td>
            <td><strong>${escapeHtml(r.store_name || "Unknown Store")}</strong></td>
            <td>${escapeHtml(r.payment_method || "Unknown")}</td>
            <td class="num">${escapeHtml(r.items_count ?? 0)}</td>
            <td class="num">${escapeHtml(formatMoney(r.discounts_saved))}</td>
            <td class="num">${escapeHtml(formatMoney(r.sgr_deposit))}</td>
            <td class="num"><strong>${escapeHtml(formatMoney(r.total_paid))} ${escapeHtml(r.currency || "RON")}</strong></td>
            <td><span class="badge ${statusBadge}">${escapeHtml(status)}</span></td>
            <td class="actions-col">
              <div class="row-actions">
                <button
                  type="button"
                  class="btn btn-secondary btn-xs"
                  data-action="edit-receipt"
                  data-receipt-id="${escapeHtml(r.receipt_id)}"
                >
                  Edit
                </button>
                <button
                  type="button"
                  class="btn btn-danger-outline btn-xs"
                  data-action="delete-receipt"
                  data-receipt-id="${escapeHtml(r.receipt_id)}"
                >
                  Delete
                </button>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  // =========================================================================
  // Modals Open / Close Helpers
  // =========================================================================

  function openModal(modalId) {
    const modal = $(modalId);
    if (!modal) return;
    modal.classList.remove("hidden");
    document.body.style.overflow = "hidden";
  }

  function closeModal(modalId) {
    const modal = $(modalId);
    if (!modal) return;
    modal.classList.add("hidden");
    const anyOpen = document.querySelectorAll(".modal-backdrop:not(.hidden)").length > 0;
    if (!anyOpen) {
      document.body.style.overflow = "";
    }
  }

  function openSetupModal(shouldRefreshDrive = true) {
    syncSetupFormFields();
    setInlineFeedback("gemini-feedback", "");
    setInlineFeedback("general-feedback", "");
    setInlineFeedback("credentials-feedback", "");
    openModal("setup-modal");
    if (shouldRefreshDrive) {
      fetchStatus(true).catch(() => {});
    }
  }

  // =========================================================================
  // Setup & Watcher Actions
  // =========================================================================

  async function handleSaveGemini(event) {
    event.preventDefault();
    const btn = $("btn-save-gemini");
    const apiKey = $("gemini-api-key").value.trim();
    const model = $("gemini-model-select").value;

    setInlineFeedback("gemini-feedback", "");
    btn.disabled = true;
    btn.textContent = "Verifying & Saving...";

    try {
      const res = await apiFetch("/api/settings/gemini", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_key: apiKey,
          model,
          verify: true,
        }),
      });
      $("gemini-api-key").value = "";
      await fetchStatus(false);
      syncSetupFormFields();
      const msg = `Gemini verified and saved (${res.masked_key || "key active"} · ${res.model}).`;
      setInlineFeedback("gemini-feedback", msg, "success");
      showToast(msg, "success");
    } catch (err) {
      setInlineFeedback("gemini-feedback", err.message || "Failed to verify Gemini API key.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Verify & Save Gemini Key";
    }
  }

  async function handleSaveGeneral(event) {
    event.preventDefault();
    const btn = $("btn-save-general");
    const driveFolderName = $("general-folder-name").value.trim();
    const pollInterval = Number($("general-poll-interval").value);
    const geminiModel = $("gemini-model-select").value;

    setInlineFeedback("general-feedback", "");
    btn.disabled = true;
    btn.textContent = "Saving...";

    try {
      await apiFetch("/api/settings/general", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          drive_folder_name: driveFolderName,
          poll_interval_seconds: pollInterval,
          gemini_model: geminiModel,
        }),
      });
      await fetchStatus(true);
      syncSetupFormFields();
      setInlineFeedback(
        "general-feedback",
        `Saved: watching "${driveFolderName}" every ${pollInterval}s.`,
        "success"
      );
      showToast("Automation settings saved.", "success");
    } catch (err) {
      setInlineFeedback("general-feedback", err.message || "Could not save settings.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Save Automation Settings";
    }
  }

  async function handleUploadCredentials() {
    const input = $("credentials-json-input");
    const btn = $("btn-upload-credentials");
    const file = input.files && input.files[0];

    setInlineFeedback("credentials-feedback", "");
    if (!file) {
      setInlineFeedback("credentials-feedback", "Select a credentials.json file first.", "error");
      return;
    }

    const formData = new FormData();
    formData.append("file", file);

    btn.disabled = true;
    btn.textContent = "Uploading...";
    try {
      const res = await apiFetch("/api/settings/credentials-json", {
        method: "POST",
        body: formData,
      });
      input.value = "";
      await fetchStatus(false);
      setInlineFeedback(
        "credentials-feedback",
        `Custom credentials.json saved (client: ${res.client_id || "custom"}).`,
        "success"
      );
      showToast("Custom OAuth credentials uploaded.", "success");
    } catch (err) {
      setInlineFeedback("credentials-feedback", err.message || "Invalid credentials.json file.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Upload credentials.json";
    }
  }

  async function handleDisconnectDrive() {
    const btn = $("btn-disconnect-drive");
    setInlineFeedback("drive-feedback", "");
    btn.disabled = true;
    btn.textContent = "Disconnecting...";

    try {
      await apiFetch("/api/auth/google/disconnect", { method: "POST" });
      await fetchStatus(true);
      setInlineFeedback("drive-feedback", "Google Drive disconnected.", "success");
      showToast("Google Drive disconnected.", "info");
    } catch (err) {
      setInlineFeedback("drive-feedback", err.message || "Failed to disconnect.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Disconnect Account";
    }
  }

  async function handleToggleWatcher() {
    const btn = $("btn-toggle-watcher");
    const running = Boolean(state.status && state.status.watcher && state.status.watcher.running);
    const endpoint = running ? "/api/watcher/pause" : "/api/watcher/start";

    btn.disabled = true;
    try {
      await apiFetch(endpoint, { method: "POST" });
      await fetchStatus(false);
      showToast(running ? "Drive watcher paused." : "Drive watcher started.", "info");
    } catch (err) {
      showToast(err.message || "Could not update watcher state.", "error");
    } finally {
      btn.disabled = false;
    }
  }

  async function handleSyncNow() {
    const btn = $("btn-sync-now");
    btn.dataset.manualLoading = "1";
    btn.disabled = true;
    const labelSpan = btn.querySelector("span");
    const origText = labelSpan ? labelSpan.textContent : "Sync Drive Now";
    if (labelSpan) labelSpan.textContent = "Syncing...";

    try {
      const res = await apiFetch("/api/watcher/sync-now", { method: "POST" });
      await Promise.all([fetchStatus(false), fetchDashboard(state.selectedMonth)]);
      const stats = res.stats || {};
      const added = Number(stats.processed || 0);
      const dups = Number(stats.duplicates || 0);
      const failed = Number(stats.failed || 0);
      showToast(
        `Drive sync finished: ${added} added, ${dups} duplicates, ${failed} failed.`,
        failed > 0 ? "warning" : "success"
      );
    } catch (err) {
      if (err.status === 401) {
        showToast("Google Drive is not connected yet. Sign in via Setup & Connections.", "warning");
        openSetupModal();
      } else {
        showToast(err.message || "Drive sync failed.", "error");
      }
    } finally {
      delete btn.dataset.manualLoading;
      if (labelSpan) labelSpan.textContent = origText;
      btn.disabled = Boolean(state.status && state.status.watcher && state.status.watcher.syncing);
    }
  }

  // =========================================================================
  // Drag-and-Drop Local Receipt Upload
  // =========================================================================

  function isSupportedUploadFile(file) {
    const name = (file && file.name ? file.name : "").toLowerCase();
    const dotIdx = name.lastIndexOf(".");
    if (dotIdx === -1) return false;
    return ALLOWED_EXTENSIONS.has(name.slice(dotIdx));
  }

  async function uploadReceiptFiles(fileList) {
    if (state.isUploading) return;
    const rawFiles = Array.from(fileList || []);
    if (!rawFiles.length) return;

    const validFiles = [];
    const invalidNames = [];
    for (const f of rawFiles) {
      if (isSupportedUploadFile(f)) {
        validFiles.push(f);
      } else {
        invalidNames.push(f.name);
      }
    }

    if (invalidNames.length) {
      showToast(
        `Skipped unsupported file(s): ${invalidNames.join(", ")}. Allowed: JPG, PNG, WEBP, HEIC, PDF.`,
        "warning"
      );
    }

    if (!validFiles.length) return;

    const formData = new FormData();
    for (const file of validFiles) {
      formData.append("files", file);
    }

    state.isUploading = true;
    const progressEl = $("upload-progress");
    const progressText = $("upload-progress-text");
    progressText.textContent = `Extracting ${validFiles.length} ${validFiles.length === 1 ? "receipt" : "receipts"} with Gemini Vision...`;
    progressEl.classList.remove("hidden");

    try {
      const data = await apiFetch("/api/receipts/upload", {
        method: "POST",
        body: formData,
      });
      const results = Array.isArray(data.results) ? data.results : [];
      renderUploadResults(results);

      const addedCount = results.filter((r) => r.status === "added").length;
      const failedCount = results.filter((r) => r.status === "failed").length;
      if (addedCount > 0) {
        showToast(`Processed ${addedCount} new ${addedCount === 1 ? "receipt" : "receipts"}.`, "success");
      } else if (failedCount > 0) {
        showToast(`${failedCount} uploaded file(s) failed extraction.`, "error");
      } else {
        showToast("Upload checked — all files were already logged.", "info");
      }

      await Promise.all([fetchStatus(false), fetchDashboard(state.selectedMonth)]);
    } catch (err) {
      showToast(err.message || "Receipt upload failed.", "error");
    } finally {
      state.isUploading = false;
      progressEl.classList.add("hidden");
      $("upload-file-input").value = "";
    }
  }

  function renderUploadResults(results) {
    const wrapper = $("upload-results-wrapper");
    const list = $("upload-results");

    if (!results.length) {
      wrapper.classList.add("hidden");
      list.innerHTML = "";
      return;
    }

    wrapper.classList.remove("hidden");
    list.innerHTML = results
      .map((res) => {
        const status = res.status || "failed";
        let badgeClass = "badge--error";
        let badgeLabel = "Failed";
        let detailText = res.error || "Extraction failed.";

        if (status === "added") {
          badgeClass = res.receipt_status === "Needs Review" ? "badge--warning" : "badge--success";
          badgeLabel = res.receipt_status === "Needs Review" ? "Added · Review" : "Added";
          detailText = `${res.receipt_id || ""} · ${res.store_name || "Store"} · ${res.receipt_date || ""} · ${formatMoney(res.total_amount)} ${res.currency || "RON"} (${res.items_count || 0} items)`;
        } else if (status === "duplicate") {
          badgeClass = "badge--neutral";
          badgeLabel = "Duplicate";
          detailText = res.error || "Already logged in budget_tracker.xlsx.";
        }

        return `
          <li class="upload-result-item">
            <div class="upload-result-main">
              <div class="upload-result-file">${escapeHtml(res.filename || "receipt")}</div>
              <div class="upload-result-detail">${escapeHtml(detailText)}</div>
            </div>
            <span class="badge ${badgeClass}">${escapeHtml(badgeLabel)}</span>
          </li>
        `;
      })
      .join("");
  }

  // =========================================================================
  // Edit Receipt, Edit Item & Delete Receipt Flows
  // =========================================================================

  function openEditReceiptModal(receiptId) {
    const receipts = (state.dashboard && state.dashboard.receipts) || [];
    const receipt = receipts.find((r) => r.receipt_id === receiptId);
    if (!receipt) return;

    $("edit-receipt-id").value = receipt.receipt_id;
    $("edit-receipt-id-label").textContent = receipt.receipt_id;
    $("edit-receipt-store").value = receipt.store_name || "";
    $("edit-receipt-date").value = receipt.date || "";
    $("edit-receipt-time").value = receipt.time || "";

    const paymentSelect = $("edit-receipt-payment");
    const payVal = receipt.payment_method || "Unknown";
    if (!Array.from(paymentSelect.options).some((o) => o.value === payVal)) {
      const opt = document.createElement("option");
      opt.value = payVal;
      opt.textContent = payVal;
      paymentSelect.appendChild(opt);
    }
    paymentSelect.value = payVal;
    $("edit-receipt-status").value = receipt.status === "Needs Review" ? "Needs Review" : "OK";

    setInlineFeedback("edit-receipt-error", "");
    openModal("edit-receipt-modal");
  }

  async function handleSaveReceipt(event) {
    event.preventDefault();
    const btn = $("btn-save-receipt");
    const receiptId = $("edit-receipt-id").value;
    const payload = {
      store_name: $("edit-receipt-store").value.trim(),
      date: $("edit-receipt-date").value.trim(),
      time: $("edit-receipt-time").value.trim(),
      payment_method: $("edit-receipt-payment").value,
      status: $("edit-receipt-status").value,
    };

    setInlineFeedback("edit-receipt-error", "");
    btn.disabled = true;
    btn.textContent = "Saving...";

    try {
      await apiFetch(`/api/receipts/${encodeURIComponent(receiptId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      closeModal("edit-receipt-modal");
      showToast(`Receipt ${receiptId} updated.`, "success");
      await Promise.all([fetchDashboard(state.selectedMonth), fetchStatus(false)]);
    } catch (err) {
      setInlineFeedback("edit-receipt-error", err.message || "Could not save receipt.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Save Receipt";
    }
  }

  function openEditItemModal(receiptId, itemIndex) {
    const items = (state.dashboard && state.dashboard.items) || [];
    const item = items.find(
      (it) => it.receipt_id === receiptId && Number(it.item_index) === Number(itemIndex)
    );
    if (!item) return;

    const taxonomy = getTaxonomy();
    const mainSelect = $("edit-item-main-category");
    const subSelect = $("edit-item-subcategory");

    const mainOpts = taxonomy.main_categories.includes(item.main_category)
      ? taxonomy.main_categories
      : [item.main_category, ...taxonomy.main_categories].filter(Boolean);
    mainSelect.innerHTML = mainOpts
      .map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`)
      .join("");

    const subOpts = taxonomy.subcategories.includes(item.subcategory)
      ? taxonomy.subcategories
      : [item.subcategory, ...taxonomy.subcategories].filter(Boolean);
    subSelect.innerHTML = subOpts
      .map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`)
      .join("");

    $("edit-item-receipt-id").value = item.receipt_id;
    $("edit-item-index").value = String(item.item_index);
    $("edit-item-context").textContent = `${item.receipt_id} · ${item.store_name || ""} · ${item.date || ""}`;
    $("edit-item-clean-name").value = item.clean_name || "";
    $("edit-item-raw-name").value = item.raw_name || "";
    mainSelect.value = item.main_category || "Other";
    subSelect.value = item.subcategory || "Home & Misc";
    $("edit-item-quantity").value = formatQty(item.quantity);
    $("edit-item-unit").value = item.unit || "buc";
    $("edit-item-unit-price").value = formatInputDecimal(item.unit_price, 2);
    $("edit-item-discount").value = formatInputDecimal(item.discount_amount, 2);
    $("edit-item-net-total").value = formatInputDecimal(item.net_total, 2);

    setInlineFeedback("edit-item-error", "");
    openModal("edit-item-modal");
  }

  function recalculateItemNetTotal() {
    const qty = Number($("edit-item-quantity").value);
    const unitPrice = Number($("edit-item-unit-price").value);
    const discount = Number($("edit-item-discount").value);

    if (!Number.isFinite(qty) || !Number.isFinite(unitPrice) || !Number.isFinite(discount)) {
      return;
    }
    const netTotal = Math.round((qty * unitPrice - discount) * 100) / 100;
    $("edit-item-net-total").value = netTotal.toFixed(2);
  }

  async function handleSaveItem(event) {
    event.preventDefault();
    const btn = $("btn-save-item");
    const receiptId = $("edit-item-receipt-id").value;
    const itemIndex = Number($("edit-item-index").value);

    const payload = {
      clean_name: $("edit-item-clean-name").value.trim(),
      raw_name: $("edit-item-raw-name").value.trim(),
      main_category: $("edit-item-main-category").value,
      subcategory: $("edit-item-subcategory").value,
      quantity: Number($("edit-item-quantity").value),
      unit: $("edit-item-unit").value.trim() || "buc",
      unit_price: Number($("edit-item-unit-price").value),
      discount_amount: Number($("edit-item-discount").value),
      net_total: Number($("edit-item-net-total").value),
      recalculate_receipt_total: true,
    };

    setInlineFeedback("edit-item-error", "");
    btn.disabled = true;
    btn.textContent = "Saving...";

    try {
      await apiFetch(
        `/api/receipts/${encodeURIComponent(receiptId)}/items/${encodeURIComponent(itemIndex)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }
      );
      closeModal("edit-item-modal");
      showToast(`Line item on ${receiptId} updated.`, "success");
      await Promise.all([fetchDashboard(state.selectedMonth), fetchStatus(false)]);
    } catch (err) {
      setInlineFeedback("edit-item-error", err.message || "Could not update line item.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Save Line Item";
    }
  }

  function openDeleteReceiptModal(receiptId) {
    const receipts = (state.dashboard && state.dashboard.receipts) || [];
    const receipt = receipts.find((r) => r.receipt_id === receiptId);
    state.pendingDeleteReceiptId = receiptId;

    const summaryEl = $("delete-receipt-summary");
    if (receipt) {
      summaryEl.innerHTML = `Are you sure you want to permanently delete receipt <strong>${escapeHtml(receipt.receipt_id)}</strong> from <strong>${escapeHtml(receipt.store_name || "Unknown Store")}</strong> (${escapeHtml(receipt.date || "")}, ${escapeHtml(formatMoney(receipt.total_paid))} ${escapeHtml(receipt.currency || "RON")})?`;
    } else {
      summaryEl.innerHTML = `Are you sure you want to permanently delete receipt <strong>${escapeHtml(receiptId)}</strong>?`;
    }

    setInlineFeedback("delete-receipt-error", "");
    openModal("delete-receipt-modal");
  }

  async function handleConfirmDeleteReceipt() {
    const receiptId = state.pendingDeleteReceiptId;
    if (!receiptId) return;

    const btn = $("btn-confirm-delete-receipt");
    setInlineFeedback("delete-receipt-error", "");
    btn.disabled = true;
    btn.textContent = "Deleting...";

    try {
      await apiFetch(`/api/receipts/${encodeURIComponent(receiptId)}`, {
        method: "DELETE",
      });
      state.pendingDeleteReceiptId = null;
      closeModal("delete-receipt-modal");
      showToast(`Receipt ${receiptId} deleted.`, "info");
      await Promise.all([fetchDashboard(state.selectedMonth), fetchStatus(false)]);
    } catch (err) {
      setInlineFeedback("delete-receipt-error", err.message || "Could not delete receipt.", "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Delete Receipt";
    }
  }

  // =========================================================================
  // Auth Query Parameter Handling & Background Polling
  // =========================================================================

  function parseInitialAuthQueryParams() {
    const params = new URLSearchParams(window.location.search);
    const authResult = params.get("auth");
    const authError = params.get("auth_error");

    if (!authResult && !params.has("auth_error")) {
      return null;
    }

    // Clean URL immediately without reloading page
    const cleanUrl = window.location.pathname + window.location.hash;
    window.history.replaceState({}, document.title, cleanUrl);

    if (authResult === "google_connected") {
      return { type: "google_connected" };
    }
    return {
      type: "auth_error",
      message: authError || "Google OAuth sign-in did not complete.",
    };
  }

  async function pollStatusTick() {
    if (document.hidden || state.isPollingStatus) return;
    state.isPollingStatus = true;
    try {
      const prevProcessed = state.lastProcessedTotal;
      const prevSyncAt = state.lastSyncAt;
      const prevLocked = state.status && state.status.excel ? state.status.excel.locked : false;

      const st = await fetchStatus(false);
      const watcher = st.watcher || {};
      const totals = watcher.totals || {};
      const nextProcessed = Number(totals.processed || 0);
      const nextSyncAt = watcher.last_sync_at || null;
      const nextLocked = Boolean(st.excel && st.excel.locked);

      state.lastProcessedTotal = nextProcessed;
      state.lastSyncAt = nextSyncAt;

      if (
        (prevProcessed !== null && nextProcessed !== prevProcessed) ||
        (prevSyncAt !== null && nextSyncAt && nextSyncAt !== prevSyncAt && !watcher.syncing) ||
        (prevLocked && !nextLocked)
      ) {
        await fetchDashboard(state.selectedMonth);
      }
    } catch {
      // Quietly ignore transient poll errors while server restarts
    } finally {
      state.isPollingStatus = false;
    }
  }

  // =========================================================================
  // Event Listeners & Initialization
  // =========================================================================

  function bindEvents() {
    // Header buttons
    $("btn-open-setup").addEventListener("click", () => openSetupModal(true));
    $("btn-toggle-watcher").addEventListener("click", handleToggleWatcher);
    $("btn-sync-now").addEventListener("click", handleSyncNow);

    // Filter bar
    $("month-filter").addEventListener("change", (e) => {
      state.selectedMonth = e.target.value || "all";
      fetchDashboard(state.selectedMonth).catch((err) => showToast(err.message, "error"));
    });

    $("btn-refresh-dashboard").addEventListener("click", async () => {
      const btn = $("btn-refresh-dashboard");
      btn.disabled = true;
      try {
        await Promise.all([fetchStatus(true), fetchDashboard(state.selectedMonth)]);
      } catch (err) {
        showToast(err.message || "Refresh failed.", "error");
      } finally {
        btn.disabled = false;
      }
    });

    // KPI Needs Review shortcut
    $("kpi-needs-review-btn").addEventListener("click", () => {
      const nrCount = Number(
        state.dashboard && state.dashboard.kpis ? state.dashboard.kpis.needs_review_count : 0
      );
      if (nrCount <= 0) return;
      state.activeTab = "receipts";
      state.statusFilter = "Needs Review";
      $("explorer-status-filter").value = "Needs Review";
      renderExplorer();
      $("tab-btn-receipts").scrollIntoView({ behavior: "smooth", block: "start" });
    });

    // Uploader interactions
    const dropzone = $("upload-dropzone");
    const fileInput = $("upload-file-input");

    $("btn-browse-files").addEventListener("click", (e) => {
      e.stopPropagation();
      fileInput.click();
    });

    dropzone.addEventListener("click", () => fileInput.click());
    dropzone.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        fileInput.click();
      }
    });

    fileInput.addEventListener("change", (e) => {
      if (e.target.files && e.target.files.length) {
        uploadReceiptFiles(e.target.files);
      }
    });

    ["dragenter", "dragover"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.add("is-dragover");
      });
    });

    ["dragleave", "drop"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.remove("is-dragover");
      });
    });

    dropzone.addEventListener("drop", (e) => {
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length) {
        uploadReceiptFiles(dt.files);
      }
    });

    // Prevent accidental page navigation if user drops a file outside the dropzone
    ["dragover", "drop"].forEach((eventName) => {
      window.addEventListener(eventName, (e) => {
        if (!dropzone.contains(e.target)) {
          e.preventDefault();
        }
      });
    });

    $("btn-clear-upload-results").addEventListener("click", () => {
      renderUploadResults([]);
    });

    // Breakdown toggle
    document.querySelectorAll(".segmented-btn[data-breakdown]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const mode = btn.dataset.breakdown === "sub" ? "sub" : "main";
        state.breakdownMode = mode;
        document.querySelectorAll(".segmented-btn[data-breakdown]").forEach((b) => {
          const active = b.dataset.breakdown === mode;
          b.classList.toggle("is-active", active);
          b.setAttribute("aria-selected", String(active));
        });
        renderCategoryBreakdown();
      });
    });

    // Monthly bar click filter (delegated)
    $("monthly-trend-chart").addEventListener("click", (e) => {
      const col = e.target.closest(".month-bar-col");
      if (!col || !col.dataset.month) return;
      const clickedMonth = col.dataset.month;
      state.selectedMonth = state.selectedMonth === clickedMonth ? "all" : clickedMonth;
      fetchDashboard(state.selectedMonth).catch((err) => showToast(err.message, "error"));
    });

    // Explorer tabs
    document.querySelectorAll(".tab-btn[data-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.activeTab = btn.dataset.tab === "receipts" ? "receipts" : "items";
        renderExplorer();
      });
    });

    // Explorer filters
    $("explorer-search").addEventListener("input", (e) => {
      state.searchQuery = e.target.value;
      renderExplorer();
    });
    $("explorer-category-filter").addEventListener("change", (e) => {
      state.categoryFilter = e.target.value;
      renderExplorer();
    });
    $("explorer-status-filter").addEventListener("change", (e) => {
      state.statusFilter = e.target.value;
      renderExplorer();
    });
    $("btn-clear-explorer-filters").addEventListener("click", () => {
      state.searchQuery = "";
      state.categoryFilter = "all";
      state.statusFilter = "all";
      $("explorer-search").value = "";
      $("explorer-category-filter").value = "all";
      $("explorer-status-filter").value = "all";
      renderExplorer();
    });

    // Explorer table row actions (event delegation)
    $("items-tbody").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-action='edit-item']");
      if (!btn) return;
      openEditItemModal(btn.dataset.receiptId, btn.dataset.itemIndex);
    });

    $("receipts-tbody").addEventListener("click", (e) => {
      const editBtn = e.target.closest("[data-action='edit-receipt']");
      if (editBtn) {
        openEditReceiptModal(editBtn.dataset.receiptId);
        return;
      }
      const delBtn = e.target.closest("[data-action='delete-receipt']");
      if (delBtn) {
        openDeleteReceiptModal(delBtn.dataset.receiptId);
      }
    });

    // Setup Modal forms & controls
    $("btn-toggle-gemini-key").addEventListener("click", () => {
      const input = $("gemini-api-key");
      const btn = $("btn-toggle-gemini-key");
      const isHidden = input.type === "password";
      input.type = isHidden ? "text" : "password";
      btn.textContent = isHidden ? "Hide" : "Show";
    });

    $("gemini-settings-form").addEventListener("submit", handleSaveGemini);
    $("general-settings-form").addEventListener("submit", handleSaveGeneral);
    $("btn-upload-credentials").addEventListener("click", handleUploadCredentials);
    $("btn-disconnect-drive").addEventListener("click", handleDisconnectDrive);

    // Edit & Delete Modal forms
    $("edit-receipt-form").addEventListener("submit", handleSaveReceipt);
    $("edit-item-form").addEventListener("submit", handleSaveItem);
    $("btn-confirm-delete-receipt").addEventListener("click", handleConfirmDeleteReceipt);

    // Auto-calc Net Total in Edit Item Modal
    ["edit-item-quantity", "edit-item-unit-price", "edit-item-discount"].forEach((id) => {
      $(id).addEventListener("input", recalculateItemNetTotal);
    });

    // Sync Main Category when Subcategory changes in Edit Item Modal
    $("edit-item-subcategory").addEventListener("change", (e) => {
      const map = getTaxonomy().subcategory_to_main;
      const mappedMain = map[e.target.value];
      if (mappedMain) {
        const mainSelect = $("edit-item-main-category");
        if (Array.from(mainSelect.options).some((o) => o.value === mappedMain)) {
          mainSelect.value = mappedMain;
        }
      }
    });

    // Sync Subcategory when Main Category changes in Edit Item Modal
    $("edit-item-main-category").addEventListener("change", (e) => {
      const selectedMain = e.target.value;
      const taxonomy = getTaxonomy();
      const map = taxonomy.subcategory_to_main;
      const subSelect = $("edit-item-subcategory");
      if (map[subSelect.value] === selectedMain) {
        return;
      }
      const firstMatchingSub = taxonomy.subcategories.find(
        (sub) => map[sub] === selectedMain
      );
      if (
        firstMatchingSub &&
        Array.from(subSelect.options).some((o) => o.value === firstMatchingSub)
      ) {
        subSelect.value = firstMatchingSub;
      }
    });

    // Modal close buttons, backdrop clicks, Escape key
    document.querySelectorAll("[data-close-modal]").forEach((btn) => {
      btn.addEventListener("click", () => closeModal(btn.dataset.closeModal));
    });

    document.querySelectorAll(".modal-backdrop").forEach((backdrop) => {
      backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop) {
          closeModal(backdrop.id);
        }
      });
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        document.querySelectorAll(".modal-backdrop:not(.hidden)").forEach((m) => {
          closeModal(m.id);
        });
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        pollStatusTick();
      }
    });
  }

  async function init() {
    bindEvents();
    const authQuery = parseInitialAuthQueryParams();

    const [statusResult, dashResult] = await Promise.allSettled([
      fetchStatus(true),
      fetchDashboard(state.selectedMonth),
    ]);

    if (statusResult.status === "fulfilled" && statusResult.value) {
      const st = statusResult.value;
      const totals = (st.watcher && st.watcher.totals) || {};
      state.lastProcessedTotal = Number(totals.processed || 0);
      state.lastSyncAt = (st.watcher && st.watcher.last_sync_at) || null;
    } else if (statusResult.status === "rejected") {
      showToast(statusResult.reason?.message || "Could not load application status.", "error");
    }

    if (dashResult.status === "rejected") {
      showToast(dashResult.reason?.message || "Could not load budget dashboard.", "error");
    }

    if (authQuery && authQuery.type === "google_connected") {
      showToast("Google Drive connected successfully!", "success");
      openSetupModal(false);
      setInlineFeedback("drive-feedback", "Google Drive connected successfully!", "success");
      state.hasAutoOpenedSetup = true;
    } else if (authQuery && authQuery.type === "auth_error") {
      const errMsg = `Google Drive sign-in error: ${authQuery.message}`;
      showToast(errMsg, "error", 8000);
      openSetupModal(false);
      setInlineFeedback("drive-feedback", errMsg, "error");
      state.hasAutoOpenedSetup = true;
    } else if (state.status && !state.status.setup_complete && !state.hasAutoOpenedSetup) {
      openSetupModal(false);
      state.hasAutoOpenedSetup = true;
    }

    window.setInterval(pollStatusTick, 4000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
