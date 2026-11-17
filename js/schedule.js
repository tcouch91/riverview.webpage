/**
 * Riverview Fire Department - Staff Scheduling Module
 * ---------------------------------------------------
 * Loaded by timesheet.html *before* its main script. It only defines state and
 * functions here; at call time it uses the portal's shared session and helpers
 * (currentUser, apiRequest, showAlert, escapeHtml, toIsoDate, parseIsoDateLocal,
 * formatHumanDate, getMondaySundayPeriod, getMaxAllowedShiftDateIso,
 * calculateDurationClient, switchTab, applyShiftPreset, resetEntryForm,
 * setPayPeriodByDate).
 *
 * Tabs:
 *   Schedule     - published month, my shifts + weekly hours, open-shift sign-up
 *   Availability - members drag across an hour grid (07:00 -> 07:00 days)
 *   Builder      - officers fill seats (by hand or Auto-Fill) with projected
 *                  weekly hours, OT warnings, and driver coverage
 */

const SCH = {
    app: "timesheet",                                        // "timesheet" | "schedule"
    lastTab: { timesheet: "tabLogHours", schedule: "tabSchedule" },
    monthByTab: {},                                          // tabId -> "YYYY-MM"
    settings: null,                                          // GET /api/schedule/settings
    view: null,                                              // Schedule tab data
    avail: null,                                             // Availability tab data
    availDays: new Map(),                                    // op_date -> 24-bit mask (bit i = startHour + i)
    availStartHour: 7,
    availWeekIdx: 0,
    availDrag: null,
    availDirty: false,
    availMemberId: "",
    employees: null,
    builder: null,                                           // Builder tab data
    builderIdx: null,
    focusDept: "",
    editSeats: false,
    presetShifts: [],
    presetReq: 0,
    loadSeq: 0
};

const SCH_FULL_DAY = 16777215;   // 24 bits
const SCH_CALLOUT_DAYS = 21;     // Availability reminder shows in the final 3 weeks
const SCH_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// =========================================================================
// Small helpers
// =========================================================================

function schIsReviewer() {
    return Boolean(currentUser) && (currentUser.role === "officer" || currentUser.role === "cfo");
}

function schPad(n) {
    return String(n).padStart(2, "0");
}

function schMonthOf(dateObj) {
    return `${dateObj.getFullYear()}-${schPad(dateObj.getMonth() + 1)}`;
}

function schCurrentMonth() {
    return schMonthOf(new Date());
}

function schAddMonths(monthStr, delta) {
    const [y, m] = monthStr.split("-").map(Number);
    return schMonthOf(new Date(y, m - 1 + delta, 1));
}

function schMonthLabel(monthStr) {
    const [y, m] = monthStr.split("-").map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString("en-US", { month: "long", year: "numeric" });
}

function schMonthDates(monthStr) {
    const [y, m] = monthStr.split("-").map(Number);
    const days = new Date(y, m, 0).getDate();
    const out = [];
    for (let d = 1; d <= days; d++) out.push(`${y}-${schPad(m)}-${schPad(d)}`);
    return out;
}

function schHrs(value) {
    return String(Math.round(Number(value || 0) * 100) / 100);
}

