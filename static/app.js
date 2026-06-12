// ----- state ---------------------------------------------------------------
const state = {
  trades: [], settings: {}, data: null, user: null,
  portfolios: [], activeId: null, isAll: false,
  freq: "weekly", ma10: true, ma40: false, editingId: null,
};
let chart = null;

// ----- helpers --------------------------------------------------------------
async function api(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json", "X-CSRF-Token": getCookie("csrftoken") } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) { showAuth(); throw new Error(j.error || "未登录"); }
  if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
  return j;
}
const $ = (id) => document.getElementById(id);
const nf = new Intl.NumberFormat("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
// Privacy mode: hide account-scaled dollar amounts (totals, P&L, market value,
// cash). Percentages, share counts, weights, and public per-share prices stay
// visible. State persists in localStorage. Toggle via the 👁 button.
let privacyOn = localStorage.getItem("privacy") === "1";
const MASK = "••••";
// keepSign: in privacy mode still show +/- so the color/direction reads right
const fmtMoney = (v) => (v == null ? "—" : privacyOn ? MASK : "$" + nf.format(v));
const signMoney = (v) => (v == null ? "—" : privacyOn ? (v >= 0 ? "+" : "-") + MASK
  : (v >= 0 ? "+$" : "-$") + nf.format(Math.abs(v)));
const signPct = (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + nf.format(v) + "%");
const fmtShares = (v) => (v == null ? "—" : privacyOn ? MASK : nf.format(v));
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function timeToStr(t) {
  if (t == null) return null;
  if (typeof t === "string") return t;
  if (typeof t === "object" && "year" in t)
    return `${t.year}-${String(t.month).padStart(2, "0")}-${String(t.day).padStart(2, "0")}`;
  if (typeof t === "number") return new Date(t * 1000).toISOString().slice(0, 10);
  return String(t);
}
function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}
let _toastTimer = null;
function showToast(msg, ok) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast " + (ok ? "ok" : "err");
  t.hidden = false;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.hidden = true; }, 3800);
}

// ----- load + render --------------------------------------------------------
async function load() {
  // Fetch state and chart independently so a slow/failed chart never blanks
  // the whole page (trades, holdings, portfolio switch still render).
  const [stRes, chRes] = await Promise.allSettled([
    api("GET", "/api/state"), api("GET", "/api/chart")]);

  if (stRes.status !== "fulfilled") throw stRes.reason;  // state is essential
  const st = stRes.value;
  state.trades = st.trades;
  state.settings = st.settings;
  state.portfolios = st.portfolios || [];
  state.groups = st.groups || [];
  state.activeId = st.active_id;
  state.isGroup = typeof st.active_id === "string" && st.active_id.startsWith("group:");
  state.isAll = st.active_id === "all";
  state.isCombined = state.isAll || state.isGroup;   // merged/read-only view
  renderPortfolioSwitch();
  syncTradeForm();
  renderTradeLog(st.trades);

  if (chRes.status === "fulfilled") {
    const ch = chRes.value;
    state.data = ch;
    syncToolbar(ch);
    renderMarketStatus(ch.market_status);   // sets state._sess (used by renderStats)
    renderStats(ch.stats && ch.stats.totals, ch);
    renderHoldings((ch.stats && ch.stats.holdings) || [], (ch.stats && ch.stats.totals) || {});
    renderClosed((ch.stats && ch.stats.closed) || []);
    renderWarnings(ch);
    renderChart(ch, state.freq);
  } else {
    // chart failed (e.g. price source slow): keep the rest usable
    $("warnBox").hidden = false;
    $("warnBox").textContent = "⚠ 走势图加载较慢或失败（股价源繁忙）。交易记录已显示；可稍后点「↻ 刷新股价」重试。";
  }
}

function renderPortfolioSwitch() {
  const sel = $("portfolioSelect");
  const single = state.portfolios.map((p) =>
    `<option value="${p.id}">${escapeHtml(p.name)}${p.num_trades ? ` · ${p.num_trades}笔` : ""}</option>`).join("");
  const groups = (state.groups || []).map((g) =>
    `<option value="group:${g.id}">⊕ ${escapeHtml(g.name)}（${g.portfolio_ids.length}个）</option>`).join("");
  sel.innerHTML =
    `<optgroup label="组合">${single}</optgroup>` +
    (groups ? `<optgroup label="自选分组">${groups}</optgroup>` : "") +
    `<optgroup label="合并"><option value="all">▦ 全部组合（合并）</option>` +
    `<option value="__newgroup__">＋ 新建自选分组…</option></optgroup>`;
  sel.value = String(state.activeId);
  // pf rename/delete only act on a single portfolio; group has its own buttons
  $("pfDelete").disabled = state.isCombined || state.portfolios.length <= 1;
  $("pfRename").disabled = state.isCombined;
  // show group manage buttons only when viewing a group
  const gEdit = $("groupEdit"), gDel = $("groupDelete");
  if (gEdit) gEdit.hidden = !state.isGroup;
  if (gDel) gDel.hidden = !state.isGroup;
}

function syncTradeForm() {
  // the trade form works in every view — the 记入组合 select picks the target.
  // 导入持仓 still writes to the active portfolio, so it stays single-view only.
  $("formHint").hidden = !state.isCombined;
  $("importBtn").hidden = state.isCombined;
  renderTradePortfolioSelect();
}

function renderTradePortfolioSelect() {
  const sel = $("f_portfolio");
  if (!sel) return;
  const prev = +sel.value || null;
  const opt = (p) => `<option value="${p.id}">${escapeHtml(p.name)}</option>`;
  if (state.isGroup) {
    // group view: members first, the rest under 其他组合
    const g = (state.groups || []).find((x) => "group:" + x.id === state.activeId);
    const ids = new Set(g ? g.portfolio_ids : []);
    const inG = state.portfolios.filter((p) => ids.has(p.id));
    const rest = state.portfolios.filter((p) => !ids.has(p.id));
    sel.innerHTML = `<optgroup label="本分组">${inG.map(opt).join("")}</optgroup>` +
      (rest.length ? `<optgroup label="其他组合">${rest.map(opt).join("")}</optgroup>` : "");
  } else {
    sel.innerHTML = state.portfolios.map(opt).join("");
  }
  // default: single view mirrors the viewed portfolio; combined views keep the
  // user's last pick when still listed, else fall back to the first option
  if (!state.isCombined) sel.value = String(state.activeId);
  else if (prev && state.portfolios.some((p) => p.id === prev)) sel.value = String(prev);
  if (!sel.value && sel.options.length) sel.selectedIndex = 0;
}

