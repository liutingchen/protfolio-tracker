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
const fmtMoney = (v) => (v == null ? "—" : "$" + nf.format(v));
const signMoney = (v) => (v == null ? "—" : (v >= 0 ? "+$" : "-$") + nf.format(Math.abs(v)));
const signPct = (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + nf.format(v) + "%");
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
  const [st, ch] = await Promise.all([api("GET", "/api/state"), api("GET", "/api/chart")]);
  state.trades = st.trades;
  state.settings = st.settings;
  state.portfolios = st.portfolios || [];
  state.activeId = st.active_id;
  state.isAll = st.active_id === "all";
  state.data = ch;
  renderPortfolioSwitch();
  setFormEnabled(!state.isAll);
  syncToolbar(ch);
  renderStats(ch.stats && ch.stats.totals, ch);
  renderHoldings((ch.stats && ch.stats.holdings) || []);
  renderTradeLog(st.trades);
  renderWarnings(ch);
  renderChart(ch, state.freq);
}

function renderPortfolioSwitch() {
  const sel = $("portfolioSelect");
  const opts = ['<option value="all">▦ 全部组合（合并）</option>'].concat(
    state.portfolios.map((p) =>
      `<option value="${p.id}">${escapeHtml(p.name)}${p.num_trades ? ` · ${p.num_trades}笔` : ""}</option>`));
  sel.innerHTML = opts.join("");
  sel.value = state.isAll ? "all" : String(state.activeId);
  $("pfDelete").disabled = state.isAll || state.portfolios.length <= 1;
  $("pfRename").disabled = state.isAll;
}

function setFormEnabled(enabled) {
  ["f_date", "f_ticker", "f_side", "f_shares", "f_price", "f_fees", "f_reason", "submitBtn"]
    .forEach((id) => { const el = $(id); if (el) el.disabled = !enabled; });
  $("formHint").hidden = enabled;
  $("tradeForm").classList.toggle("disabled", !enabled);
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
  $("capitalInput").disabled = state.isAll;   // sum is read-only in combined view
  $("capitalSave").disabled = state.isAll;
  $("clearBtn").hidden = state.isAll || state.trades.length === 0;
}

function renderStats(t, ch) {
  const bar = $("statsBar");
  if (!t || t.total_value == null) { bar.innerHTML = ""; return; }
  const cls = (v) => (v >= 0 ? "pos" : "neg");
  const items = [
    ["账户总值", fmtMoney(t.total_value), ""],
    ["累计盈亏", signMoney(t.total_pnl), cls(t.total_pnl)],
    ["收益率", signPct(t.return_pct), t.return_pct == null ? "" : cls(t.return_pct)],
    ["已实现盈亏", signMoney(t.realized_pnl), cls(t.realized_pnl)],
    ["未实现盈亏", signMoney(t.unrealized_pnl), cls(t.unrealized_pnl)],
    ["持仓市值", fmtMoney(t.market_value), ""],
    ["现金", fmtMoney(t.cash), ""],
    ["持仓数", t.num_positions, ""],
  ];
  bar.innerHTML = items.map(([k, v, c]) =>
    `<div class="stat"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("");
}

function renderHoldings(holdings) {
  const box = $("holdings");
  if (!holdings.length) { box.innerHTML = `<p class="empty-mini">暂无持仓。</p>`; return; }
  box.innerHTML = `<table><thead><tr>
      <th>代码</th><th>股数</th><th>均价</th><th>现价</th><th>市值</th><th>浮动盈亏</th>
    </tr></thead><tbody>` +
    holdings.map((h) => {
      const c = (h.unrealized || 0) >= 0 ? "pos" : "neg";
      return `<tr>
        <td class="tickercell">${h.ticker}</td>
        <td>${h.shares}</td>
        <td>${h.avg_cost == null ? "—" : "$" + nf.format(h.avg_cost)}</td>
        <td>${h.last_price == null ? "—" : "$" + nf.format(h.last_price)}</td>
        <td>${fmtMoney(h.market_value)}</td>
        <td class="${c}">${signMoney(h.unrealized)} <span class="muted">(${signPct(h.unrealized_pct)})</span></td>
      </tr>`;
    }).join("") + `</tbody></table>`;
}

function renderTradeLog(trades) {
  const box = $("tradeLog");
  if (!trades.length) { box.innerHTML = `<p class="empty-mini">还没有交易记录。</p>`; return; }
  const rows = [...trades].reverse();
  const isAll = state.isAll;
  box.innerHTML = `<table><thead><tr>
      <th>日期</th>${isAll ? "<th>组合</th>" : ""}<th>代码</th><th>方向</th><th>股数</th><th>价格</th><th>费用</th>
      <th class="reasoncell">原因</th>${isAll ? "" : "<th></th>"}
    </tr></thead><tbody>` +
    rows.map((t) => `<tr>
      <td>${t.date}</td>
      ${isAll ? `<td>${escapeHtml(t.portfolio_name || "")}</td>` : ""}
      <td class="tickercell">${t.ticker}</td>
      <td style="text-align:center"><span class="pill ${t.side}">${t.side === "buy" ? "买入" : "卖出"}</span></td>
      <td>${nf.format(t.shares)}</td>
      <td>$${nf.format(t.price)}</td>
      <td>${t.fees ? "$" + nf.format(t.fees) : "—"}</td>
      <td class="reasoncell">${escapeHtml(t.reason || "")}</td>
      ${isAll ? "" : `<td style="white-space:nowrap">
        <button class="rowbtn" onclick="editTrade(${t.id})">✎</button>
        <button class="rowbtn del" onclick="deleteTrade(${t.id})">🗑</button>
      </td>`}
    </tr>`).join("") + `</tbody></table>`;
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
  if (!ch.has_data) return;

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

  main.setMarkers(d.markers.map((m) => ({
    time: m.time, position: m.position, color: m.color, shape: m.shape, text: m.text,
  })));

  setupTooltip(host, d.markers);
  chart.timeScale().fitContent();
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
  };
}
function resetForm() {
  state.editingId = null;
  $("tradeForm").reset();
  $("f_date").value = new Date().toISOString().slice(0, 10);
  $("formTitle").textContent = "添加交易";
  $("submitBtn").textContent = "添加";
  $("cancelEdit").hidden = true;
  $("formError").hidden = true;
}
window.editTrade = function (id) {
  const t = state.trades.find((x) => x.id === id);
  if (!t) return;
  state.editingId = id;
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
      else await api("POST", "/api/trades", body);
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
    if (e.target.value === "all") await api("POST", "/api/view-all", {});
    else await api("POST", `/api/portfolios/${e.target.value}/activate`, {});
    resetForm();
    await load();
  });
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

  $("refreshBtn").addEventListener("click", async () => {
    const b = $("refreshBtn"); const old = b.textContent;
    b.textContent = "刷新中…"; b.disabled = true;
    try { await api("POST", "/api/refresh-prices", {}); await load(); }
    finally { b.textContent = old; b.disabled = false; }
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