function schShortDate(iso) {
    return parseIsoDateLocal(iso).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

function schShortRange(startIso, endIso) {
    const s = parseIsoDateLocal(startIso);
    const e = parseIsoDateLocal(endIso);
    const mon = (d) => d.toLocaleDateString("en-US", { month: "short" });
    return s.getMonth() === e.getMonth()
        ? `${mon(s)} ${s.getDate()}–${e.getDate()}`
        : `${mon(s)} ${s.getDate()}–${mon(e)} ${e.getDate()}`;
}

function schTimeToMinutes(t) {
    if (t === "24:00") return 1440;
    const [h, m] = String(t).split(":").map(Number);
    return h * 60 + m;
}

function schMinToHhmm(mins) {
    const m = ((mins % 1440) + 1440) % 1440;
    return `${schPad(Math.floor(m / 60))}:${schPad(m % 60)}`;
}

// Whole days since the Unix epoch for a YYYY-MM-DD string (timezone-free)
function schDayIndex(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return Math.round(Date.UTC(y, m - 1, d) / 86400000);
}

function schAddDays(iso, n) {
    const d = new Date((schDayIndex(iso) + n) * 86400000);
    return `${d.getUTCFullYear()}-${schPad(d.getUTCMonth() + 1)}-${schPad(d.getUTCDate())}`;
}

function schWeekdayIndex(iso) {
    return (parseIsoDateLocal(iso).getDay() + 6) % 7; // Monday = 0
}

// Seat window [startMin, endMin] on an absolute timeline (overnight seats end the next day)
function schSeatInterval(slot) {
    const base = schDayIndex(slot.shift_date) * 1440;
    const s = schTimeToMinutes(slot.start_time);
    const e = schTimeToMinutes(slot.end_time);
    return [base + s, base + (e <= s ? 1440 + e : e)];
}

// What the assignee actually works (partial coverage), else the whole seat
function schWorkInterval(slot) {
    if (slot.cover_start_min !== null && slot.cover_start_min !== undefined
        && slot.cover_end_min !== null && slot.cover_end_min !== undefined) {
        const base = schDayIndex(slot.shift_date) * 1440;
        return [base + slot.cover_start_min, base + slot.cover_end_min];
    }
    return schSeatInterval(slot);
}

function schSlotsOverlap(a, b) {
    const [as, ae] = schWorkInterval(a);
    const [bs, be] = schWorkInterval(b);
    return as < be && bs < ae;
}

function schSlotStart(slot) {
    const d = parseIsoDateLocal(slot.shift_date);
    const mins = schTimeToMinutes(slot.start_time);
    return new Date(d.getFullYear(), d.getMonth(), d.getDate(), Math.floor(mins / 60), mins % 60);
}

function schShiftClass(shiftKey) {
    return `shift-${String(shiftKey).replace(/[^a-z0-9_-]/gi, "")}`;
}

function schWeekStart(iso) {
    return getMondaySundayPeriod(iso).start;
}

function schShiftHours(tpl, shiftKey) {
    const sh = ((tpl && tpl.shifts) || []).find(s => s.key === shiftKey);
    if (!sh) return 0;
    const calc = calculateDurationClient(sh.start, sh.end);
    return calc ? calc.hours : 0;
}

function schPopcount(n) {
    let c = 0;
    let v = n >>> 0;
    while (v) {
        c += v & 1;
        v >>>= 1;
    }
    return c;
}

// {op_date: mask} -> Set of absolute hours (dayIndex * 24 + hour)
function schMaskHours(days, startHour) {
    const hours = new Set();
    const entries = days instanceof Map ? days.entries() : Object.entries(days || {});
    for (const [iso, rawMask] of entries) {
        const base = schDayIndex(iso) * 24 + startHour;
        let mask = Number(rawMask) >>> 0;
        let i = 0;
        while (mask) {
            if (mask & 1) hours.add(base + i);
            mask >>>= 1;
            i++;
        }
    }
    return hours;
}

/**
 * Largest contiguous available run inside a seat [startMin, endMin).
 * Mirrors seat_coverage() in the backend. Returns {share 0-1, start, end}.
 */
function schCoverage(hours, startMin, endMin) {
    const total = endMin - startMin;
    if (!hours || !hours.size || total <= 0) return { share: 0, start: startMin, end: startMin };
    let best = { len: 0, start: startMin, end: startMin };
    let runStart = null;
    const firstHour = Math.floor(startMin / 60);
    const lastHour = Math.floor((endMin - 1) / 60);
    for (let h = firstHour; h <= lastHour + 1; h++) {
        const available = h <= lastHour && hours.has(h);
        if (available && runStart === null) {
            runStart = h;
        } else if (!available && runStart !== null) {
            const s = Math.max(startMin, runStart * 60);
            const e = Math.min(endMin, h * 60);
            if (e - s > best.len) best = { len: e - s, start: s, end: e };
            runStart = null;
        }
    }
    return { share: best.len / total, start: best.start, end: best.end };
}

// Availability-grid rows (0-23 from startHour) that a template shift covers
function schShiftRows(shift, startHour) {
    let s = schTimeToMinutes(shift.start);
    let e = schTimeToMinutes(shift.end);
    if (e <= s) e += 1440;
    if (s < startHour * 60) {
        s += 1440;
        e += 1440;
    }
    const rows = [];
    for (let r = 0; r < 24; r++) {
        const hs = (startHour + r) * 60;
        if (hs < e && s < hs + 60) rows.push(r);
    }
    return rows;
}

function schCrewKey(slot) {
    return `${slot.shift_date}|${slot.shift_key}|${slot.position_key}`;
}

function schDriverPositions(tpl) {
    return new Set(((tpl && tpl.positions) || []).filter(p => p.requires_driver).map(p => p.key));
}

function schToast(message, type = "success") {
    let el = document.getElementById("schToast");
    if (!el) {
        el = document.createElement("div");
        el.id = "schToast";
        el.setAttribute("role", "status");
        document.body.appendChild(el);
    }
    el.textContent = message;
    el.className = `sch-toast no-print ${type === "error" ? "is-error" : ""} show`;
    clearTimeout(schToast.timer);
    schToast.timer = setTimeout(() => el.classList.remove("show"), type === "error" ? 6500 : 2200);
}

// =========================================================================
// Timesheet | Schedule switching (called by the portal)
// =========================================================================

function applyPortalTabVisibility() {
    const reviewer = schIsReviewer();
    document.querySelectorAll("#portalTabs .tab-btn").forEach(btn => {
        const app = btn.getAttribute("data-app") || "timesheet";
        const show = app === SCH.app && (!btn.classList.contains("reviewer-only") || reviewer);
        btn.style.display = show ? "inline-flex" : "none";
    });
}

function switchApp(app, opts = {}) {
    const target = app === "schedule" ? "schedule" : "timesheet";
    if (SCH.app === "schedule" && target !== "schedule" && !schCanLeaveTab(null)) {
        return Promise.resolve();
    }
    SCH.app = target;
    const isSchedule = target === "schedule";

    document.querySelectorAll(".app-switch-btn").forEach(b => {
        const active = b.getAttribute("data-app") === target;
        b.classList.toggle("active", active);
        b.setAttribute("aria-selected", String(active));
    });
    document.getElementById("payPeriodBar").style.display = isSchedule ? "none" : "";
    document.getElementById("scheduleMonthBar").style.display = isSchedule ? "" : "none";
    document.getElementById("portalTitle").textContent = isSchedule ? "Schedule" : "Timesheet";
    document.title = `${isSchedule ? "Schedule" : "Timesheet"} | Riverview Fire Department`;

    const navTs = document.getElementById("navTimesheetLink");
    const navSch = document.getElementById("navScheduleLink");
    [[navTs, !isSchedule], [navSch, isSchedule]].forEach(([el, on]) => {
        if (!el) return;
        el.style.color = on ? "var(--primary-red-light)" : "";
        el.style.fontWeight = on ? "700" : "";
    });

    applyPortalTabVisibility();
    try {
        history.replaceState(null, "", isSchedule ? "#schedule" : window.location.pathname + window.location.search);
    } catch (_) { /* file:// or sandboxed */ }

    let tab = opts.tab || SCH.lastTab[target];
    if (tab === "tabScheduleBuilder" && !schIsReviewer()) tab = "tabSchedule";
    return switchTab(tab);
}

// Guard used by switchTab: confirm before abandoning unsaved availability edits
function schCanLeaveTab(targetTab) {
    const onAvailability = SCH.app === "schedule" && SCH.lastTab.schedule === "tabAvailability";
    if (onAvailability && targetTab !== "tabAvailability") {
        return schConfirmDiscardAvailability();
    }
    return true;
}

function schConfirmDiscardAvailability() {
    if (!SCH.availDirty) return true;
    if (!confirm("You have unsaved availability changes. Discard them?")) return false;
    SCH.availDirty = false;
    return true;
}

function resetScheduleState() {
    Object.assign(SCH, {
        app: "timesheet",
        lastTab: { timesheet: "tabLogHours", schedule: "tabSchedule" },
        monthByTab: {},
        settings: null,
        view: null,
        avail: null,
        availDays: new Map(),
        availWeekIdx: 0,
        availDrag: null,
        availDirty: false,
        availMemberId: "",
        employees: null,
        builder: null,
        builderIdx: null,
        focusDept: "",
        editSeats: false,
        presetShifts: []
    });
    const bar = document.getElementById("scheduledPresetBar");
    if (bar) {
        bar.style.display = "none";
        bar.innerHTML = "";
    }
}

// =========================================================================
// Month bar & tab loading
// =========================================================================

function schActiveTab() {
    return SCH.lastTab.schedule;
}

function schDefaultMonth(tabId) {
    const current = schCurrentMonth();
    if (tabId === "tabSchedule") return current;
    if (tabId === "tabScheduleBuilder") return schAddMonths(current, 1);

    // Availability: the soonest upcoming month still accepting availability
    const upcoming = SCH.view && SCH.view.upcoming_availability;
    if (upcoming && upcoming.month) return upcoming.month;
    const tpl = SCH.settings && SCH.settings.template;
    const lead = tpl ? Number(tpl.availability_lead_days) : 14;
    const todayIso = toIsoDate(new Date());
    for (let i = 1; i <= 3; i++) {
        const m = schAddMonths(current, i);
        const [y, mo] = m.split("-").map(Number);
        if (todayIso <= toIsoDate(new Date(y, mo - 1, 1 - lead))) return m;
    }
    return schAddMonths(current, 1);
}

function updateScheduleMonthBar() {
    const tab = schActiveTab();
    const month = SCH.monthByTab[tab];
    document.getElementById("schMonthTitle").textContent = month ? schMonthLabel(month) : "";
    document.getElementById("schMonthResetBtn").textContent =
        tab === "tabSchedule" ? "This Month" : tab === "tabAvailability" ? "Next Due" : "Next Month";
}

function setScheduleMonthSub(text) {
    document.getElementById("schMonthSub").textContent = text;
}

function schShiftMonth(delta) {
    const tab = schActiveTab();
    if (tab === "tabAvailability" && !schConfirmDiscardAvailability()) return;
    SCH.monthByTab[tab] = schAddMonths(SCH.monthByTab[tab] || schCurrentMonth(), delta);
    refreshScheduleViews();
}

function schResetMonth() {
    const tab = schActiveTab();
    if (tab === "tabAvailability" && !schConfirmDiscardAvailability()) return;
    SCH.monthByTab[tab] = schDefaultMonth(tab);
    refreshScheduleViews();
}

async function refreshScheduleViews() {
    if (!currentUser) return;
    let tab = schActiveTab();
    if (tab === "tabScheduleBuilder" && !schIsReviewer()) {
        tab = SCH.lastTab.schedule = "tabSchedule";
    }
    if (!SCH.settings) {
        try {
            SCH.settings = await apiRequest("/api/schedule/settings");
        } catch (_) {
            SCH.settings = null;
        }
    }
    if (!SCH.monthByTab[tab]) SCH.monthByTab[tab] = schDefaultMonth(tab);
    updateScheduleMonthBar();
    setScheduleMonthSub("Loading…");

    if (tab === "tabSchedule") await loadScheduleView();
    else if (tab === "tabAvailability") await loadAvailabilityView();
    else await loadBuilderView();
}

function schOpenAvailability(month, deptId) {
    SCH.monthByTab.tabAvailability = month;
    if (deptId && schIsReviewer()) SCH.availMemberId = deptId;
    if (SCH.app !== "schedule") {
        switchApp("schedule", { tab: "tabAvailability" });
    } else {
        switchTab("tabAvailability");
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
}

function schOpenBuilder(month) {
    SCH.monthByTab.tabScheduleBuilder = month;
    switchTab("tabScheduleBuilder");
}

// =========================================================================
// Shared grid rendering (read-only schedule, builder, print)
// =========================================================================

function schGridColumns(slots, tpl) {
    const cols = [];
    const seen = new Set();
    slots.forEach(s => {
        const key = `${s.shift_key}|${s.position_key}`;
        if (seen.has(key)) return;
        seen.add(key);
        cols.push({
            key,
            shift_key: s.shift_key,
            shift_label: s.shift_label,
            position_key: s.position_key,
            position_label: s.position_label,
            start_time: s.start_time,
            end_time: s.end_time
        });
    });
    const shiftIdx = new Map(((tpl && tpl.shifts) || []).map((s, i) => [s.key, i]));
    const posIdx = new Map(((tpl && tpl.positions) || []).map((p, i) => [p.key, i]));
    const rank = (map, k) => (map.has(k) ? map.get(k) : 99);
    cols.sort((a, b) =>
        rank(shiftIdx, a.shift_key) - rank(shiftIdx, b.shift_key) ||
        rank(posIdx, a.position_key) - rank(posIdx, b.position_key)
    );
    return cols;
}

function schSlotsByDate(slots) {
    const map = new Map();
    slots.forEach(s => {
        if (!map.has(s.shift_date)) map.set(s.shift_date, new Map());
        const day = map.get(s.shift_date);
        const key = `${s.shift_key}|${s.position_key}`;
        if (!day.has(key)) day.set(key, []);
        day.get(key).push(s);
    });
    map.forEach(day => day.forEach(list => list.sort((a, b) => a.seat - b.seat)));
    return map;
}

function schGridHeadHtml(cols, driverPositions) {
    const groups = [];
    cols.forEach(c => {
        const last = groups[groups.length - 1];
        if (last && last.shift_key === c.shift_key) {
            last.span++;
        } else {
            groups.push({ shift_key: c.shift_key, label: c.shift_label, start: c.start_time, end: c.end_time, span: 1 });
        }
    });
    const drivers = driverPositions || new Set();
    return `
        <thead>
            <tr>
                <th rowspan="2">Date</th>
                ${groups.map(g => `
                    <th colspan="${g.span}" class="sch-grid-group ${schShiftClass(g.shift_key)}">
                        ${escapeHtml(g.label)}<span class="sch-grid-times">${escapeHtml(g.start)}–${escapeHtml(g.end)}</span>
                    </th>`).join("")}
            </tr>
            <tr>${cols.map(c => `<th>${escapeHtml(c.position_label)}${drivers.has(c.position_key) ? ' <span class="sch-drv-tag" title="Needs a qualified driver">D</span>' : ""}</th>`).join("")}</tr>
        </thead>`;
}

function schDateCellHtml(iso, extraHtml = "") {
    const d = parseIsoDateLocal(iso);
    const dow = d.toLocaleDateString("en-US", { weekday: "short" });
    const md = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    return `<td class="sch-date-cell" data-label="Date"><span class="sch-date-dow">${dow}</span> ${md}${extraHtml}</td>`;
}

function schRowClass(iso, todayIso) {
    const dow = parseIsoDateLocal(iso).getDay();
    return `${dow === 0 || dow === 6 ? "sch-row-weekend" : ""} ${iso === todayIso ? "sch-row-today" : ""}`;
}

// Crew-level driver state for a list of slots (all seats of one date/shift/position)
function schCrewDriverState(seats, isDriverFn) {
    const filled = seats.filter(s => s.dept_id);
    const hasDriver = filled.some(s => isDriverFn(s));
    const open = seats.filter(s => !s.dept_id);
    return { filled: filled.length, open: open.length, hasDriver, heldForDriver: !hasDriver && open.length === 1 };
}

// =========================================================================
// SCHEDULE TAB: published month, my shifts, open shifts
// =========================================================================

async function loadScheduleView() {
    const month = SCH.monthByTab.tabSchedule;
    const seq = ++SCH.loadSeq;
    try {
        const data = await apiRequest(`/api/schedule/${month}`);
        if (seq !== SCH.loadSeq) return;
        SCH.view = data;
        renderScheduleView();
    } catch (err) {
        if (seq === SCH.loadSeq) {
            setScheduleMonthSub("");
            showAlert(err.message, "error");
        }
    }
}

function renderScheduleView() {
    const data = SCH.view;
    if (!data) return;
    const month = data.month;
    const me = currentUser.dept_id;

    renderAvailabilityCallout(data.upcoming_availability);

    if (data.published) {
        const when = month.published_at
            ? ` ${new Date(month.published_at).toLocaleDateString("en-US", { month: "short", day: "numeric" })}`
            : "";
        const who = month.published_by ? ` by ${String(month.published_by).replace(/^9\d{2}\s+/, "")}` : "";
        setScheduleMonthSub(`Published${when}${who}`);
    } else {
        setScheduleMonthSub("Not published yet");
    }

    const mySlots = data.slots.filter(s => s.dept_id === me);
    const hours = data.member_hours || {};
    const byType = {};
    mySlots.forEach(s => { byType[s.shift_label] = (byType[s.shift_label] || 0) + 1; });
    document.getElementById("schMyShiftCount").textContent = mySlots.length;
    document.getElementById("schMyShiftSub").textContent =
        Object.keys(byType).length ? Object.entries(byType).map(([k, v]) => `${v} ${k}`).join(" • ") : "this month";
    document.getElementById("schMyHours").textContent = schHrs(hours.month_hours);
    document.getElementById("schMyHoursSub").textContent =
        hours.projected_ot_hours > 0 ? `${schHrs(hours.projected_ot_hours)}h projected OT` : "this month";

    const now = new Date();
    const openFuture = data.slots.filter(s => !s.dept_id && schSlotStart(s) > now);
    document.getElementById("schOpenCount").textContent = openFuture.length;

    const unpublishedCard = document.getElementById("schUnpublishedCard");
    const publishedWrap = document.getElementById("schPublishedWrap");
    if (!data.published) {
        publishedWrap.style.display = "none";
        unpublishedCard.style.display = "block";
        const deadline = formatHumanDate(month.availability_deadline);
        const availLine = month.availability_open
            ? `Availability is due <b>${escapeHtml(deadline)}</b>.`
            : "Availability is closed and an officer is building the schedule.";
        unpublishedCard.innerHTML = `
            <div class="sch-empty">
                <div class="sch-empty-icon">&#128203;</div>
                <div class="sch-empty-title">The ${escapeHtml(month.label)} schedule hasn't been published yet.</div>
                <div class="sch-subtle">${availLine}</div>
                ${month.availability_open ? `<button type="button" class="btn btn-primary" onclick="schOpenAvailability('${month.month}')">Enter Availability</button>` : ""}
                ${schIsReviewer() ? `<button type="button" class="btn btn-dark" onclick="schOpenBuilder('${month.month}')">Open Builder</button>` : ""}
            </div>`;
        return;
    }

    unpublishedCard.style.display = "none";
    publishedWrap.style.display = "block";
    renderMyShifts(mySlots);
    renderVacancies(openFuture, mySlots);
    renderScheduleGrid();
}

function renderAvailabilityCallout(up) {
    const box = document.getElementById("schAvailCallout");
    const days = up ? Number(up.days_until_deadline) : NaN;
    if (!up || days > SCH_CALLOUT_DAYS) {
        box.style.display = "none";
        return;
    }
    const due = parseIsoDateLocal(up.deadline).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
    const left = days <= 0 ? "due today" : `${days} day${days === 1 ? "" : "s"} left`;
    if (up.submitted) {
        box.className = "sch-callout is-done";
        box.innerHTML = `
            <div><b>&#10003; ${escapeHtml(up.label)} availability submitted.</b> You can change it until ${escapeHtml(due)}.</div>
            <button type="button" class="btn btn-outline btn-sm" onclick="schOpenAvailability('${up.month}')">Review</button>`;
    } else {
        box.className = "sch-callout";
        box.innerHTML = `
            <div><b>${escapeHtml(up.label)} availability is due ${escapeHtml(due)}</b> (${left}).</div>
            <button type="button" class="btn btn-primary btn-sm" onclick="schOpenAvailability('${up.month}')">Enter Availability</button>`;
    }
    box.style.display = "flex";
}

function renderMyShifts(mySlots) {
    const data = SCH.view;
    const thr = (data.member && data.member.ot_threshold) || 40;
    const byWeek = (data.member_hours && data.member_hours.by_week) || {};

    document.getElementById("schMyWeekChips").innerHTML = (data.weeks || []).map(w => {
        const h = byWeek[w.start] || 0;
        const ot = h > thr;
        return `<span class="sch-week-chip ${ot ? "is-ot" : ""}" title="Mon–Sun pay week (OT after ${thr}h)">${schShortRange(w.start, w.end)}: <b>${schHrs(h)}h</b>${ot ? " OT" : ""}</span>`;
    }).join("");

    const tbody = document.getElementById("schMyShiftsTbody");
    if (!mySlots.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="empty-row-cell" style="text-align: center; color: var(--text-muted);">You have no shifts this month. Sign up for an open shift below.</td></tr>`;
        return;
    }

    const now = new Date();
    const maxLogIso = getMaxAllowedShiftDateIso();
    const cutoffMs = (Number(data.signup_drop_cutoff_hours) || 24) * 3600000;
    tbody.innerHTML = mySlots.map(s => {
        const start = schSlotStart(s);
        const canLog = (s.work_date || s.shift_date) <= maxLogIso;
        const canDrop = s.assigned_via === "signup" && (start - now) > cutoffMs;
        const times = s.is_partial ? `${s.work_start}–${s.work_end}` : `${s.start_time}–${s.end_time}`;
        return `
            <tr class="${start < now ? "sch-row-past" : ""}">
                <td data-label="Date"><b>${escapeHtml(schShortDate(s.shift_date))}</b></td>
                <td data-label="Shift">
                    <span>
                        <span class="badge sch-shift-badge ${schShiftClass(s.shift_key)}">${escapeHtml(s.shift_label)}</span>
                        ${escapeHtml(times)}
                        ${s.is_partial ? '<span class="badge badge-pending" title="You cover part of this shift">Partial</span>' : ""}
                        ${s.is_overnight && !s.is_partial ? '<span class="badge badge-overnight">+1d</span>' : ""}
                    </span>
                </td>
                <td data-label="Assignment">
                    <span>${escapeHtml(s.position_label)}${s.assigned_via === "signup" ? ' <span class="badge badge-pending">Signed up</span>' : ""}</span>
                </td>
                <td data-label="Hours"><b>${schHrs(s.hours)}</b></td>
                <td data-label="Actions" class="actions-cell">
                    <div class="actions-btn-group">
                        ${canLog ? `<button type="button" class="btn btn-outline btn-sm" onclick="schLogScheduledShift(${s.id})">Log Hours</button>` : ""}
                        ${canDrop ? `<button type="button" class="btn btn-outline btn-sm" style="color: #dc2626;" onclick="schDropSlot(${s.id})">Drop</button>` : ""}
                        ${!canLog && !canDrop ? '<span style="font-size: 0.8rem; color: var(--text-muted);">Scheduled</span>' : ""}
                    </div>
                </td>
            </tr>`;
    }).join("");
}

function renderVacancies(openSlots, mySlots) {
    const data = SCH.view;
    const box = document.getElementById("schVacancyList");
    if (!openSlots.length) {
        box.innerHTML = `<div class="sch-empty-line">No open shifts right now. Every seat is covered. &#128077;</div>`;
        return;
    }
    const thr = (data.member && data.member.ot_threshold) || 40;
    const byWeek = (data.member_hours && data.member_hours.by_week) || {};
    const iAmDriver = Boolean(data.member && data.member.is_driver);
    const driverPositions = schDriverPositions(data.template);
    const crews = new Map();
    data.slots.forEach(s => {
        const k = schCrewKey(s);
        if (!crews.has(k)) crews.set(k, []);
        crews.get(k).push(s);
    });

    const days = new Map();
    openSlots.forEach(s => {
        if (!days.has(s.shift_date)) days.set(s.shift_date, new Map());
        const groups = days.get(s.shift_date);
        const key = `${s.shift_key}|${s.position_key}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(s);
    });

    box.innerHTML = Array.from(days.entries()).map(([iso, groups]) => {
        const chips = Array.from(groups.values()).map(list => {
            const s = list[0];
            const conflict = mySlots.find(m => schSlotsOverlap(m, s));
            const crew = schCrewDriverState(crews.get(schCrewKey(s)) || [], x => x.is_driver);
            const needsDriver = driverPositions.has(s.position_key) && !crew.hasDriver;
            const heldForDriver = needsDriver && crew.heldForDriver;
            const weekHrs = byWeek[schWeekStart(s.shift_date)];
            const projected = (weekHrs || 0) + s.hours;
            const ot = projected > thr;
            let sub;
            let disabled = false;
            if (conflict) {
                sub = `You're already on ${conflict.shift_label} (${conflict.position_label})`;
                disabled = true;
            } else if (heldForDriver && !iAmDriver) {
                sub = "Held for a qualified driver";
                disabled = true;
            } else {
                sub = `${schHrs(s.hours)}h`;
                if (list.length > 1) sub += ` • ${list.length} open`;
                if (needsDriver && iAmDriver) sub += " • driver needed";
                if (weekHrs !== undefined) sub += ` • your week → ${schHrs(projected)}h${ot ? " (OT)" : ""}`;
            }
            return `
                <button type="button" class="sch-vac-chip ${schShiftClass(s.shift_key)} ${ot && !disabled ? "is-ot" : ""} ${needsDriver && iAmDriver && !disabled ? "is-driver-needed" : ""}"
                        ${disabled ? "disabled" : ""} onclick="schSignupSlot(${s.id})">
                    ${escapeHtml(s.shift_label)} ${escapeHtml(s.start_time)}–${escapeHtml(s.end_time)} · ${escapeHtml(s.position_label)}
                    <small>${escapeHtml(sub)}</small>
                </button>`;
        }).join("");
        return `<div class="sch-vac-day"><div class="sch-vac-date">${escapeHtml(schShortDate(iso))}</div><div class="sch-vac-chips">${chips}</div></div>`;
    }).join("");
}