function syncToolbar(ch) {
  $("capitalInput").value = state.settings.starting_capital || "";
  const mode = ch.mode || state.settings.display_mode || "pnl";
  document.querySelectorAll("#modeSeg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === mode));
  document.querySelectorAll("#freqSeg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.freq === state.freq));
  $("ma10Chk").checked = state.ma10;
  $("ma40Chk").checked = state.ma40;
  $("ma40Chk").parentElement.style.display = state.freq === "weekly" ? "" : "none";
  $("capitalInput").disabled = state.isCombined;   // sum is read-only in combined view
  $("capitalSave").disabled = state.isCombined;
  $("clearBtn").hidden = state.isCombined || state.trades.length === 0;
  $("exportBtn").hidden = state.trades.length === 0;   // export works in any view
}

function syncPrivacyBtn() {
  const b = $("privacyBtn");
  if (!b) return;
  b.textContent = privacyOn ? "🙈 隐私中" : "👁 隐私";
  b.classList.toggle("active", privacyOn);
}

function renderMarketStatus(ms) {
  const el = $("marketStatus");
  state._sess = ms ? ms.session : null;   // used by renderStats for the 盘前/盘后 label
  if (!ms || !ms.session) { el.hidden = true; return; }
  const map = {
    regular: { txt: "盘中", cls: "ms-open", dot: "🟢" },
    pre: { txt: "盘前", cls: "ms-ext", dot: "🟡" },
    post: { txt: "盘后", cls: "ms-ext", dot: "🟡" },
  };
  const m = map[ms.session] || { txt: "已收盘", cls: "ms-closed", dot: "⚪" };
  // if market is open but session is regular, show 盘中; closed regular -> 已收盘
  const label = (ms.session === "regular" && ms.market !== "open") ? "已收盘" : m.txt;
  el.className = "market-status " + m.cls;
  el.textContent = `${m.dot} ${label}`;
  el.title = ms.as_of ? "行情时间：" + ms.as_of : "";
  el.hidden = false;
}

function renderStats(t, ch) {
  const bar = $("statsBar");
  if (!t || t.total_value == null) { bar.innerHTML = ""; return; }
  const cls = (v) => (v >= 0 ? "pos" : "neg");
  // include the after-hours segment in the account totals (single value),
  // matching the holdings 当日/市值 columns. 现价-like detail stays per-holding.
  const dayTot = (t.ext_pnl != null && t.day_pnl != null) ? t.day_pnl + t.ext_pnl : t.day_pnl;
  const mvTot = (t.market_value_ext != null) ? t.market_value_ext : t.market_value;
  const tvTot = (t.total_value_ext != null) ? t.total_value_ext : t.total_value;
  const unrTot = (t.market_value_ext != null) ? t.unrealized_pnl + (t.market_value_ext - t.market_value) : t.unrealized_pnl;
  // 累计盈亏 must include the after-hours move too, like the other account
  // totals — otherwise during pre/post it won't equal 已实现 + 未实现 (which do
  // include it). Cumulative P&L = realized + unrealized, so adding the holdings'
  // extended-session move (ext_pnl) keeps the identity intact.
  const totTot = (t.ext_pnl != null) ? t.total_pnl + t.ext_pnl : t.total_pnl;
  // percentages for the P&L cards
  const dayPctTot = (dayTot != null && (tvTot - dayTot)) ? (dayTot / (tvTot - dayTot) * 100) : t.day_pnl_pct;
  const unrPct = (unrTot != null && (mvTot - unrTot)) ? (unrTot / (mvTot - unrTot) * 100) : null;
  const totPct = (t.ext_pnl != null && t.invested) ? (totTot / t.invested * 100) : t.return_pct;
  // P&L stat: big % main + small $ sub. amountOnly stats just show $.
  const subAmt = (v) => `<div class="ext-sub ${cls(v)}">${signMoney(v)}</div>`;
  // item tuple: [label, mainValue, colorClass, kind, subHtml]
  const items = [
    ["账户总值", fmtMoney(tvTot), "", "", ""],
    ["当日盈亏", dayTot == null ? "—" : signPct(dayPctTot), dayTot == null ? "" : cls(dayTot), "",
      dayTot == null ? "" : subAmt(dayTot)],
    ["累计盈亏", totPct == null ? signMoney(totTot) : signPct(totPct), cls(totTot), "",
      subAmt(totTot)],
    ["已实现盈亏", signMoney(t.realized_pnl), cls(t.realized_pnl), "", ""],
    ["未实现盈亏", unrPct == null ? signMoney(unrTot) : signPct(unrPct), cls(unrTot), "",
      subAmt(unrTot)],
    ["持仓市值", fmtMoney(mvTot), "", "", ""],
    ["现金", fmtMoney(t.cash), "", "cash", ""],
    ["持仓数", t.num_positions, "", "", ""],
    // drawdown cards — only shown when data is available
    ...(t.ath != null ? [[
      "历史最高",
      t.ath_dd_pct === 0 ? "🏔 历史新高" : signPct(t.ath_dd_pct),
      t.ath_dd_pct === 0 ? "pos" : (t.ath_dd_pct < 0 ? "neg" : ""),
      "",
      `<div class="ext-sub muted">${fmtMoney(t.ath)}</div>`,
    ]] : []),
    ...(t.week_high != null ? [[
      "近5日高",
      t.week_dd_pct === 0 ? "—" : signPct(t.week_dd_pct),
      t.week_dd_pct === 0 ? "" : (t.week_dd_pct < 0 ? "neg" : "pos"),
      "",
      `<div class="ext-sub muted">${fmtMoney(t.week_high)}</div>`,
    ]] : []),
  ];
  // remember the current cash so the editor can prefill it
  state.currentCash = t.cash;
  bar.innerHTML = items.map(([k, v, c, kind, sub]) => {
    if (kind === "cash" && !state.isCombined) {
      return `<div class="stat stat-editable" id="cashStat" title="点击修改现金">` +
        `<div class="k">${k} <span class="edit-pencil">✎</span></div><div class="v ${c}">${v}</div></div>`;
    }
    return `<div class="stat"><div class="k">${k}</div><div class="v ${c}">${v}</div>${sub || ""}</div>`;
  }).join("");

  const cashStat = $("cashStat");
  if (cashStat) cashStat.addEventListener("click", editCash);
}

async function editCash() {
  const cur = state.currentCash;
  const input = prompt("设置当前现金金额（$）：\n会自动调整起始本金，使现金等于该值；持仓和盈亏不受影响。",
    cur == null ? "" : String(cur));
  if (input === null) return;
  const val = parseFloat(String(input).replace(/[$,\s]/g, ""));
  if (isNaN(val) || val < 0) { showToast("请输入有效的非负数字。", false); return; }
  try {
    await api("POST", "/api/set-cash", { cash: val });
    await load();
    showToast("现金已更新为 " + fmtMoney(val) + " ✓", true);
  } catch (ex) { showToast(ex.message, false); }
}

// ----- custom portfolio groups --------------------------------------------
let editingGroupId = null;
function openGroupModal(group) {
  editingGroupId = group ? group.id : null;
  $("groupModalTitle").textContent = group ? "编辑自选分组" : "新建自选分组";
  $("groupName").value = group ? group.name : "";
  $("groupMsg").hidden = true;
  const chosen = new Set(group ? group.portfolio_ids : []);
  $("groupPicker").innerHTML = state.portfolios.map((p) =>
    `<label class="grp-opt"><input type="checkbox" value="${p.id}"${chosen.has(p.id) ? " checked" : ""} /> ${escapeHtml(p.name)}</label>`
  ).join("") || `<p class="muted">还没有组合。</p>`;
  $("groupModal").hidden = false;
}
async function saveGroup() {
  const name = $("groupName").value.trim();
  const ids = [...$("groupPicker").querySelectorAll("input:checked")].map((i) => +i.value);
  const msg = $("groupMsg");
  if (!name) { msg.textContent = "请填写分组名称。"; msg.className = "msg err"; msg.hidden = false; return; }
  if (ids.length < 1) { msg.textContent = "请至少选择一个组合。"; msg.className = "msg err"; msg.hidden = false; return; }
  try {
    if (editingGroupId) await api("PUT", `/api/groups/${editingGroupId}`, { name, portfolio_ids: ids });
    else await api("POST", "/api/groups", { name, portfolio_ids: ids });
    $("groupModal").hidden = true;
    resetForm();
    await load();
    showToast(editingGroupId ? "分组已更新 ✓" : "分组已创建 ✓", true);
  } catch (ex) { msg.textContent = ex.message; msg.className = "msg err"; msg.hidden = false; }
}

// Color the per-position 8% risk: a single position losing >2% of the whole
// account at an 8% stop is a meaningful concentration warning (O'Neil-ish).
function riskClass(r) {
  if (r == null) return "";
  if (r >= 2) return "neg";          // red: heavy single-name risk
  if (r >= 1.2) return "warn-amber"; // amber: getting concentrated
  return "muted";
}

function renderHoldings(holdings, totals) {
  const box = $("holdings");
  totals = totals || {};
  state._holdings = holdings;        // stash for re-render on header-click sort
  state._holdTotals = totals;
  if (!holdings.length) { box.innerHTML = `<p class="empty-mini">暂无持仓。</p>`; return; }

  // per-row derived values (after-hours-inclusive where applicable)
  const rowVals = (h) => {
    const dayVal = (h.ext_chg != null && h.day_chg != null) ? h.day_chg + h.ext_chg : h.day_chg;
    const mvVal = (h.ext_mv != null) ? h.ext_mv : h.market_value;
    const unVal = (h.ext_unreal != null) ? h.ext_unreal : h.unrealized;
    const unPct = (h.ext_unreal != null && h.avg_cost) ? (unVal / (h.avg_cost * h.shares) * 100) : h.unrealized_pct;
    // day % vs the day's starting value (current value − today's change)
    const dayBase = (dayVal != null && mvVal != null) ? (mvVal - dayVal) : null;
    const dayPct = (dayVal != null && dayBase) ? (dayVal / dayBase * 100) : h.day_chg_pct;
    return { dayVal, mvVal, unVal, unPct, dayPct };
  };
  // P&L cell: big % on top, small $ underneath
  const pnlCell = (val, pct, colorClass) =>
    val == null ? "—"
      : `<div class="pnl-pct ${colorClass}">${signPct(pct)}</div>` +
        `<div class="pnl-amt muted">${signMoney(val)}</div>`;
  // columns: key + how to extract the sortable value + numeric?
  const cols = [
    { key: "ticker", label: "代码", num: false, val: (h) => h.ticker },
    { key: "shares", label: "股数", num: true, val: (h) => h.shares },
    { key: "avg_cost", label: "均价", num: true, val: (h) => h.avg_cost },
    { key: "last_price", label: "现价", num: true, val: (h) => h.last_price },
    { key: "day", label: "当日", num: true, val: (h) => rowVals(h).dayVal, title: "当日涨跌（含盘后）" },
    { key: "mv", label: "市值", num: true, val: (h) => rowVals(h).mvVal },
    { key: "unreal", label: "浮动盈亏", num: true, val: (h) => rowVals(h).unVal },
    { key: "weight", label: "仓位占比", num: true, val: (h) => h.weight, title: "该股市值占账户总值的比例" },
    { key: "risk", label: `风险敞口<span class="th-sub">(-8%)</span>`, num: true, val: (h) => h.risk_8pct, title: "若该股跌 8%（欧奈尔止损线），账户将损失的百分比" },
  ];

  // apply current sort (default: market value desc, matching backend)
  const s = state.holdSort || (state.holdSort = { key: "mv", dir: -1 });
  const col = cols.find((c) => c.key === s.key) || cols[5];
  const sorted = [...holdings].sort((a, b) => {
    let va = col.val(a), vb = col.val(b);
    if (col.num) { va = (va == null ? -Infinity : va); vb = (vb == null ? -Infinity : vb); return (va - vb) * s.dir; }
    return String(va).localeCompare(String(vb)) * s.dir;
  });

  const arrow = (k) => s.key === k ? (s.dir === 1 ? " ▲" : " ▼") : "";
  const thead = cols.map((c) =>
    `<th class="sortable${s.key === c.key ? " sorted" : ""}" data-sort="${c.key}"${c.title ? ` title="${c.title}"` : ""}>${c.label}<span class="sort-arrow">${arrow(c.key)}</span></th>`
  ).join("");

  box.innerHTML = `<table><thead><tr>${thead}</tr></thead><tbody>` +
    sorted.map((h) => {
      const eCls = (v) => (v || 0) >= 0 ? "pos" : "neg";
      const sess = h.session === "pre" ? "盘前" : "盘后";
      const extPx = (h.ext_price != null) ? `<div class="ext-px ${eCls(h.ext_chg_pct)}">${sess} $${nf.format(h.ext_price)} ${signPct(h.ext_chg_pct)}</div>` : "";
      const { dayVal, mvVal, unVal, unPct, dayPct } = rowVals(h);
      const dc = (dayVal || 0) >= 0 ? "pos" : "neg";
      const c = (unVal || 0) >= 0 ? "pos" : "neg";
      // in combined view, show which portfolio(s) hold this ticker under the code
      const pfLine = (state.isCombined && h.portfolios && h.portfolios.length)
        ? `<div class="pf-tag" title="所属组合">${h.portfolios.map(escapeHtml).join("、")}</div>` : "";
      return `<tr>
        <td data-label="代码"><button type="button" class="ticker-btn" onclick="openStock('${h.ticker}')" title="查看 ${h.ticker} K 线图">${h.ticker}</button>${pfLine}</td>
        <td data-label="股数">${fmtShares(h.shares)}</td>
        <td data-label="均价">${h.avg_cost == null ? "—" : "$" + nf.format(h.avg_cost)}</td>
        <td data-label="现价">${h.last_price == null ? "—" : "$" + nf.format(h.last_price)}${extPx}</td>
        <td data-label="当日">${pnlCell(dayVal, dayPct, dc)}</td>
        <td data-label="市值">${fmtMoney(mvVal)}</td>
        <td data-label="浮动盈亏">${pnlCell(unVal, unPct, c)}</td>
        <td data-label="仓位占比">${h.weight == null ? "—" : nf.format(h.weight) + "%"}<div class="weight-bar"><span style="width:${Math.min(h.weight || 0, 100)}%"></span></div></td>
        <td data-label="风险敞口" class="${riskClass(h.risk_8pct)}">${h.risk_8pct == null ? "—" : "-" + nf.format(h.risk_8pct) + "%"}</td>
      </tr>`;
    }).join("") + `</tbody></table>` +
    (totals.invested_pct == null ? "" :
      `<div class="holdings-foot">
         <span>持仓占用 <b>${nf.format(totals.invested_pct)}%</b>（现金 ${nf.format(Math.max(100 - (totals.invested_pct || 0), 0))}%）</span>
         <span title="所有持仓各跌 8% 时，账户合计损失">组合总风险 <b class="${riskClass(totals.total_risk_8pct >= 6 ? 2 : totals.total_risk_8pct >= 4 ? 1.2 : 0)}">-${nf.format(totals.total_risk_8pct || 0)}%</b></span>
       </div>`);

  // wire header clicks: same column toggles dir, new column sorts (text asc, numbers desc first)
  box.querySelectorAll("th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      const cdef = cols.find((c) => c.key === k);
      if (state.holdSort.key === k) state.holdSort.dir *= -1;
      else state.holdSort = { key: k, dir: cdef.num ? -1 : 1 };
      renderHoldings(state._holdings, state._holdTotals);
    });
  });
}