function renderScheduleGrid() {
    const data = SCH.view;
    const table = document.getElementById("schViewGrid");
    if (!data || !data.published) {
        table.innerHTML = "";
        return;
    }
    const filter = document.getElementById("schViewFilter").value;
    const me = currentUser.dept_id;
    const cols = schGridColumns(data.slots, data.template);
    const byDate = schSlotsByDate(data.slots);
    const now = new Date();
    const todayIso = toIsoDate(now);
    const mySlots = data.slots.filter(s => s.dept_id === me);
    const iAmDriver = Boolean(data.member && data.member.is_driver);
    const driverPositions = schDriverPositions(data.template);

    const dates = schMonthDates(data.month.month).filter(iso => {
        const day = byDate.get(iso);
        if (!day) return false;
        const all = Array.from(day.values()).flat();
        if (filter === "mine") return all.some(s => s.dept_id === me);
        if (filter === "open") return all.some(s => !s.dept_id && schSlotStart(s) > now);
        return true;
    });

    if (!dates.length) {
        table.innerHTML = `<tbody><tr><td class="empty-row-cell" style="text-align: center; color: var(--text-muted);">No matching days.</td></tr></tbody>`;
        return;
    }

    const body = dates.map(iso => {
        const day = byDate.get(iso);
        const cells = cols.map(c => {
            const seats = day.get(c.key) || [];
            const crew = schCrewDriverState(seats, x => x.is_driver);
            const held = driverPositions.has(c.position_key) && crew.heldForDriver;
            const inner = seats.map(s => {
                if (s.dept_id) {
                    const name = s.employee_name || s.dept_id;
                    const partial = s.is_partial ? ` <span class="sch-partial-tag">${escapeHtml(s.work_start)}–${escapeHtml(s.work_end)}</span>` : "";
                    return `<span class="sch-name ${s.dept_id === me ? "is-me" : ""}" title="${escapeHtml(name)}">${escapeHtml(name)}${partial}</span>`;
                }
                const blocked = held && !iAmDriver;
                const canSign = schSlotStart(s) > now && !mySlots.some(m => schSlotsOverlap(m, s)) && !blocked;
                const label = held ? "Open · driver" : "Open";
                return canSign
                    ? `<button type="button" class="sch-open-pill" onclick="schSignupSlot(${s.id})">${label} · Sign up</button>`
                    : `<span class="sch-open-pill">${label}</span>`;
            }).join("");
            return `<td data-label="${escapeHtml(c.shift_label)} · ${escapeHtml(c.position_label)}"><div class="sch-seat-stack">${inner || '<span class="sch-muted-dash">—</span>'}</div></td>`;
        }).join("");
        return `<tr class="${schRowClass(iso, todayIso)}">${schDateCellHtml(iso)}${cells}</tr>`;
    }).join("");

    table.innerHTML = schGridHeadHtml(cols) + `<tbody>${body}</tbody>`;
}

async function schSignupSlot(slotId) {
    const s = ((SCH.view && SCH.view.slots) || []).find(x => x.id === slotId);
    if (!s) return;
    const ok = confirm(
        `Sign up for ${s.shift_label} ${s.start_time}–${s.end_time} at ${s.position_label} on ${formatHumanDate(s.shift_date)}?`
    );
    if (!ok) return;
    try {
        const res = await apiRequest(`/api/schedule/slots/${slotId}/signup`, { method: "POST", body: "{}" });
        showAlert(res.message);
    } catch (err) {
        showAlert(err.message, "error");
    }
    await loadScheduleView();
}

async function schDropSlot(slotId) {
    if (!confirm("Drop this shift? It will reopen for others to sign up.")) return;
    try {
        const res = await apiRequest(`/api/schedule/slots/${slotId}/drop`, { method: "POST", body: "{}" });
        showAlert(res.message);
    } catch (err) {
        showAlert(err.message, "error");
    }
    await loadScheduleView();
}

// Schedule -> Timesheet: prefill Log Shift with a scheduled shift (the part they actually work)
function schLogScheduledShift(slotId) {
    const s = ((SCH.view && SCH.view.slots) || []).find(x => x.id === slotId);
    if (!s) return;
    const workDate = s.work_date || s.shift_date;
    if (workDate > getMaxAllowedShiftDateIso()) {
        showAlert("You can log this shift starting the day before it.", "error");
        return;
    }
    resetEntryForm();
    setPayPeriodByDate(workDate, false);
    document.getElementById("entryShiftDate").value = workDate;
    if (schIsReviewer()) {
        const memberSelect = document.getElementById("entryDeptId");
        if (memberSelect) memberSelect.value = currentUser.dept_id;
    }
    applyShiftPreset(s.work_start || s.start_time, s.work_end || s.end_time);
    document.getElementById("entryNotes").value = `Scheduled: ${s.shift_label} - ${s.position_label}`;
    switchApp("timesheet", { tab: "tabLogHours" });
    window.scrollTo({ top: 160, behavior: "smooth" });
    showAlert("Shift details filled in from the schedule. Adjust the times if needed, then submit.");
}

// =========================================================================
// AVAILABILITY TAB: drag-to-select hour grid
// =========================================================================

async function schEnsureEmployees() {
    if (SCH.employees) return SCH.employees;
    try {
        const data = await apiRequest("/api/employees");
        SCH.employees = (data.employees || []).filter(e => e.active);
    } catch (_) {
        SCH.employees = [];
    }
    return SCH.employees;
}

async function loadAvailabilityView() {
    const month = SCH.monthByTab.tabAvailability;
    const reviewer = schIsReviewer();
    document.getElementById("availMemberGroup").style.display = reviewer ? "block" : "none";

    if (reviewer) {
        const emps = await schEnsureEmployees();
        if (!SCH.availMemberId) SCH.availMemberId = currentUser.dept_id;
        const sel = document.getElementById("availMemberSelect");
        sel.innerHTML = emps.map(e =>
            `<option value="${escapeHtml(e.dept_id)}">${escapeHtml(e.dept_id)} — ${escapeHtml(e.name)}${e.dept_id === currentUser.dept_id ? " (you)" : ""}</option>`
        ).join("");
        sel.value = SCH.availMemberId;
    }

    const deptId = reviewer ? SCH.availMemberId : currentUser.dept_id;
    const query = deptId && deptId !== currentUser.dept_id ? `?dept_id=${encodeURIComponent(deptId)}` : "";
    const seq = ++SCH.loadSeq;
    try {
        const data = await apiRequest(`/api/schedule/${month}${query}`);
        if (seq !== SCH.loadSeq) return;
        const sameView = SCH.avail && SCH.avail.month.month === data.month.month && SCH.avail.member.dept_id === data.member.dept_id;
        SCH.avail = data;
        SCH.availStartHour = Number(data.availability.start_hour) || 0;
        SCH.availDays = new Map(Object.entries(data.availability.days || {}).map(([k, v]) => [k, Number(v)]));
        SCH.availDirty = false;
        if (!sameView) SCH.availWeekIdx = 0;
        renderAvailability();
    } catch (err) {
        if (seq === SCH.loadSeq) {
            setScheduleMonthSub("");
            showAlert(err.message, "error");
        }
    }
}

function schChangeAvailMember(deptId) {
    if (!schConfirmDiscardAvailability()) {
        document.getElementById("availMemberSelect").value = SCH.availMemberId;
        return;
    }
    SCH.availMemberId = deptId;
    loadAvailabilityView();
}

function schAvailEditable() {
    return Boolean(SCH.avail) && (SCH.avail.month.availability_open || schIsReviewer());
}

// Mondays of every week that touches the month
function schAvailWeeks() {
    const dates = schMonthDates(SCH.avail.month.month);
    const weeks = [];
    let monday = schAddDays(dates[0], -schWeekdayIndex(dates[0]));
    while (monday <= dates[dates.length - 1]) {
        weeks.push(monday);
        monday = schAddDays(monday, 7);
    }
    return weeks;
}

function schAvailWeekDates() {
    const weeks = schAvailWeeks();
    const monday = weeks[Math.min(SCH.availWeekIdx, weeks.length - 1)];
    return Array.from({ length: 7 }, (_, i) => schAddDays(monday, i));
}

function schInAvailMonth(iso) {
    return iso.slice(0, 7) === SCH.avail.month.month;
}

function renderAvailability() {
    const data = SCH.avail;
    const month = data.month;
    const tpl = data.template;
    const editable = schAvailEditable();
    const forOther = data.member.dept_id !== currentUser.dept_id;

    document.getElementById("availTitle").textContent =
        `${month.label} Availability${forOther ? ` — ${data.member.name}` : ""}`;

    const badge = document.getElementById("availStatusBadge");
    const due = formatHumanDate(month.availability_deadline);
    let status;
    if (month.status === "published") {
        badge.className = "badge badge-approved";
        badge.textContent = "Published";
        status = "This schedule is published. Sign up for open shifts on the Schedule tab.";
    } else if (month.availability_open) {
        badge.className = "badge badge-pending";
        badge.textContent = "Open";
        const d = Number(month.days_until_deadline);
        status = `Due ${due} • ${d <= 0 ? "due today" : `${d} day${d === 1 ? "" : "s"} left`}`;
    } else {
        badge.className = "badge badge-rejected";
        badge.textContent = "Closed";
        status = `Closed ${due}. Contact an officer to make changes.`;
    }
    if (!month.availability_open && schIsReviewer()) status += " Officers can still edit.";
    const sub = data.submission;
    status += sub
        ? ` • Submitted ${new Date(sub.updated_at || sub.submitted_at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`
        : " • Not submitted yet";
    document.getElementById("availStatusText").textContent = status;
    setScheduleMonthSub(
        month.status === "published" ? "Schedule published" : month.availability_open ? `Availability due ${due}` : "Availability closed"
    );

    const disabledAttr = editable ? "" : "disabled";
    document.getElementById("availQuickBar").innerHTML =
        `<span class="preset-label">Quick fill:</span>` +
        tpl.shifts.map(sh =>
            `<button type="button" class="preset-chip" ${disabledAttr} onclick="schAvailQuick('${sh.key}')" title="Toggle ${escapeHtml(sh.start)}–${escapeHtml(sh.end)} on every day of the month">All ${escapeHtml(sh.label)}s</button>`
        ).join("") +
        `<button type="button" class="preset-chip" ${disabledAttr} onclick="schAvailCopyWeek()" title="Repeat this week's pattern on every other week">Copy Week to All</button>` +
        `<button type="button" class="preset-chip" ${disabledAttr} onclick="schAvailQuick('')">Clear All</button>`;

    const maxInput = document.getElementById("availMaxHours");
    const notesInput = document.getElementById("availNotes");
    maxInput.value = sub && sub.max_hours !== null && sub.max_hours !== undefined ? sub.max_hours : "";
    notesInput.value = sub ? (sub.notes || "") : "";
    maxInput.disabled = !editable;
    notesInput.disabled = !editable;

    const btn = document.getElementById("availSaveBtn");
    btn.disabled = !editable;
    btn.textContent = sub ? "Update Availability" : "Submit Availability";

    const lockNote = document.getElementById("availLockedNote");
    if (editable) {
        lockNote.style.display = "none";
    } else {
        lockNote.textContent = month.status === "published"
            ? `Read-only: the ${month.label} schedule is already published.`
            : `Read-only: ${month.label} availability closed ${due}. Contact an officer to make changes.`;
        lockNote.style.display = "block";
    }

    renderAvailWeek();
    schUpdateAvailSummary();
}

function renderAvailWeek() {
    const data = SCH.avail;
    const editable = schAvailEditable();
    const weeks = schAvailWeeks();
    SCH.availWeekIdx = Math.max(0, Math.min(SCH.availWeekIdx, weeks.length - 1));
    const dates = schAvailWeekDates();
    const startHour = SCH.availStartHour;

    // Week tabs with hour totals
    document.getElementById("availWeekTabs").innerHTML = weeks.map((monday, i) => {
        let hrs = 0;
        for (let d = 0; d < 7; d++) {
            const iso = schAddDays(monday, d);
            if (schInAvailMonth(iso)) hrs += schPopcount(SCH.availDays.get(iso) || 0);
        }
        return `<button type="button" class="sch-week-tab ${i === SCH.availWeekIdx ? "active" : ""}" onclick="schAvailGoWeek(${i})">
            ${schShortRange(monday, schAddDays(monday, 6))}<small>${hrs ? `${hrs}h` : "—"}</small></button>`;
    }).join("");
    document.getElementById("availWeekPrev").disabled = SCH.availWeekIdx === 0;
    document.getElementById("availWeekNext").disabled = SCH.availWeekIdx >= weeks.length - 1;

    // Shift bands for the hour labels
    const bandByRow = new Array(24).fill("");
    data.template.shifts.forEach(sh => {
        schShiftRows(sh, startHour).forEach(r => { if (!bandByRow[r]) bandByRow[r] = schShiftClass(sh.key); });
    });

    const cells = [`<div class="sch-hg-corner"></div>`];
    dates.forEach((iso, c) => {
        const d = parseIsoDateLocal(iso);
        const off = !schInAvailMonth(iso);
        cells.push(`
            <button type="button" class="sch-hg-day ${off ? "is-off-month" : ""}" data-col="${c}" ${off || !editable ? "disabled" : ""}
                    title="${off ? "Not in this month" : `Toggle all 24 hours on ${escapeHtml(schShortDate(iso))}`}">
                <span>${SCH_DOW[c]}</span><b>${d.getDate()}</b><small class="sch-hg-count"></small>
            </button>`);
    });
    for (let r = 0; r < 24; r++) {
        const hour = (startHour + r) % 24;
        const nextDay = startHour + r >= 24;
        const midnight = hour === 0 && r > 0;
        cells.push(`
            <button type="button" class="sch-hg-hour ${bandByRow[r]} ${midnight ? "is-midnight" : ""}" data-row="${r}" ${editable ? "" : "disabled"}
                    title="Toggle ${schPad(hour)}:00 across this week">
                ${schPad(hour)}<span class="sch-hg-min">:00</span>${nextDay && hour === 0 ? '<span class="sch-hg-next">+1</span>' : ""}
            </button>`);
        dates.forEach((iso, c) => {
            const off = !schInAvailMonth(iso);
            cells.push(`<div class="sch-hg-cell ${off ? "is-off-month" : ""} ${midnight ? "is-midnight" : ""} ${!editable ? "is-locked" : ""}" data-col="${c}" data-row="${r}"></div>`);
        });
    }
    const grid = document.getElementById("availHourGrid");
    grid.innerHTML = cells.join("");
    schPaintAvailGrid();
}

function schPaintAvailGrid() {
    const dates = schAvailWeekDates();
    const grid = document.getElementById("availHourGrid");
    grid.querySelectorAll(".sch-hg-cell").forEach(cell => {
        const mask = SCH.availDays.get(dates[+cell.dataset.col]) || 0;
        cell.classList.toggle("on", Boolean((mask >>> +cell.dataset.row) & 1));
    });
    grid.querySelectorAll(".sch-hg-day").forEach(btn => {
        const iso = dates[+btn.dataset.col];
        const n = schInAvailMonth(iso) ? schPopcount(SCH.availDays.get(iso) || 0) : 0;
        btn.querySelector(".sch-hg-count").textContent = n ? `${n}h` : "";
    });
}

function schAvailGoWeek(index) {
    SCH.availWeekIdx = index;
    renderAvailWeek();
}

function schAvailShiftWeek(delta) {
    SCH.availWeekIdx += delta;
    renderAvailWeek();
}

function schSetAvailMask(iso, mask) {
    if (mask) SCH.availDays.set(iso, mask >>> 0);
    else SCH.availDays.delete(iso);
}

function schMarkAvailDirty() {
    SCH.availDirty = true;
    schUpdateAvailSummary();
}

// --- drag selection (when2meet style: drag a rectangle; first cell decides add vs. remove) ---