function renderClosed(closed) {
  const card = $("closedCard");
  if (!closed.length) { card.hidden = true; return; }
  card.hidden = false;
  state._closed = closed;   // remembered so the 复盘 modal can read/refresh a note
  const box = $("closed");
  // summary: total realized across all sold tickers
  const total = closed.reduce((s, x) => s + (x.realized || 0), 0);
  const wins = closed.filter((x) => (x.realized || 0) > 0).length;
  $("closedSummary").innerHTML =
    `合计已实现 <b class="${total >= 0 ? "pos" : "neg"}">${signMoney(total)}</b> · ` +
    `${closed.length} 只卖出（盈 ${wins} / 亏 ${closed.length - wins}）`;
  const px = (v) => (v == null ? "—" : "$" + nf.format(v));
  const noteCell = (x) => {
    const has = x.note && x.note.trim();
    if (has) {
      const safe = escapeHtml(x.note);
      return `<button type="button" class="review-btn has" title="${safe}" onclick="openReviewModal('${x.ticker}')">📝 <span class="review-snip">${safe}</span></button>`;
    }
    return `<button type="button" class="review-btn" onclick="openReviewModal('${x.ticker}')">＋ 复盘</button>`;
  };
  box.innerHTML = `<table><thead><tr>
      <th>代码</th><th title="卖出的股数">股数</th><th title="所卖股票的平均买入成本">买入价</th><th title="平均卖出价">卖出价</th><th>回报率</th><th>已实现盈亏</th><th>最近卖出</th><th>复盘</th>
    </tr></thead><tbody>` +
    closed.map((x) => {
      const c = (x.realized || 0) >= 0 ? "pos" : "neg";
      const hold = x.still_holding ? ` <span class="hold-tag" title="仍持有部分仓位">持有中</span>` : "";
      const lots = x.lots || [];
      const expandable = lots.length > 1;   // only worth expanding when multiple sells
      const toggle = expandable
        ? `<button type="button" class="lot-toggle" aria-expanded="false" title="展开每一笔卖出" onclick="toggleLots('${x.ticker}', this)">▸</button>`
        : `<span class="lot-toggle-spacer"></span>`;
      const main = `<tr>
        <td data-label="代码">${toggle}<button type="button" class="ticker-btn" onclick="openStock('${x.ticker}')" title="查看 ${x.ticker} K 线图">${x.ticker}</button>${hold}</td>
        <td data-label="股数">${fmtShares(x.sold_shares)}</td>
        <td data-label="买入价">${px(x.avg_buy)}</td>
        <td data-label="卖出价">${px(x.avg_sell)}</td>
        <td data-label="回报率" class="${c} pnl-pct">${x.return_pct == null ? "—" : signPct(x.return_pct)}</td>
        <td data-label="已实现盈亏" class="${c}">${signMoney(x.realized)}</td>
        <td data-label="最近卖出" class="muted">${x.last_sell}</td>
        <td data-label="复盘">${noteCell(x)}</td>
      </tr>`;
      const lotRows = expandable ? lots.map((l, i) => {
        const lc = (l.realized || 0) >= 0 ? "pos" : "neg";
        return `<tr class="lot-row" hidden data-lot="${escapeHtml(x.ticker)}">
          <td data-label="第几笔" class="lot-label">└ 第 ${i + 1} 笔</td>
          <td data-label="股数">${fmtShares(l.shares)}</td>
          <td data-label="买入价">${px(l.buy)}</td>
          <td data-label="卖出价">${px(l.sell)}</td>
          <td data-label="回报率" class="${lc}">${l.return_pct == null ? "—" : signPct(l.return_pct)}</td>
          <td data-label="已实现盈亏" class="${lc}">${signMoney(l.realized)}</td>
          <td data-label="卖出日期" class="muted">${l.date}</td>
          <td></td>
        </tr>`;
      }).join("") : "";
      return main + lotRows;
    }).join("") + `</tbody></table>`;
}

// expand/collapse the per-sell breakdown for one ticker in 已平仓盈亏
function toggleLots(ticker, btn) {
  const show = btn.getAttribute("aria-expanded") !== "true";
  document.querySelectorAll(`#closed tr[data-lot="${CSS.escape(ticker)}"]`)
    .forEach((r) => { r.hidden = !show; });
  btn.setAttribute("aria-expanded", show ? "true" : "false");
  btn.textContent = show ? "▾" : "▸";
}

let reviewTicker = null;
function openReviewModal(ticker) {
  reviewTicker = ticker;
  const row = (state._closed || []).find((c) => c.ticker === ticker);
  $("reviewTicker").textContent = ticker;
  $("reviewNote").value = (row && row.note) || "";
  $("reviewMsg").hidden = true;
  $("reviewModal").hidden = false;
  setTimeout(() => $("reviewNote").focus(), 30);
}
async function saveReview() {
  const note = $("reviewNote").value.trim();
  const msg = $("reviewMsg");
  try {
    await api("POST", "/api/review-note", { ticker: reviewTicker, note });
    const row = (state._closed || []).find((c) => c.ticker === reviewTicker);
    if (row) row.note = note;                 // reflect immediately without a full reload
    $("reviewModal").hidden = true;
    renderClosed(state._closed || []);
    showToast(note ? "复盘已保存 ✓" : "复盘已清空", true);
  } catch (ex) { msg.textContent = ex.message; msg.className = "msg err"; msg.hidden = false; }
}