// Grid cell under a screen point, from the grid's geometry (clamped to the grid, so a drag
// that wanders past the edge keeps extending to the last row/column)
function schAvailCellAt(clientX, clientY) {
    const grid = document.getElementById("availHourGrid");
    const first = grid.querySelector('.sch-hg-cell[data-col="0"][data-row="0"]');
    const last = grid.querySelector('.sch-hg-cell[data-col="6"][data-row="23"]');
    if (!first || !last) return null;
    const a = first.getBoundingClientRect();
    const z = last.getBoundingClientRect();
    const colW = (z.right - a.left) / 7;
    const rowH = (z.bottom - a.top) / 24;
    if (colW <= 0 || rowH <= 0) return null;
    const col = Math.min(6, Math.max(0, Math.floor((clientX - a.left) / colW)));
    const row = Math.min(23, Math.max(0, Math.floor((clientY - a.top) / rowH)));
    return { col, row };
}

function schAvailPointerDown(e) {
    const cell = e.target.closest(".sch-hg-cell");
    if (!cell) return;
    if (!schAvailEditable()) {
        schToast("This month's availability is closed, so it's read-only. Contact an officer to change it.", "error");
        return;
    }
    if (cell.classList.contains("is-off-month")) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;
    e.preventDefault();
    const grid = document.getElementById("availHourGrid");
    try {
        grid.setPointerCapture(e.pointerId); // keep receiving moves on touch screens and outside the grid
    } catch (_) { /* older browsers */ }
    const col = +cell.dataset.col;
    const row = +cell.dataset.row;
    const iso = schAvailWeekDates()[col];
    const isOn = Boolean(((SCH.availDays.get(iso) || 0) >>> row) & 1);
    SCH.availDrag = { col0: col, row0: row, on: !isOn, base: new Map(SCH.availDays), last: "", pointerId: e.pointerId };
    schAvailApplyDrag(col, row);
}

function schAvailPointerMove(e) {
    const drag = SCH.availDrag;
    if (!drag || (drag.pointerId !== undefined && e.pointerId !== drag.pointerId)) return;
    e.preventDefault();
    const pos = schAvailCellAt(e.clientX, e.clientY);
    if (pos) schAvailApplyDrag(pos.col, pos.row);
}

function schAvailPointerUp() {
    if (!SCH.availDrag) return;
    SCH.availDrag = null;
    renderAvailWeek(); // refresh week-tab totals
    schMarkAvailDirty();
}

function schAvailApplyDrag(col, row) {
    const drag = SCH.availDrag;
    const key = `${col},${row}`;
    if (drag.last === key) return;
    drag.last = key;
    const dates = schAvailWeekDates();
    const [c1, c2] = [Math.min(drag.col0, col), Math.max(drag.col0, col)];
    const [r1, r2] = [Math.min(drag.row0, row), Math.max(drag.row0, row)];
    let rectMask = 0;
    for (let r = r1; r <= r2; r++) rectMask |= 1 << r;
    SCH.availDays = new Map(drag.base);
    for (let c = c1; c <= c2; c++) {
        const iso = dates[c];
        if (!schInAvailMonth(iso)) continue;
        const mask = SCH.availDays.get(iso) || 0;
        schSetAvailMask(iso, drag.on ? (mask | rectMask) : (mask & ~rectMask));
    }
    schPaintAvailGrid();
    schUpdateAvailSummary();
}

function schAvailGridClick(e) {
    if (!schAvailEditable()) return;
    const dates = schAvailWeekDates();
    const dayBtn = e.target.closest(".sch-hg-day");
    if (dayBtn && !dayBtn.disabled) {
        const iso = dates[+dayBtn.dataset.col];
        schSetAvailMask(iso, (SCH.availDays.get(iso) || 0) === SCH_FULL_DAY ? 0 : SCH_FULL_DAY);
        renderAvailWeek();
        schMarkAvailDirty();
        return;
    }
    const hourBtn = e.target.closest(".sch-hg-hour");
    if (hourBtn && !hourBtn.disabled) {
        const bit = 1 << +hourBtn.dataset.row;
        const inMonth = dates.filter(schInAvailMonth);
        const allOn = inMonth.every(iso => (SCH.availDays.get(iso) || 0) & bit);
        inMonth.forEach(iso => {
            const mask = SCH.availDays.get(iso) || 0;
            schSetAvailMask(iso, allOn ? (mask & ~bit) : (mask | bit));
        });
        renderAvailWeek();
        schMarkAvailDirty();
    }
}

function schAvailQuick(shiftKey) {
    if (!schAvailEditable()) return;
    const dates = schMonthDates(SCH.avail.month.month);
    if (!shiftKey) {
        if (SCH.availDays.size && !confirm("Clear all of your availability for this month?")) return;
        SCH.availDays = new Map();
    } else {
        const sh = SCH.avail.template.shifts.find(s => s.key === shiftKey);
        let shiftMask = 0;
        schShiftRows(sh, SCH.availStartHour).forEach(r => { shiftMask |= 1 << r; });
        const allOn = dates.every(iso => ((SCH.availDays.get(iso) || 0) & shiftMask) === shiftMask);
        dates.forEach(iso => {
            const mask = SCH.availDays.get(iso) || 0;
            schSetAvailMask(iso, allOn ? (mask & ~shiftMask) : (mask | shiftMask));
        });
    }
    renderAvailWeek();
    schMarkAvailDirty();
}

function schAvailCopyWeek() {
    if (!schAvailEditable()) return;
    const source = schAvailWeekDates();
    const pattern = source.map(iso => (schInAvailMonth(iso) ? (SCH.availDays.get(iso) || 0) : null));
    const targets = schMonthDates(SCH.avail.month.month).filter(iso => !source.includes(iso));
    const overwrites = targets.some(iso => pattern[schWeekdayIndex(iso)] !== null && SCH.availDays.get(iso));
    if (overwrites && !confirm("Copy this week's hours to every other week of the month? Those weeks will be replaced.")) return;
    targets.forEach(iso => {
        const mask = pattern[schWeekdayIndex(iso)];
        if (mask !== null) schSetAvailMask(iso, mask);
    });
    renderAvailWeek();
    schMarkAvailDirty();
    showAlert("Copied this week's pattern to the rest of the month.");
}

function schUpdateAvailSummary() {
    const data = SCH.avail;
    const tpl = data.template;
    let total = 0;
    SCH.availDays.forEach((mask, iso) => { if (schInAvailMonth(iso)) total += schPopcount(mask); });

    const hours = schMaskHours(SCH.availDays, SCH.availStartHour);
    const threshold = (Number(tpl.min_coverage_pct) || 75) / 100;
    const counts = tpl.shifts.map(sh => {
        let full = 0;
        let partial = 0;
        schMonthDates(data.month.month).forEach(iso => {
            const seat = schSeatInterval({ shift_date: iso, start_time: sh.start, end_time: sh.end });
            const cov = schCoverage(hours, seat[0], seat[1]);
            if (cov.share >= 0.999) full++;
            else if (cov.share > 0 && cov.share + 1e-9 >= threshold) partial++;
        });
        return { label: sh.label, full, partial };
    });

    document.getElementById("availSummaryValue").textContent = `${total} hr${total === 1 ? "" : "s"}`;
    const parts = counts.filter(c => c.full + c.partial).map(c =>
        `${c.full + c.partial} ${c.label}${c.partial ? ` (${c.partial} partial)` : ""}`
    );
    document.getElementById("availSummarySub").textContent = total
        ? (parts.length ? `Can cover ${parts.join(" • ")} shifts` : `Not enough for a full shift (needs ${tpl.min_coverage_pct}% of one)`)
        : "Nothing selected. Submitting tells the officer you're unavailable this month.";
    document.getElementById("availDirtyText").textContent = SCH.availDirty ? "Unsaved changes" : "";
}

async function schSaveAvailability() {
    const data = SCH.avail;
    if (!data) return;
    const btn = document.getElementById("availSaveBtn");
    const maxRaw = document.getElementById("availMaxHours").value.trim();
    const days = {};
    SCH.availDays.forEach((mask, iso) => { if (mask && schInAvailMonth(iso)) days[iso] = mask; });
    btn.disabled = true;
    try {
        const res = await apiRequest(`/api/schedule/${data.month.month}/availability`, {
            method: "PUT",
            body: JSON.stringify({
                dept_id: data.member.dept_id,
                start_hour: SCH.availStartHour,
                days,
                notes: document.getElementById("availNotes").value.trim(),
                max_hours: maxRaw === "" ? null : Number(maxRaw)
            })
        });
        data.submission = res.submission;
        data.availability = res.availability;
        SCH.availDirty = false;
        if (SCH.view && SCH.view.upcoming_availability && SCH.view.upcoming_availability.month === data.month.month
            && data.member.dept_id === currentUser.dept_id) {
            SCH.view.upcoming_availability.submitted = true;
        }
        showAlert(res.message);
        renderAvailability();
    } catch (err) {
        showAlert(err.message, "error");
    } finally {
        btn.disabled = !schAvailEditable();
    }
}

// =========================================================================
// BUILDER TAB (officers)
// =========================================================================

async function loadBuilderView() {
    const month = SCH.monthByTab.tabScheduleBuilder;
    const seq = ++SCH.loadSeq;
    try {
        const data = await apiRequest(`/api/schedule/${month}/builder`);
        if (seq !== SCH.loadSeq) return;
        SCH.builder = data;
        SCH.employees = data.employees.slice();
        if (SCH.focusDept && !data.employees.some(e => e.dept_id === SCH.focusDept)) SCH.focusDept = "";
        schIndexBuilder();
        renderBuilderStatus();
        renderBuilderHours();
        renderBuilderGrid();
        renderTemplateEditor();
    } catch (err) {
        if (seq === SCH.loadSeq) {
            setScheduleMonthSub("");
            showAlert(err.message, "error");
        }
    }
}

function schIndexBuilder() {
    const b = SCH.builder;
    const startHour = Number(b.avail_start_hour) || 0;
    const availHours = new Map();
    Object.entries(b.availability || {}).forEach(([dept, days]) => availHours.set(dept, schMaskHours(days, startHour)));
    const hoursById = new Map(((b.hours && b.hours.members) || []).map(r => [r.dept_id, r]));
    const assignments = new Map();
    b.slots.concat(b.edge_slots || []).forEach(s => {
        if (!s.dept_id) return;
        if (!assignments.has(s.dept_id)) assignments.set(s.dept_id, []);
        assignments.get(s.dept_id).push({ id: s.id, iv: schWorkInterval(s) });
    });
    const empById = new Map(b.employees.map(e => [e.dept_id, e]));
    SCH.builderIdx = {
        availHours,
        hoursById,
        assignments,
        empById,
        threshold: (Number(b.template.min_coverage_pct) || 75) / 100,
        driverPositions: schDriverPositions(b.template)
    };
}

function schIsBusy(deptId, iv, slotId) {
    const list = SCH.builderIdx.assignments.get(deptId);
    if (!list) return false;
    return list.some(o => o.id !== slotId && iv[0] < o.iv[1] && o.iv[0] < iv[1]);
}

function schIsDriverSlot(slot) {
    const e = SCH.builderIdx.empById.get(slot.dept_id);
    return e ? Boolean(e.is_driver) : Boolean(slot.is_driver);
}