// ----- single-stock chart (weekly / daily) --------------------------------
let stockChart = null;
let stockTicker = null;
let stockFreq = "weekly";
window.openStock = function (ticker) {
  stockTicker = ticker;
  stockFreq = "weekly";
  syncStockFreqSeg();
  $("stockTitle").textContent = ticker;
  $("stockModal").hidden = false;
  loadStock();
};
function syncStockFreqSeg() {
  document.querySelectorAll("#stockFreqSeg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.sfreq === stockFreq));
}
async function loadStock() {
  $("stockMeta").textContent = "加载中…";
  $("stockEmpty").hidden = true;
  $("stockBox").hidden = true;
  try {
    const d = await api("GET", "/api/stock/" + encodeURIComponent(stockTicker) + "?freq=" + stockFreq);
    if (!d.has_data) {
      $("stockMeta").textContent = "";
      $("stockLegend").innerHTML = "";
      $("stockEmpty").hidden = false;
      renderStockChart(null);
      return;
    }
    $("stockMeta").textContent = `$${nf.format(d.last)} · 截至 ${d.asof}`;
    renderStockLegend(d);
    renderStockChart(d);
  } catch (ex) {
    $("stockMeta").textContent = "";
    $("stockEmpty").textContent = "加载失败：" + ex.message;
    $("stockEmpty").hidden = false;
  }
}

function renderStockLegend(d) {
  const lg = $("stockLegend");
  const label = stockFreq === "weekly" ? "周K线" : "日K线";
  // O'Neil upper channel line status (weekly only): broken = offensive sell
  let ch = "";
  if (stockFreq === "weekly") {
    const c = d.channel;
    if (!c) {
      ch = `<span class="lg muted">上轨道线：未形成（近 5 个月内无可连的高点结构）</span>`;
    } else if (c.confirmed) {
      // break stays flagged for 8 weeks (SCRB-style: spike above the line then
      // pullback IS the climax event, not just a current-week condition)
      const ago = c.broken_weeks_ago;
      ch = `<span class="lg"><span class="dot chan-dot"></span>上轨道线 $${nf.format(c.line_last)} · ${c.num_touches} 个高点 / ${c.span_weeks} 周</span>` +
        (!c.broken_high ? `<span class="lg muted">距上轨道线 +${nf.format(c.dist_pct)}%</span>`
          : ago > 0 ? `<span class="lg neg"><b>⚠ ${ago} 周前（${c.broken_time}）突破上轨道线 — climax 卖出信号已触发</b></span>`
          : c.broken_close ? `<span class="lg neg"><b>⚠ 本周突破上轨道线 — 进攻性卖出信号</b></span>`
          : `<span class="lg warn-amber">⚠ 本周盘中穿越上轨道线</span>`);
    } else {
      // 2-point tentative line — drawn early so the ceiling is visible before
      // the 3rd approach; course warns 2-point lines are unreliable
      const ago = c.broken_weeks_ago;
      ch = `<span class="lg"><span class="dot chan-dot tent"></span>上轨道线（预备 · 待第 3 次触线确认）$${nf.format(c.line_last)} · ${c.span_weeks} 周</span>` +
        (!c.broken_high ? `<span class="lg muted">距预备线 +${nf.format(c.dist_pct)}%</span>`
          : ago > 0 ? `<span class="lg warn-amber"><b>⚠ ${ago} 周前穿越预备线（2 点线信号弱）</b></span>`
          : c.broken_close ? `<span class="lg warn-amber"><b>⚠ 收盘越过预备线（2 点线信号弱）</b></span>`
          : `<span class="lg warn-amber">⚠ 本周盘中穿越预备线</span>`);
    }
  }
  lg.innerHTML = `<span class="lg lg-candle">${label}</span>` +
    (d.mas || []).map((m) =>
      `<span class="lg"><span class="dot" style="background:${m.color}"></span>${m.label}</span>`).join("") + ch;
}

function renderStockChart(d) {
  const host = $("stockChart");
  if (stockChart) { stockChart.remove(); stockChart = null; }
  if (!d || !d.candles.length) return;
  // MarketSurge-style chart: white bg, dotted grid, LOG price scale,
  // thin OHLC bars colored vs PRIOR close (blue up / magenta down)
  stockChart = LightweightCharts.createChart(host, {
    autoSize: true,
    layout: { background: { color: "#ffffff" }, textColor: "#333", fontSize: 12 },
    grid: { vertLines: { color: "#e3e3e3", style: LightweightCharts.LineStyle.Dotted },
            horzLines: { color: "#e3e3e3", style: LightweightCharts.LineStyle.Dotted } },
    rightPriceScale: { borderColor: "#c4c4c4", mode: LightweightCharts.PriceScaleMode.Logarithmic },
    timeScale: { borderColor: "#c4c4c4", rightOffset: 4 },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  stockChart.priceScale("right").applyOptions({ scaleMargins: { top: 0.08, bottom: 0.26 } });
  const MS_UP = "#1565c0", MS_DN = "#d81b60";
  const candle = stockChart.addBarSeries({ thinBars: true, openVisible: false });
  let _pc = null;
  candle.setData(d.candles.map((b) => {
    const color = (_pc == null || b.close >= _pc) ? MS_UP : MS_DN;
    _pc = b.close;
    return { ...b, color };
  }));
  (d.mas || []).forEach((m) => {
    if (m.points && m.points.length) {
      const s = stockChart.addLineSeries({ color: m.color, lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false });
      s.setData(m.points);
    }
  });
  // O'Neil upper channel line: dashed red through the fitted swing highs,
  // extended to the latest bar (weekly only — backend sends channel:null on
  // daily). Tentative 2-point lines render dotted + faded.
  if (d.channel && d.channel.points && d.channel.points.length === 2) {
    const conf = d.channel.confirmed;
    const s = stockChart.addLineSeries({
      color: conf ? "#d32f2f" : "rgba(211,47,47,.5)", lineWidth: 2,
      lineStyle: conf ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Dotted,
      priceLineVisible: false, crosshairMarkerVisible: false, lastValueVisible: false,
    });
    s.setData(d.channel.points);
  }
  if (d.volume && d.volume.length) {
    const v = stockChart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol" });
    v.setData(d.volume);
    stockChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  }
  if (d.markers && d.markers.length) candle.setMarkers(d.markers);
  setupStockBox(host, d);
  stockChart.timeScale().fitContent();
}

function fmtVol(v) {
  if (v == null) return "—";
  if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(Math.round(v));
}
function setupStockBox(host, d) {
  const box = $("stockBox");
  const bars = {}; (d.bars || []).forEach((b) => { bars[b.time] = b; });
  const times = (d.bars || []).map((b) => b.time);
  const maSpecs = d.mas || [];
  const fmt = (v) => (v == null ? "—" : "$" + nf.format(v));
  const signPctV = (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + nf.format(v) + "%");
  const cls = (v) => (v == null ? "" : v >= 0 ? "pos" : "neg");
  const row = (k, v, kc, vc) =>
    `<div class="db-row"><span class="db-k" ${kc ? `style="color:${kc}"` : ""}>${k}</span>` +
    `<span class="db-v ${vc || ""}">${v}</span></div>`;
  const distPct = (price, ma) => (ma && price != null) ? ((price - ma) / ma * 100) : null;
  const render = (time) => {
    const b = bars[time]; if (!b) { box.hidden = true; return; }
    let html = row("Date", time) + row("Open", fmt(b.open)) + row("High", fmt(b.high)) +
      row("Low", fmt(b.low)) + row("Close", fmt(b.close)) +
      row("% Chg", signPctV(b.chg_pct), null, cls(b.chg_pct)) +
      row("Cls Range", b.cls_range == null ? "—" : nf.format(b.cls_range) + "%") +
      row("Vol", fmtVol(b.volume)) +
      row("Vol %", signPctV(b.vol_pct), null, cls(b.vol_pct)) +
      `<div class="db-sep"></div>`;
    // each MA: value + distance-from-price, MarketSurge style "105.04 +1.7%"
    (b.ma ? maSpecs : []).forEach((m) => {
      const val = b.ma[m.key];
      const dist = distPct(b.close, val);
      html += row(m.label, val == null ? "—" :
        `${fmt(val)} <span class="${cls(dist)}">${signPctV(dist)}</span>`, m.color);
    });
    box.innerHTML = html;
    box.hidden = false;
  };
  const latest = times[times.length - 1];
  if (latest) render(latest);
  stockChart.subscribeCrosshairMove((param) => {
    const key = timeToStr(param.time);
    if (key && bars[key]) render(key); else if (latest) render(latest);
  });
}

function renderTradeLog(trades) {
  const box = $("tradeLog");
  if (!trades.length) { box.innerHTML = `<p class="empty-mini">还没有交易记录。</p>`; return; }
  const rows = [...trades].reverse();
  const isAll = state.isCombined;
  box.innerHTML = `<table><thead><tr>
      <th>日期</th>${isAll ? "<th>组合</th>" : ""}<th>代码</th><th>方向</th><th>股数</th><th>价格</th><th>费用</th>
      <th class="reasoncell">原因</th>${isAll ? "" : "<th></th>"}
    </tr></thead><tbody>` +
    rows.map((t) => `<tr>
      <td data-label="日期">${t.date}</td>
      ${isAll ? `<td data-label="组合">${escapeHtml(t.portfolio_name || "")}</td>` : ""}
      <td data-label="代码" class="tickercell">${t.ticker}</td>
      <td data-label="方向" style="text-align:center"><span class="pill ${t.side}">${t.side === "buy" ? "买入" : "卖出"}</span></td>
      <td data-label="股数">${fmtShares(t.shares)}</td>
      <td data-label="价格">$${nf.format(t.price)}</td>
      <td data-label="费用">${t.fees ? (privacyOn ? MASK : "$" + nf.format(t.fees)) : "—"}</td>
      <td data-label="原因" class="reasoncell">${escapeHtml(t.reason || "")}</td>
      ${isAll ? "" : `<td data-label="操作" class="actioncell" style="white-space:nowrap">
        <button class="rowbtn" onclick="editTrade(${t.id})">✎</button>
        <button class="rowbtn del" onclick="deleteTrade(${t.id})">🗑</button>
      </td>`}
    </tr>`).join("") + `</tbody></table>`;
}

function csvCell(v) {
  // quote if it contains comma/quote/newline; double internal quotes
  const s = v == null ? "" : String(v);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
function exportTradesCsv() {
  const trades = state.trades || [];
  if (!trades.length) { showToast("没有交易记录可导出。", false); return; }
  const isAll = state.isCombined;
  const header = ["Date", "Ticker", "Side", "Shares", "Price", "Fees", "Reason"];
  if (isAll) header.splice(1, 0, "Portfolio");
  const lines = [header.join(",")];
  // chronological (oldest first) for a clean ledger
  [...trades].forEach((t) => {
    const row = [t.date,
      ...(isAll ? [t.portfolio_name || ""] : []),
      t.ticker, t.side, t.shares, t.price, t.fees || 0, t.reason || ""];
    lines.push(row.map(csvCell).join(","));
  });
  // BOM so Excel opens UTF-8 (Chinese reasons) correctly
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const name = (state.data && state.data.portfolio && state.data.portfolio.name) || "portfolio";
  const today = new Date().toISOString().slice(0, 10);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `trades_${name}_${today}.csv`.replace(/[\/\\:*?"<>|]+/g, "-");
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  showToast(`已导出 ${trades.length} 笔交易 ✓`, true);
}

function renderWarnings(ch) {
  const box = $("warnBox");
  const msgs = [...(ch.warnings || [])];
  const errs = ch.errors || {};
  Object.keys(errs).forEach((k) => msgs.push(`${k}: ${errs[k]}`));
  if (!msgs.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = "⚠ " + msgs.map(escapeHtml).join(" · ");
}

// ----- chart ----------------------------------------------------------------
function renderChart(ch, freq) {
  const host = $("chart");
  if (chart) { chart.remove(); chart = null; }
  $("chartEmpty").hidden = !!ch.has_data;
  if (!ch.has_data) { $("dataBox").hidden = true; return; }

  chart = LightweightCharts.createChart(host, {
    autoSize: true,
    layout: { background: { color: "#0b0e11" }, textColor: "#d1d4dc", fontSize: 12 },
    grid: { vertLines: { color: "#1c2127" }, horzLines: { color: "#1c2127" } },
    rightPriceScale: { borderColor: "#2a2f36", scaleMargins: { top: 0.08, bottom: 0.25 } },
    timeScale: { borderColor: "#2a2f36", rightOffset: 4 },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });

  const weekly = freq === "weekly";
  const d = weekly ? ch.weekly : ch.daily;
  let main;
  if (weekly) {
    main = chart.addCandlestickSeries({
      upColor: "#26a69a", downColor: "#ef5350", borderUpColor: "#26a69a",
      borderDownColor: "#ef5350", wickUpColor: "#26a69a", wickDownColor: "#ef5350",
    });
    main.setData(d.candles);
  } else {
    main = chart.addAreaSeries({
      lineColor: "#f0b90b", topColor: "rgba(240,185,11,.22)",
      bottomColor: "rgba(240,185,11,0)", lineWidth: 2,
    });
    main.setData(d.line);
  }

  if (state.ma10) {
    const maData = weekly ? d.ma10 : d.ma50;
    if (maData && maData.length) {
      const s = chart.addLineSeries({ color: "#2962ff", lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false });
      s.setData(maData);
    }
  }
  if (weekly && state.ma40 && d.ma40 && d.ma40.length) {
    const s = chart.addLineSeries({ color: "#ff9800", lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false });
    s.setData(d.ma40);
  }

  const vol = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol" });
  vol.setData(d.volume);
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });

  // No on-chart buy/sell arrows on the portfolio chart — with many holdings
  // they pile up and clutter the view. The crosshair tooltip shows what was
  // bought/sold that period instead (see setupTooltip).
  setupTooltip(host, d.markers);
  setupDataBox(d, weekly);
  chart.timeScale().fitContent();
}

// MarketSurge-style hover report (Date/OHLC/Last/%Chg/SMA10/SMA40).
function setupDataBox(d, weekly) {
  const box = $("dataBox");
  // index every series by time for O(1) lookup
  const bars = {};   // time -> {o,h,l,c}
  if (weekly) {
    d.candles.forEach((b) => { bars[b.time] = { o: b.open, h: b.high, l: b.low, c: b.close }; });
  } else {
    d.line.forEach((p) => { bars[p.time] = { o: null, h: null, l: null, c: p.value }; });
  }
  const ma10 = {}, ma40 = {};
  (weekly ? d.ma10 : d.ma50).forEach((p) => { ma10[p.time] = p.value; });
  if (weekly) d.ma40.forEach((p) => { ma40[p.time] = p.value; });

  const times = (weekly ? d.candles : d.line).map((b) => b.time);
  const prevClose = {};   // time -> previous bar's close, for %Chg
  let pc = null;
  times.forEach((t) => { prevClose[t] = pc; pc = bars[t].c; });

  const unit = (state.data && state.data.unit) || "$";
  const fmt = (v) => (v == null ? "—" : (unit === "idx" ? "" : "$") + nf.format(v));
  const ma10Label = weekly ? "SMA(10)" : "MA(50d)";

  const render = (time) => {
    const b = bars[time];
    if (!b) { box.hidden = true; return; }
    const last = b.c, pcv = prevClose[time];
    const chg = (pcv != null) ? last - pcv : null;
    const chgPct = (pcv != null && pcv !== 0) ? (chg / pcv * 100) : null;
    const cls = (v) => (v == null ? "" : v >= 0 ? "pos" : "neg");
    const signed = (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + nf.format(v));
    const row = (k, v, kc, vc) =>
      `<div class="db-row"><span class="db-k" ${kc ? `style="color:${kc}"` : ""}>${k}</span>` +
      `<span class="db-v ${vc || ""}">${v}</span></div>`;
    const m10 = ma10[time], m40 = ma40[time];
    let html = row("Date", time);
    if (weekly) {
      html += row("Open", fmt(b.o)) + row("High", fmt(b.h)) + row("Low", fmt(b.l));
    }
    html += row("Last", fmt(last));
    html += row("Chg", signed(chg), null, cls(chg));
    html += row("% Chg", chgPct == null ? "—" : signed(chgPct) + "%", null, cls(chgPct));
    html += `<div class="db-sep"></div>`;
    html += row(ma10Label, fmt(m10), "#2962ff");
    if (weekly) html += row("SMA(40)", fmt(m40), "#ff9800");
    box.innerHTML = html;
    box.hidden = false;
  };

  // default: latest bar
  const latest = times[times.length - 1];
  if (latest) render(latest);

  chart.subscribeCrosshairMove((param) => {
    const key = timeToStr(param.time);
    if (key && bars[key]) render(key);
    else if (latest) render(latest);   // off-chart: fall back to latest
  });
}

function setupTooltip(host, markers) {
  const tip = $("tooltip");
  const byTime = {};
  markers.forEach((m) => { (byTime[m.time] = byTime[m.time] || []).push(m); });
  chart.subscribeCrosshairMove((param) => {
    const key = timeToStr(param.time);
    if (!key || !byTime[key] || !param.point) { tip.hidden = true; return; }
    const list = byTime[key];
    tip.innerHTML = `<div><b>${key}</b></div>` + list.map((m) =>
      `<div class="tt-row"><span class="pill ${m.side}">${m.side === "buy" ? "买" : "卖"}</span> ` +
      `<b>${m.ticker}</b> ${m.shares} @ $${m.price}` +
      (m.portfolio ? ` <span class="muted">· ${escapeHtml(m.portfolio)}</span>` : "") +
      (m.reason ? `<div class="tt-reason">${escapeHtml(m.reason)}</div>` : "") + `</div>`).join("");
    tip.hidden = false;
    const w = tip.offsetWidth || 260;
    let left = param.point.x + 16;
    if (left + w > host.clientWidth) left = param.point.x - w - 12;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = Math.max(8, param.point.y + 8) + "px";
  });
}

// ----- form -----------------------------------------------------------------
function readForm() {
  return {
    date: $("f_date").value, ticker: $("f_ticker").value,
    side: $("f_side").value, shares: $("f_shares").value,
    price: $("f_price").value, fees: $("f_fees").value || 0,
    reason: $("f_reason").value,
    portfolio_id: +$("f_portfolio").value || undefined,
  };
}
function resetForm() {
  state.editingId = null;
  $("tradeForm").reset();
  $("f_date").value = new Date().toISOString().slice(0, 10);
  $("f_portfolio").disabled = false;
  renderTradePortfolioSelect();   // form.reset() jumps the select to option 0
  $("formTitle").textContent = "添加交易";
  $("submitBtn").textContent = "添加";
  $("cancelEdit").hidden = true;
  $("formError").hidden = true;
}
window.editTrade = function (id) {
  const t = state.trades.find((x) => x.id === id);
  if (!t) return;
  state.editingId = id;
  // editing keeps the trade in its portfolio — the select is just a display
  if (t.portfolio_id) $("f_portfolio").value = String(t.portfolio_id);
  $("f_portfolio").disabled = true;
  $("f_date").value = t.date; $("f_ticker").value = t.ticker;
  $("f_side").value = t.side; $("f_shares").value = t.shares;
  $("f_price").value = t.price; $("f_fees").value = t.fees || "";
  $("f_reason").value = t.reason || "";
  $("formTitle").textContent = "编辑交易 #" + id;
  $("submitBtn").textContent = "保存";
  $("cancelEdit").hidden = false;
  window.scrollTo({ top: 0, behavior: "smooth" });
};
window.deleteTrade = async function (id) {
  if (!confirm("确定删除这笔交易？")) return;
  await api("DELETE", "/api/trades/" + id);
  if (state.editingId === id) resetForm();
  await load();
};

// ----- events ---------------------------------------------------------------
function wire() {
  $("tradeForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("formError");
    try {
      const body = readForm();
      if (state.editingId) await api("PUT", "/api/trades/" + state.editingId, body);
      else {
        const j = await api("POST", "/api/trades", body);
        const pf = state.portfolios.find((p) => p.id === j.portfolio_id);
        showToast(pf ? `已添加到「${pf.name}」 ✓` : "交易已添加 ✓", true);
      }
      resetForm();
      await load();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  });
  $("cancelEdit").addEventListener("click", resetForm);

  document.querySelectorAll("#freqSeg button").forEach((b) =>
    b.addEventListener("click", () => {
      state.freq = b.dataset.freq;
      syncToolbar(state.data);
      renderChart(state.data, state.freq);
    }));

  document.querySelectorAll("#modeSeg button").forEach((b) =>
    b.addEventListener("click", async () => {
      await api("POST", "/api/settings", { display_mode: b.dataset.mode });
      await load();
    }));

  $("ma10Chk").addEventListener("change", (e) => { state.ma10 = e.target.checked; renderChart(state.data, state.freq); });
  $("ma40Chk").addEventListener("change", (e) => { state.ma40 = e.target.checked; renderChart(state.data, state.freq); });

  $("capitalSave").addEventListener("click", async () => {
    await api("POST", "/api/settings", { starting_capital: $("capitalInput").value || 0 });
    await load();
  });

  $("portfolioSelect").addEventListener("change", async (e) => {
    const v = e.target.value;
    if (v === "__newgroup__") { e.target.value = String(state.activeId); openGroupModal(); return; }
    if (v === "all") await api("POST", "/api/view-all", {});
    else if (v.startsWith("group:")) await api("POST", `/api/groups/${v.slice(6)}/activate`, {});
    else await api("POST", `/api/portfolios/${v}/activate`, {});
    resetForm();
    await load();
  });
  $("groupEdit").addEventListener("click", () => {
    const g = (state.groups || []).find((x) => "group:" + x.id === state.activeId);
    if (g) openGroupModal(g);
  });
  $("groupDelete").addEventListener("click", async () => {
    const g = (state.groups || []).find((x) => "group:" + x.id === state.activeId);
    if (!g || !confirm(`删除自选分组「${g.name}」？（不影响组合本身和交易）`)) return;
    await api("DELETE", `/api/groups/${g.id}`);
    resetForm();
    await load();
  });
  // group create/edit modal
  $("groupModalClose").addEventListener("click", () => { $("groupModal").hidden = true; });
  $("groupCancel").addEventListener("click", () => { $("groupModal").hidden = true; });
  $("groupModal").addEventListener("click", (e) => { if (e.target === $("groupModal")) $("groupModal").hidden = true; });
  $("groupSave").addEventListener("click", saveGroup);
  // 复盘 (post-trade review) modal
  $("reviewClose").addEventListener("click", () => { $("reviewModal").hidden = true; });
  $("reviewCancel").addEventListener("click", () => { $("reviewModal").hidden = true; });
  $("reviewModal").addEventListener("click", (e) => { if (e.target === $("reviewModal")) $("reviewModal").hidden = true; });
  $("reviewSave").addEventListener("click", saveReview);
  $("pfNew").addEventListener("click", async () => {
    const name = prompt("新组合名称：", "");
    if (name === null || !name.trim()) return;
    try { await api("POST", "/api/portfolios", { name: name.trim() }); resetForm(); await load(); }
    catch (ex) { alert(ex.message); }
  });
  $("pfRename").addEventListener("click", async () => {
    const cur = state.portfolios.find((p) => p.id === state.activeId);
    const name = prompt("重命名组合：", cur ? cur.name : "");
    if (name === null || !name.trim()) return;
    try { await api("PUT", `/api/portfolios/${state.activeId}`, { name: name.trim() }); await load(); }
    catch (ex) { alert(ex.message); }
  });
  $("pfDelete").addEventListener("click", async () => {
    const cur = state.portfolios.find((p) => p.id === state.activeId);
    if (!confirm(`确定删除组合「${cur ? cur.name : ""}」及其全部交易记录？此操作不可撤销。`)) return;
    await api("DELETE", `/api/portfolios/${state.activeId}`);
    resetForm();
    await load();
  });

  syncPrivacyBtn();
  $("privacyBtn").addEventListener("click", () => {
    privacyOn = !privacyOn;
    localStorage.setItem("privacy", privacyOn ? "1" : "0");
    syncPrivacyBtn();
    if (state.data) {            // re-render with the new masking, no refetch
      renderStats(state.data.stats && state.data.stats.totals, state.data);
      renderHoldings((state.data.stats && state.data.stats.holdings) || [],
                     (state.data.stats && state.data.stats.totals) || {});
      renderTradeLog(state.trades);
    }
  });

  $("refreshBtn").addEventListener("click", async () => {
    const b = $("refreshBtn"); const old = b.textContent;
    b.textContent = "刷新中…"; b.disabled = true;
    // hard client-side cap so the button always recovers, even if the request
    // stalls; whatever got fetched server-side is shown on the subsequent load.
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 90000);
    try {
      const r = await fetch("/api/refresh-prices", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": getCookie("csrftoken") },
        body: "{}", signal: ctrl.signal });
      const j = await r.json().catch(() => ({}));
      if (j && j.failed && j.failed.length) {
        showToast("以下代码暂未取到价格，可稍后再试：" + j.failed.join(", "), false);
      } else {
        showToast("股价已刷新 ✓", true);
      }
    } catch (ex) {
      showToast("刷新超时，已取到的价格将显示；可稍后再试一次。", false);
    } finally {
      clearTimeout(timer);
      b.textContent = old; b.disabled = false;
      try { await load(); } catch (e) { /* load has its own error handling */ }
    }
  });

  $("seedBtn").addEventListener("click", async () => {
    try { await api("POST", "/api/seed", {}); await load(); }
    catch (ex) { alert(ex.message); }
  });

  $("clearBtn").addEventListener("click", async () => {
    if (!confirm("确定清空全部交易记录？此操作不可撤销。")) return;
    await api("POST", "/api/clear", {});
    resetForm();
    await load();
  });

  $("exportBtn").addEventListener("click", exportTradesCsv);

  // import-holdings modal
  const impAddRow = (ticker = "", shares = "", cost = "") => {
    const div = document.createElement("div");
    div.className = "imp-row";
    div.innerHTML =
      `<input class="imp-ticker" placeholder="AAPL" autocomplete="off" value="${ticker}" />` +
      `<input class="imp-shares" type="number" min="0" step="any" placeholder="100" value="${shares}" />` +
      `<input class="imp-cost" type="number" min="0" step="any" placeholder="150.00" value="${cost}" />` +
      `<button type="button" class="rowbtn del imp-del" title="删除此行">✕</button>`;
    div.querySelector(".imp-del").addEventListener("click", () => div.remove());
    $("impRows").appendChild(div);
  };
  const openImport = () => {
    $("imp_date").value = new Date().toISOString().slice(0, 10);
    $("imp_cash").value = "";
    $("impRows").innerHTML = "";
    impAddRow(); impAddRow(); impAddRow();
    $("importMsg").hidden = true;
    $("importModal").hidden = false;
  };
  const closeImport = () => { $("importModal").hidden = true; };
  $("importBtn").addEventListener("click", openImport);
  $("importClose").addEventListener("click", closeImport);
  $("importCancel").addEventListener("click", closeImport);
  $("importModal").addEventListener("click", (e) => {
    if (e.target === $("importModal")) closeImport();
  });

  // single-stock chart modal
  const closeStock = () => {
    $("stockModal").hidden = true;
    if (stockChart) { stockChart.remove(); stockChart = null; }
  };
  $("stockClose").addEventListener("click", closeStock);
  $("stockModal").addEventListener("click", (e) => {
    if (e.target === $("stockModal")) closeStock();
  });
  document.querySelectorAll("#stockFreqSeg button").forEach((b) =>
    b.addEventListener("click", () => {
      if (stockFreq === b.dataset.sfreq) return;
      stockFreq = b.dataset.sfreq;
      syncStockFreqSeg();
      loadStock();
    }));

  $("impAddRow").addEventListener("click", () => impAddRow());
  $("importSubmit").addEventListener("click", async () => {
    const msg = $("importMsg");
    const holdings = [];
    document.querySelectorAll("#impRows .imp-row").forEach((row) => {
      const ticker = row.querySelector(".imp-ticker").value.trim();
      const shares = row.querySelector(".imp-shares").value;
      const avg_cost = row.querySelector(".imp-cost").value;
      if (ticker || shares || avg_cost) holdings.push({ ticker, shares, avg_cost });
    });
    const body = { date: $("imp_date").value, cash: $("imp_cash").value || 0, holdings };
    try {
      const j = await api("POST", "/api/import-holdings", body);
      closeImport();
      await load();
      showToast(`已导入：${j.added} 个持仓${body.cash > 0 ? " + 现金" : ""} ✓`, true);
    } catch (ex) { msg.textContent = ex.message; msg.className = "msg err"; msg.hidden = false; }
  });
}

// ----- auth -----------------------------------------------------------------
let authMode = "login";     // 'login' | 'register'
let authView = "auth";      // 'auth' | 'forgot' | 'reset'
let pendingNote = null;     // one-shot note for the auth screen
let resetToken = "";        // token from a /reset?token= link
let lastAuthEmail = "";     // remembered for "resend verification"

function showApp(user) {
  state.user = user;
  $("authScreen").hidden = true;
  $("appMain").hidden = false;
  $("authedActions").hidden = false;
  $("userEmail").textContent = user.email;
}
function showNote(text) {
  $("authNote").textContent = text;
  $("authNote").hidden = false;
}
function showAuth() {
  $("appMain").hidden = true;
  $("authedActions").hidden = true;
  $("authScreen").hidden = false;
  const note = pendingNote || sessionStorage.getItem("authNote");
  if (note) { showNote(note); pendingNote = null; sessionStorage.removeItem("authNote"); }
}
function setAuthView(view) {
  authView = view;
  $("authMain").hidden = view !== "auth";
  $("forgotForm").hidden = view !== "forgot";
  $("resetForm").hidden = view !== "reset";
  $("authTitle").textContent =
    view === "forgot" ? "找回密码" : view === "reset" ? "设置新密码"
                                   : (authMode === "login" ? "登录" : "注册");
}
function setAuthMode(mode) {
  authMode = mode;
  document.querySelectorAll("#authSeg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.auth === mode));
  $("authSubmit").textContent = mode === "login" ? "登录" : "注册";
  $("auth_password").autocomplete = mode === "login" ? "current-password" : "new-password";
  $("authError").hidden = true;
  if (authView === "auth") $("authTitle").textContent = mode === "login" ? "登录" : "注册";
}
async function doAuth(e) {
  e.preventDefault();
  const err = $("authError");
  err.hidden = true;
  const isNew = authMode === "register";
  lastAuthEmail = $("auth_email").value.trim();
  const body = { email: lastAuthEmail, password: $("auth_password").value };
  try {
    const r = await fetch("/api/auth/" + authMode, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCookie("csrftoken") },
      body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "请求失败");
    showApp(j);
    $("authForm").reset();
    await load();
    showToast(isNew ? `欢迎，${j.email}！已为你创建一个空组合，开始记录第一笔交易吧 📈`
                    : "登录成功", true);
  } catch (ex) { err.textContent = ex.message; err.hidden = false; }
}
function wireAuth() {
  document.querySelectorAll("#authSeg button").forEach((b) =>
    b.addEventListener("click", () => setAuthMode(b.dataset.auth)));
  $("authForm").addEventListener("submit", doAuth);

  $("forgotLink").addEventListener("click", (e) => {
    e.preventDefault(); $("forgotMsg").hidden = true; setAuthView("forgot");
  });
  document.querySelectorAll(".backToLogin").forEach((a) =>
    a.addEventListener("click", (e) => { e.preventDefault(); setAuthView("auth"); setAuthMode("login"); }));

  $("forgotForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("forgotMsg");
    try {
      const j = await api("POST", "/api/auth/forgot-password", { email: $("forgot_email").value.trim() });
      msg.textContent = j.message; msg.className = "msg ok"; msg.hidden = false;
    } catch (ex) { msg.textContent = ex.message; msg.className = "msg err"; msg.hidden = false; }
  });

  $("resetForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("resetMsg");
    if ($("reset_pw").value !== $("reset_pw2").value) {
      msg.textContent = "两次输入的新密码不一致。"; msg.className = "msg err"; msg.hidden = false; return;
    }
    try {
      const j = await api("POST", "/api/auth/reset-password",
        { token: resetToken, new_password: $("reset_pw").value });
      msg.textContent = j.message + " 正在返回登录…"; msg.className = "msg ok"; msg.hidden = false;
      history.replaceState({}, "", "/");
      setTimeout(() => { $("resetForm").reset(); setAuthView("auth"); setAuthMode("login"); }, 1600);
    } catch (ex) { msg.textContent = ex.message; msg.className = "msg err"; msg.hidden = false; }
  });

  $("logoutBtn").addEventListener("click", async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST", headers: { "X-CSRF-Token": getCookie("csrftoken") } });
    } finally {
      sessionStorage.setItem("authNote", "已安全退出登录。");
      location.reload();
    }
  });

  // account settings modal
  $("accountBtn").addEventListener("click", () => {
    $("accountEmail").textContent = state.user ? state.user.email : "";
    $("pwForm").reset();
    $("pwMsg").hidden = true;
    $("accountModal").hidden = false;
  });
  $("accountClose").addEventListener("click", () => { $("accountModal").hidden = true; });
  $("accountModal").addEventListener("click", (e) => {
    if (e.target === $("accountModal")) $("accountModal").hidden = true;
  });
  $("pwForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("pwMsg");
    if ($("pw_new").value !== $("pw_new2").value) {
      msg.textContent = "两次输入的新密码不一致。"; msg.className = "msg err"; msg.hidden = false; return;
    }
    try {
      await api("POST", "/api/auth/change-password",
        { current_password: $("pw_current").value, new_password: $("pw_new").value });
      $("pwForm").reset();
      msg.textContent = "密码已更新 ✓"; msg.className = "msg ok"; msg.hidden = false;
    } catch (ex) { msg.textContent = ex.message; msg.className = "msg err"; msg.hidden = false; }
  });
  $("deleteAccountBtn").addEventListener("click", async () => {
    const pw = prompt("删除账号将永久清除你的全部组合与交易，且无法恢复。\n请输入登录密码确认：");
    if (!pw) return;
    try {
      await api("POST", "/api/auth/delete-account", { password: pw });
      sessionStorage.setItem("authNote", "账号已删除。");
      location.reload();
    } catch (ex) { alert(ex.message); }
  });
}

// ----- init -----------------------------------------------------------------
async function init() {
  resetForm();
  wire();
  wireAuth();
  const params = new URLSearchParams(location.search);
  if (location.pathname === "/reset") {       // landing from a password-reset email
    resetToken = params.get("token") || "";
    setAuthView("reset");
    showAuth();
    return;                                    // don't auto-login on the reset page
  }
  try {
    const r = await fetch("/api/auth/me");
    if (r.ok) { showApp(await r.json()); await load(); }
    else showAuth();
  } catch { showAuth(); }
}
init();