function renderBuilderStatus() {
    const b = SCH.builder;
    const m = b.month;
    const cov = b.coverage;
    const totals = b.hours.totals;
    const drivers = cov.drivers || { required: 0, covered: 0 };
    const pct = cov.total ? Math.round((cov.filled / cov.total) * 100) : 0;
    const openByShift = Object.values(cov.by_shift)
        .filter(x => x.total > 0)
        .map(x => `${x.label} ${x.total - x.filled}`)
        .join(" • ");

    document.getElementById("builderStats").innerHTML = `
        <div class="stat-card stat-approved">
            <div class="stat-label">Seats Filled</div>
            <div class="stat-value">${cov.filled}<span class="sch-stat-of">/ ${cov.total}</span></div>
            <div class="stat-sub">${pct}% covered</div>
        </div>
        <div class="stat-card stat-primary">
            <div class="stat-label">Open Seats</div>
            <div class="stat-value">${cov.open}</div>
            <div class="stat-sub">${escapeHtml(openByShift)}</div>
        </div>
        <div class="stat-card ${drivers.covered < drivers.required ? "stat-pending" : "stat-approved"}">
            <div class="stat-label">Drivers</div>
            <div class="stat-value">${drivers.covered}<span class="sch-stat-of">/ ${drivers.required}</span></div>
            <div class="stat-sub">station shifts with a driver</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Availability In</div>
            <div class="stat-value">${totals.members_submitted}<span class="sch-stat-of">/ ${totals.member_count}</span></div>
            <div class="stat-sub">members responded</div>
        </div>
        <div class="stat-card stat-pending">
            <div class="stat-label">Projected OT</div>
            <div class="stat-value">${schHrs(totals.projected_ot_hours)}</div>
            <div class="stat-sub">${totals.members_with_ot} member${totals.members_with_ot === 1 ? "" : "s"} over threshold</div>
        </div>`;

    const published = m.status === "published";
    document.getElementById("builderTitle").innerHTML =
        `${escapeHtml(m.label)} <span class="badge ${published ? "badge-approved" : "badge-pending"}">${published ? "Published" : "Draft"}</span>`;
    document.getElementById("builderStatusText").textContent = published
        ? "Members can see this schedule and sign up for open seats. Changes here are live."
        : "Draft. Members can't see this schedule until you publish it.";
    setScheduleMonthSub(published ? "Published — edits are live" : "Draft — hidden from members");

    document.getElementById("builderActions").innerHTML =
        (published
            ? `<button type="button" class="btn btn-outline btn-sm" onclick="schPublishMonth(false)">Return to Draft</button>`
            : `<button type="button" class="btn btn-success btn-sm" onclick="schPublishMonth(true)">&#10003; Publish Schedule</button>`) +
        `<button type="button" class="btn btn-secondary btn-sm" onclick="schPrintSchedule('builder')">&#128424; Print</button>`;

    const autoCount = b.slots.filter(s => s.dept_id && s.assigned_via === "autofill").length;
    const undoBtn = document.getElementById("autofillUndoBtn");
    undoBtn.style.display = autoCount ? "" : "none";
    undoBtn.textContent = `Undo Auto-Fill (${autoCount})`;

    document.getElementById("builderDeadline").value = m.availability_deadline;
    const missing = b.employees.filter(e => !b.submissions[e.dept_id]);
    document.getElementById("builderMissingCount").textContent = `(${missing.length})`;
    document.getElementById("builderMissingList").innerHTML = missing.length
        ? missing.map(e =>
            `<button type="button" class="sch-chip" onclick="schOpenAvailability('${m.month}', '${escapeHtml(e.dept_id)}')" title="Enter availability for ${escapeHtml(e.name)}">${escapeHtml(e.name)}</button>`
        ).join("")
        : `<span class="sch-subtle">Everyone has responded. &#10003;</span>`;
}

function renderBuilderHours() {
    const b = SCH.builder;
    if (!b) return;
    const weeks = b.hours.weeks;
    const sort = document.getElementById("builderHoursSort").value;
    const rows = b.hours.members.slice();
    const byName = (x, y) => x.name.localeCompare(y.name);
    if (sort === "hours_desc") rows.sort((x, y) => y.month_hours - x.month_hours || byName(x, y));
    else if (sort === "hours_asc") rows.sort((x, y) => x.month_hours - y.month_hours || byName(x, y));
    else if (sort === "avail_desc") rows.sort((x, y) => y.available_hours - x.available_hours || byName(x, y));
    else rows.sort(byName);

    const first = b.month.first_day;
    const last = b.month.last_day;
    document.getElementById("builderHoursThead").innerHTML = `
        <tr>
            <th>Member</th>
            <th>Available</th>
            <th>Shifts</th>
            <th>Month</th>
            ${weeks.map(w => {
                const partial = w.start < first || w.end > last;
                return `<th title="${partial ? "Includes days from the neighboring month" : "Mon–Sun pay week"}">${schShortRange(w.start, w.end)}${partial ? "*" : ""}</th>`;
            }).join("")}
            <th>Max</th>
        </tr>`;

    const tbody = document.getElementById("builderHoursTbody");
    if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="${5 + weeks.length}" class="empty-row-cell" style="text-align: center; color: var(--text-muted);">No active members.</td></tr>`;
        document.getElementById("builderHoursTfoot").innerHTML = "";
        return;
    }

    tbody.innerHTML = rows.map(r => {
        const types = Object.entries(r.shifts_by_type || {});
        const typeText = types.length
            ? ` <span class="sch-subtle">(${types.map(([k, v]) => {
                const sh = b.template.shifts.find(s => s.key === k);
                return `${v}${sh ? sh.label.charAt(0) : k}`;
            }).join(" ")}${r.partial_shifts ? `, ${r.partial_shifts} partial` : ""})</span>`
            : "";
        const weekCells = weeks.map(w => {
            const h = r.by_week[w.start] || 0;
            const cell = h > r.ot_threshold
                ? `<span class="sch-hours-ot" title="Over ${r.ot_threshold}h">${schHrs(h)}h OT</span>`
                : h === 0 ? `<span class="sch-hours-zero">0</span>` : `${schHrs(h)}h`;
            return `<td data-label="${schShortRange(w.start, w.end)}">${cell}</td>`;
        }).join("");
        return `
            <tr class="sch-hours-row ${SCH.focusDept === r.dept_id ? "sch-focus-row" : ""}" onclick="schFocusMember('${escapeHtml(r.dept_id)}')" title="Highlight ${escapeHtml(r.name)} on the grid">
                <td data-label="Member">
                    <span>
                        <b>${escapeHtml(r.name)}</b>
                        <span class="badge ${r.ot_threshold === 53 ? "badge-2448" : "badge-role"}">${r.ot_threshold === 53 ? "24/48" : "Std"}</span>
                        ${r.is_driver ? '<span class="badge sch-driver-badge">Driver</span>' : ""}
                        ${r.availability_notes ? `<div class="sch-note-line">&ldquo;${escapeHtml(r.availability_notes)}&rdquo;</div>` : ""}
                    </span>
                </td>
                <td data-label="Available">${r.submitted
                    ? `${r.available_hours}h <span class="sch-subtle">(${r.available_shifts} shifts)</span>`
                    : '<span class="badge badge-pending">No response</span>'}</td>
                <td data-label="Shifts">${r.month_shifts}${typeText}</td>
                <td data-label="Month"><b>${schHrs(r.month_hours)}h</b>${r.over_max ? ' <span class="badge badge-ot">Over max</span>' : ""}</td>
                ${weekCells}
                <td data-label="Max">${r.max_hours !== null && r.max_hours !== undefined ? `${r.max_hours}h` : '<span class="sch-hours-zero">—</span>'}</td>
            </tr>`;
    }).join("");

    const t = b.hours.totals;
    document.getElementById("builderHoursTfoot").innerHTML = `
        <tr>
            <td colspan="3" data-label="Total">Total (${t.members_scheduled} scheduled)</td>
            <td data-label="Month">${schHrs(t.scheduled_hours)}h</td>
            <td colspan="${weeks.length + 1}" data-label="Projected OT">${t.projected_ot_hours > 0 ? `${schHrs(t.projected_ot_hours)}h projected OT` : "No projected OT"}</td>
        </tr>`;
}

function schFocusMember(deptId) {
    SCH.focusDept = SCH.focusDept === deptId ? "" : deptId;
    renderBuilderHours();
    renderBuilderGrid();
    if (SCH.focusDept) {
        const banner = document.getElementById("builderFocusBanner");
        if (banner) banner.scrollIntoView({ behavior: "smooth", block: "start" });
    }
}

function schToggleEditSeats(on) {
    SCH.editSeats = Boolean(on);
    renderBuilderGrid();
}

function schSeatSelectHtml(slot, emps, crew) {
    const idx = SCH.builderIdx;
    const week = schWeekStart(slot.shift_date);
    const seat = schSeatInterval(slot);
    const seatHours = (seat[1] - seat[0]) / 60;
    const available = [];
    const others = [];
    let currentFound = false;

    emps.forEach(e => {
        const h = idx.hoursById.get(e.dept_id) || {};
        const monthH = h.month_hours || 0;
        const weekH = (h.by_week || {})[week] || 0;
        const isCurrent = slot.dept_id === e.dept_id;
        if (isCurrent) currentFound = true;
        const cov = schCoverage(idx.availHours.get(e.dept_id), seat[0], seat[1]);
        const qualifies = cov.share > 0 && cov.share + 1e-9 >= idx.threshold;
        const partial = cov.share > 0 && cov.share < 0.999;
        const work = partial ? [cov.start, cov.end] : seat;
        const workHours = (work[1] - work[0]) / 60;
        const busy = !isCurrent && schIsBusy(e.dept_id, work, slot.id);
        const ot = !isCurrent && weekH + workHours > e.ot_threshold;
        const name = `${e.name}${e.is_driver ? " (D)" : ""}`;

        let label;
        if (isCurrent) {
            label = name;
        } else {
            label = name;
            if (qualifies && partial) label += ` · ${schMinToHhmm(cov.start)}–${schMinToHhmm(cov.end)}`;
            label += ` · ${schHrs(monthH)}h · wk ${schHrs(weekH)}h`;
            if (!qualifies && cov.share > 0) label += ` · only ${schHrs(workHours)}h avail`;
            if (busy) label += " · busy";
            else if (ot) label += " · OT";
        }
        const html = `<option value="${escapeHtml(e.dept_id)}" ${isCurrent ? "selected" : ""} ${busy ? "disabled" : ""}>${escapeHtml(label)}</option>`;
        (qualifies ? available : others).push({ html, monthH, name: e.name, full: !partial, driver: e.is_driver });
    });
    // Driver-needed seats list drivers first; otherwise full coverage first, then fewest hours
    const driverFirst = crew.needsDriver && !crew.hasDriver;
    available.sort((a, b) =>
        (driverFirst ? (b.driver ? 1 : 0) - (a.driver ? 1 : 0) : 0) ||
        (b.full ? 1 : 0) - (a.full ? 1 : 0) ||
        a.monthH - b.monthH ||
        a.name.localeCompare(b.name)
    );

    const heldForDriver = !slot.dept_id && crew.needsDriver && crew.heldForDriver;
    let options = `<option value="">${heldForDriver ? "— Needs driver —" : "— Open —"}</option>`;
    if (slot.dept_id && !currentFound) {
        options += `<option value="${escapeHtml(slot.dept_id)}" selected>${escapeHtml(slot.employee_name || slot.dept_id)} (inactive)</option>`;
    }
    options += `<optgroup label="Available (${available.length})">${available.map(o => o.html).join("")}</optgroup>`;
    options += `<optgroup label="Not on availability">${others.map(o => o.html).join("")}</optgroup>`;

    let cls = heldForDriver ? "is-open is-driver-needed" : "is-open";
    let title = heldForDriver ? "Open seat — this crew still needs a qualified driver" : "Open seat";
    if (slot.dept_id) {
        const cov = schCoverage(idx.availHours.get(slot.dept_id), seat[0], seat[1]);
        const assigneeAvail = cov.share > 0 && cov.share + 1e-9 >= idx.threshold;
        const h = idx.hoursById.get(slot.dept_id);
        const emp = idx.empById.get(slot.dept_id);
        const weekH = h ? (h.by_week[week] || 0) : 0;
        cls = "is-filled";
        title = slot.employee_name || slot.dept_id;
        if (h) title += ` — ${schHrs(h.month_hours)}h this month, ${schHrs(weekH)}h this week`;
        if (slot.is_partial) title += ` — works ${slot.work_start}–${slot.work_end} (${schHrs(slot.hours)} of ${schHrs(seatHours)}h)`;
        if (!assigneeAvail) {
            cls += " is-unavail";
            title += " — not on their availability";
        }
        if (emp && weekH > emp.ot_threshold) {
            cls += " is-ot";
            title += " — overtime week";
        }
        if (slot.assigned_via === "autofill") {
            cls += " is-auto";
            title += " — auto-filled";
        }
        if (slot.assigned_via === "signup") title += " — signed up";
        if (SCH.focusDept && slot.dept_id === SCH.focusDept) cls += " is-focus";
    }

    const aria = `${slot.shift_label} ${slot.position_label} seat ${slot.seat}, ${schShortDate(slot.shift_date)}`;
    const select = `<select class="sch-seat-select ${cls}" data-slot-id="${slot.id}" title="${escapeHtml(title)}" aria-label="${escapeHtml(aria)}" onchange="schAssignSlot(${slot.id}, this)">${options}</select>`;
    const remove = SCH.editSeats
        ? `<button type="button" class="sch-seat-x" onclick="schRemoveSeat(${slot.id})" title="Remove this seat" aria-label="Remove seat">&times;</button>`
        : "";
    const meta = slot.dept_id && slot.is_partial
        ? `<div class="sch-seat-meta">${escapeHtml(slot.work_start)}–${escapeHtml(slot.work_end)} · ${schHrs(slot.hours)}h</div>`
        : "";
    return `<div class="sch-seat-row">${select}${remove}</div>${meta}`;
}

function renderBuilderGrid(focusSlotId) {
    const b = SCH.builder;
    if (!b) return;
    const idx = SCH.builderIdx;
    const filter = document.getElementById("builderGridFilter").value;
    const cols = schGridColumns(b.slots, b.template);
    const byDate = schSlotsByDate(b.slots);
    const todayIso = toIsoDate(new Date());
    const emps = b.employees.slice().sort((x, y) => x.name.localeCompare(y.name));
    document.getElementById("builderEditSeats").checked = SCH.editSeats;

    const banner = document.getElementById("builderFocusBanner");
    const focusEmp = SCH.focusDept ? idx.empById.get(SCH.focusDept) : null;
    const focusHours = focusEmp ? idx.availHours.get(SCH.focusDept) : null;
    if (focusEmp) {
        const h = idx.hoursById.get(SCH.focusDept) || {};
        banner.innerHTML = `
            <span>Highlighting <b>${escapeHtml(focusEmp.name)}</b>${focusEmp.is_driver ? " (driver)" : ""}: blue cells = available,
                outlined = assigned (${schHrs(h.month_hours)}h, ${h.month_shifts || 0} shifts).</span>
            <button type="button" class="btn btn-outline btn-sm" onclick="schFocusMember('${escapeHtml(focusEmp.dept_id)}')">Clear</button>`;
        banner.style.display = "flex";
    } else {
        banner.style.display = "none";
        banner.innerHTML = "";
    }

    const crewIssue = (iso, c) => {
        const seats = (byDate.get(iso) || new Map()).get(c.key) || [];
        const state = schCrewDriverState(seats, schIsDriverSlot);
        return idx.driverPositions.has(c.position_key) && state.filled > 0 && !state.hasDriver;
    };
    const dates = schMonthDates(b.month.month).filter(iso => {
        if (filter === "all") return true;
        const day = byDate.get(iso);
        if (filter === "open") return day && Array.from(day.values()).flat().some(s => !s.dept_id);
        if (filter === "driver") return cols.some(c => crewIssue(iso, c));
        return true;
    });

    if (!dates.length) {
        document.getElementById("builderGrid").innerHTML =
            `<tbody><tr><td class="empty-row-cell" style="text-align: center; color: var(--text-muted);">${filter === "driver" ? "Every staffed station shift has a driver." : "Every seat is filled."} &#10003;</td></tr></tbody>`;
        return;
    }

    const body = dates.map(iso => {
        const day = byDate.get(iso) || new Map();
        const all = Array.from(day.values()).flat();
        const filled = all.filter(s => s.dept_id).length;
        const cov = `<span class="sch-day-cov ${filled === all.length ? "is-full" : ""}" title="Seats filled">${filled}/${all.length}</span>`;
        const cells = cols.map(c => {
            const seats = day.get(c.key) || [];
            const state = schCrewDriverState(seats, schIsDriverSlot);
            state.needsDriver = idx.driverPositions.has(c.position_key);
            let focusCls = "";
            if (focusHours) {
                const seat = schSeatInterval({ shift_date: iso, start_time: c.start_time, end_time: c.end_time });
                const share = schCoverage(focusHours, seat[0], seat[1]).share;
                if (share > 0 && share + 1e-9 >= idx.threshold) focusCls = "sch-focus-avail";
            }
            const selects = seats.map(s => schSeatSelectHtml(s, emps, state)).join("");
            const warn = state.needsDriver && state.filled > 0 && !state.hasDriver
                ? `<span class="sch-driver-warn" title="This crew has no qualified driver">&#9888; No driver</span>`
                : "";
            const add = SCH.editSeats
                ? `<button type="button" class="sch-add-seat" onclick="schAddSeat('${iso}', '${c.shift_key}', '${c.position_key}')">+ Seat</button>`
                : "";
            const foot = warn || add ? `<div class="sch-cell-foot">${warn}${add}</div>` : "";
            return `<td data-label="${escapeHtml(c.shift_label)} · ${escapeHtml(c.position_label)}" class="${focusCls}"><div class="sch-seat-stack">${selects || (SCH.editSeats ? "" : '<span class="sch-muted-dash">—</span>')}${foot}</div></td>`;
        }).join("");
        return `<tr class="${schRowClass(iso, todayIso)}">${schDateCellHtml(iso, cov)}${cells}</tr>`;
    }).join("");

    document.getElementById("builderGrid").innerHTML = schGridHeadHtml(cols, idx.driverPositions) + `<tbody>${body}</tbody>`;

    if (focusSlotId) {
        const el = document.querySelector(`select[data-slot-id="${focusSlotId}"]`);
        if (el) el.focus({ preventScroll: true });
    }
}

// Apply a Builder mutation response ({hours, coverage}) and re-render
function schApplyBuilderState(res, focusSlotId) {
    const b = SCH.builder;
    if (res.hours) b.hours = res.hours;
    if (res.coverage) b.coverage = res.coverage;
    schIndexBuilder();
    renderBuilderStatus();
    renderBuilderHours();
    renderBuilderGrid(focusSlotId);
}

async function schAssignSlot(slotId, selectEl) {
    const deptId = selectEl.value;
    selectEl.disabled = true;
    try {
        const res = await apiRequest(`/api/schedule/slots/${slotId}`, {
            method: "PUT",
            body: JSON.stringify({ dept_id: deptId || null })
        });
        const b = SCH.builder;
        const i = b.slots.findIndex(s => s.id === slotId);
        if (i >= 0) b.slots[i] = res.slot;
        schApplyBuilderState(res, slotId);
        const h = res.slot.dept_id ? SCH.builderIdx.hoursById.get(res.slot.dept_id) : null;
        if (h) {
            const partial = res.slot.is_partial ? ` for ${res.slot.work_start}–${res.slot.work_end}` : "";
            schToast(`${res.slot.employee_name} assigned${partial} (${schHrs(h.month_hours)}h this month)`);
        } else {
            schToast(res.message);
        }
    } catch (err) {
        schToast(err.message, "error");
        renderBuilderGrid(slotId);
    }
}

async function schAddSeat(iso, shiftKey, positionKey) {
    const b = SCH.builder;
    try {
        const res = await apiRequest(`/api/schedule/${b.month.month}/slots`, {
            method: "POST",
            body: JSON.stringify({ shift_date: iso, shift_key: shiftKey, position_key: positionKey })
        });
        b.slots.push(res.slot);
        schApplyBuilderState(res, res.slot.id);
        schToast(`Seat added: ${schShortDate(iso)} ${res.slot.shift_label} · ${res.slot.position_label}`);
    } catch (err) {
        schToast(err.message, "error");
    }
}

async function schRemoveSeat(slotId) {
    const b = SCH.builder;
    const slot = b.slots.find(s => s.id === slotId);
    if (!slot) return;
    let force = false;
    if (slot.dept_id) {
        if (!confirm(`Remove this seat? ${slot.employee_name || slot.dept_id} will be unassigned from ${slot.shift_label} ${slot.position_label} on ${formatHumanDate(slot.shift_date)}.`)) return;
        force = true;
    }
    try {
        const res = await apiRequest(`/api/schedule/slots/${slotId}${force ? "?force=true" : ""}`, { method: "DELETE" });
        b.slots = b.slots.filter(s => s.id !== slotId);
        schApplyBuilderState(res);
        schToast(`Seat removed: ${schShortDate(slot.shift_date)} ${slot.shift_label} · ${slot.position_label}`);
    } catch (err) {
        schToast(err.message, "error");
    }
}

async function schAutoFill() {
    const b = SCH.builder;
    const allowOt = document.getElementById("autofillAllowOt").checked;
    const msg = `Auto-fill the ${b.coverage.open} open seats in ${b.month.label}?\n\n` +
        "It uses submitted availability, puts a qualified driver on each station first, balances hours " +
        `(fewest first), and avoids double-booking and stretches over 24 hours${allowOt ? "" : " and overtime"}. ` +
        "Seats you've already filled aren't changed, and you can undo it.";
    if (!confirm(msg)) return;
    const btn = document.getElementById("autofillBtn");
    btn.disabled = true;
    btn.textContent = "Filling…";
    try {
        const res = await apiRequest(`/api/schedule/${b.month.month}/autofill`, {
            method: "POST",
            body: JSON.stringify({ allow_ot: allowOt })
        });
        showAlert(res.message);
        await loadBuilderView();
    } catch (err) {
        showAlert(err.message, "error");
    } finally {
        btn.disabled = false;
        btn.innerHTML = "&#9889; Auto-Fill Open Seats";
    }
}

async function schClearAutoFill() {
    const b = SCH.builder;
    const count = b.slots.filter(s => s.dept_id && s.assigned_via === "autofill").length;
    if (!confirm(`Clear the ${count} seats filled by Auto-Fill? Seats you assigned by hand or members signed up for stay as they are.`)) return;
    try {
        const res = await apiRequest(`/api/schedule/${b.month.month}/autofill/clear`, { method: "POST", body: "{}" });
        showAlert(res.message);
        await loadBuilderView();
    } catch (err) {
        showAlert(err.message, "error");
    }
}

async function schPublishMonth(publish) {
    const b = SCH.builder;
    const missingDrivers = (b.coverage.drivers && b.coverage.drivers.missing) || 0;
    const msg = publish
        ? `Publish the ${b.month.label} schedule?\n\nMembers will see it and can sign up for the ${b.coverage.open} open seats.` +
          (missingDrivers ? `\n\nHeads up: ${missingDrivers} station shifts don't have a qualified driver yet.` : "")
        : `Return ${b.month.label} to draft?\n\nMembers will no longer see it until you publish again.`;
    if (!confirm(msg)) return;
    try {
        const res = await apiRequest(`/api/schedule/${b.month.month}/publish`, {
            method: "POST",
            body: JSON.stringify({ published: publish })
        });
        showAlert(res.message);
        await loadBuilderView();
    } catch (err) {
        showAlert(err.message, "error");
    }
}

async function schSaveDeadline() {
    const b = SCH.builder;
    const value = document.getElementById("builderDeadline").value;
    if (!b || !value) return;
    try {
        const res = await apiRequest(`/api/schedule/${b.month.month}`, {
            method: "PUT",
            body: JSON.stringify({ availability_deadline: value })
        });
        b.month = res.month;
        renderBuilderStatus();
        showAlert(`Availability for ${res.month.label} is now due ${formatHumanDate(res.month.availability_deadline)}.`);
    } catch (err) {
        showAlert(err.message, "error");
    }
}

// --- Staffing levels (template) editor ---

function schTimeOptionsHtml(selected, includeMidnight) {
    const items = [];
    for (let h = 0; h < 24; h++) {
        for (const m of [0, 15, 30, 45]) {
            const v = `${schPad(h)}:${schPad(m)}`;
            items.push(`<option value="${v}" ${v === selected ? "selected" : ""}>${v}</option>`);
        }
    }
    if (includeMidnight) items.push(`<option value="24:00" ${selected === "24:00" ? "selected" : ""}>24:00</option>`);
    return items.join("");
}

function renderTemplateEditor() {
    const b = SCH.builder;
    const tpl = b.template;
    document.getElementById("builderTemplateBody").innerHTML = `
        <p class="sch-subtle" style="margin: 0.75rem 0 1rem;">
            Seats per shift, used whenever a new month is started. <b>Save &amp; Apply</b> also updates
            ${escapeHtml(b.month.label)}: missing seats are added and extra open seats removed (including ones
            added by hand). Filled seats are never removed.
        </p>
        <div class="table-responsive">
            <table class="data-table sch-template-table">
                <thead>
                    <tr>
                        <th>Shift</th><th>Start</th><th>End</th>
                        ${tpl.positions.map(p => `<th>${escapeHtml(p.label)}</th>`).join("")}
                        <th>Hours</th>
                    </tr>
                </thead>
                <tbody>
                    <tr class="sch-template-driver-row">
                        <td data-label="Shift"><b>Driver required</b></td>
                        <td data-label="Start"></td><td data-label="End"></td>
                        ${tpl.positions.map(p => `
                            <td data-label="${escapeHtml(p.label)} driver">
                                <label class="sch-check"><input type="checkbox" id="tplDriver_${p.key}" ${p.requires_driver ? "checked" : ""}> Driver</label>
                            </td>`).join("")}
                        <td data-label="Hours"></td>
                    </tr>
                    ${tpl.shifts.map((sh, i) => `
                        <tr>
                            <td data-label="Shift"><b>${escapeHtml(sh.label)}</b></td>
                            <td data-label="Start"><select class="form-control" id="tplStart_${i}" onchange="schTemplateHoursPreview()">${schTimeOptionsHtml(sh.start, false)}</select></td>
                            <td data-label="End"><select class="form-control" id="tplEnd_${i}" onchange="schTemplateHoursPreview()">${schTimeOptionsHtml(sh.end, true)}</select></td>
                            ${tpl.positions.map(p => `
                                <td data-label="${escapeHtml(p.label)}">
                                    <input type="number" class="form-control" id="tplCount_${i}_${p.key}" min="0" max="6" step="1" inputmode="numeric" value="${Number(sh.staffing[p.key] || 0)}">
                                </td>`).join("")}
                            <td data-label="Hours" id="tplHours_${i}"><b>${schHrs(schShiftHours(tpl, sh.key))}</b></td>
                        </tr>`).join("")}
                </tbody>
            </table>
        </div>
        <div class="form-grid" style="margin-top: 1rem;">
            <div class="form-group">
                <label for="tplCoverage">Partial availability fills a shift if it covers at least (%)</label>
                <input type="number" id="tplCoverage" class="form-control" min="10" max="100" step="5" inputmode="numeric" value="${Number(tpl.min_coverage_pct)}">
            </div>
            <div class="form-group">
                <label for="tplLeadDays">Availability due (days before the month starts)</label>
                <input type="number" id="tplLeadDays" class="form-control" min="0" max="60" step="1" inputmode="numeric" value="${Number(tpl.availability_lead_days)}">
            </div>
        </div>
        <div class="sch-save-row">
            <button type="button" class="btn btn-primary" onclick="schSaveTemplate(false)">Save Staffing Levels</button>
            <button type="button" class="btn btn-outline" onclick="schSaveTemplate(true)">Save &amp; Apply to ${escapeHtml(b.month.label)}</button>
        </div>`;
}

function schReadTemplateForm() {
    const tpl = JSON.parse(JSON.stringify(SCH.builder.template));
    tpl.positions.forEach(p => {
        p.requires_driver = document.getElementById(`tplDriver_${p.key}`).checked;
    });
    tpl.shifts.forEach((sh, i) => {
        sh.start = document.getElementById(`tplStart_${i}`).value;
        sh.end = document.getElementById(`tplEnd_${i}`).value;
        tpl.positions.forEach(p => {
            sh.staffing[p.key] = Number(document.getElementById(`tplCount_${i}_${p.key}`).value || 0);
        });
    });
    tpl.min_coverage_pct = Number(document.getElementById("tplCoverage").value || 75);
    tpl.availability_lead_days = Number(document.getElementById("tplLeadDays").value || 0);
    return tpl;
}

function schTemplateHoursPreview() {
    const tpl = schReadTemplateForm();
    tpl.shifts.forEach((sh, i) => {
        const calc = calculateDurationClient(sh.start, sh.end);
        const cell = document.getElementById(`tplHours_${i}`);
        if (cell) cell.innerHTML = `<b>${calc ? schHrs(calc.hours) : "—"}</b>`;
    });
}

async function schSaveTemplate(applyToMonth) {
    const b = SCH.builder;
    if (applyToMonth && !confirm(
        `Save these staffing levels and apply them to ${b.month.label}?\n\nMissing seats will be added and extra open seats removed (including ones added by hand). Filled seats are kept.`
    )) return;
    try {
        const res = await apiRequest("/api/schedule/settings", {
            method: "PUT",
            body: JSON.stringify({ template: schReadTemplateForm() })
        });
        SCH.settings = null;
        b.template = res.template;
        if (applyToMonth) {
            const sync = await apiRequest(`/api/schedule/${b.month.month}/sync`, { method: "POST", body: "{}" });
            showAlert(sync.message);
        } else {
            showAlert(`${res.message} New months will use them.`);
        }
        await loadBuilderView();
    } catch (err) {
        showAlert(err.message, "error");
    }
}

// =========================================================================
// Print
// =========================================================================

function schPrintSchedule(source) {
    const data = source === "builder" ? SCH.builder : SCH.view;
    if (!data || !data.slots || !data.slots.length) {
        showAlert("There's no schedule to print yet.", "error");
        return;
    }
    const month = data.month;
    const cols = schGridColumns(data.slots, data.template);
    const byDate = schSlotsByDate(data.slots);
    const rows = schMonthDates(month.month).map(iso => {
        const day = byDate.get(iso) || new Map();
        const cells = cols.map(c =>
            `<td>${(day.get(c.key) || []).map(s => {
                if (!s.dept_id) return "<i>OPEN</i>";
                const partial = s.is_partial ? ` (${escapeHtml(s.work_start)}–${escapeHtml(s.work_end)})` : "";
                return `${escapeHtml(s.employee_name || s.dept_id)}${partial}`;
            }).join("<br>")}</td>`
        ).join("");
        return `<tr><td><b>${escapeHtml(schShortDate(iso))}</b></td>${cells}</tr>`;
    }).join("");

    document.getElementById("printableSchedule").innerHTML = `
        <div class="sch-print-head">
            <div>
                <h1>RIVERVIEW FIRE DEPARTMENT</h1>
                <div class="sch-print-sub">${escapeHtml(month.label)} Staffing Schedule${month.status !== "published" ? " (DRAFT)" : ""}</div>
            </div>
            <div class="sch-print-meta">
                Printed ${escapeHtml(new Date().toLocaleString())}<br>
                By ${escapeHtml(`${currentUser.dept_id} ${currentUser.name}`)}
            </div>
        </div>
        <table>${schGridHeadHtml(cols)}<tbody>${rows}</tbody></table>`;
    document.body.classList.add("printing-schedule");
    window.print();
}

window.addEventListener("afterprint", () => document.body.classList.remove("printing-schedule"));

// =========================================================================
// Timesheet tie-in: one-tap "Scheduled" presets on the Log Shift form
// =========================================================================

async function loadScheduledPresets() {
    const bar = document.getElementById("scheduledPresetBar");
    if (!bar || !currentUser) return;
    const date = document.getElementById("entryShiftDate").value;
    const memberSelect = document.getElementById("entryDeptId");
    const deptId = schIsReviewer() && memberSelect && memberSelect.value ? memberSelect.value : currentUser.dept_id;
    const reqId = ++SCH.presetReq;
    if (!date) {
        bar.style.display = "none";
        return;
    }
    try {
        const params = new URLSearchParams({ start: date, end: date });
        if (deptId !== currentUser.dept_id) params.set("dept_id", deptId);
        const data = await apiRequest(`/api/schedule/shifts?${params.toString()}`);
        if (reqId !== SCH.presetReq) return;
        SCH.presetShifts = data.shifts || [];
        if (!SCH.presetShifts.length) {
            bar.style.display = "none";
            bar.innerHTML = "";
            return;
        }
        bar.innerHTML = `<span class="preset-label">Scheduled:</span>` + SCH.presetShifts.map((s, i) => `
            <button type="button" class="preset-chip" onclick="applyScheduledPreset(${i})" title="Fill in your scheduled ${escapeHtml(s.shift_label)} shift">
                &#128203; ${escapeHtml(s.shift_label)} · ${escapeHtml(s.position_label)} (${escapeHtml(s.work_start)}–${escapeHtml(s.work_end)})
            </button>`).join("");
        bar.style.display = "";
    } catch (_) {
        if (reqId === SCH.presetReq) bar.style.display = "none";
    }
}

function applyScheduledPreset(index) {
    const s = SCH.presetShifts[index];
    if (!s) return;
    applyShiftPreset(s.work_start, s.work_end);
    const notes = document.getElementById("entryNotes");
    if (notes && !notes.value.trim()) notes.value = `Scheduled: ${s.shift_label} - ${s.position_label}`;
}

// =========================================================================
// Wiring
// =========================================================================

["availMaxHours", "availNotes"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("input", schMarkAvailDirty);
});

(function wireAvailabilityGrid() {
    const grid = document.getElementById("availHourGrid");
    if (!grid) return;
    grid.addEventListener("pointerdown", schAvailPointerDown);
    grid.addEventListener("pointermove", schAvailPointerMove);
    grid.addEventListener("lostpointercapture", schAvailPointerUp);
    grid.addEventListener("click", schAvailGridClick);
    grid.addEventListener("dragstart", (e) => e.preventDefault());
    window.addEventListener("pointerup", schAvailPointerUp);
    window.addEventListener("pointercancel", schAvailPointerUp);
})();

window.addEventListener("hashchange", () => {
    if (!currentUser) return;
    const target = window.location.hash === "#schedule" ? "schedule" : "timesheet";
    if (target !== SCH.app) switchApp(target);
});

window.addEventListener("beforeunload", (e) => {
    if (SCH.availDirty) {
        e.preventDefault();
        e.returnValue = "";
    }
});
