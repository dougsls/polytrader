/* PolyTrader Demo — Mission Control client.
 * Single-page navigation + auto-refresh fetch loop + canvas equity curve.
 * Uses native fetch with same-origin Basic Auth (browser handles).
 */

const REFRESH_MS = 10000;
const TOAST_TIMEOUT = 3500;

const fmt = {
  usd: (v) => v == null ? "—" : `${v >= 0 ? "" : "-"}$${Math.abs(v).toFixed(2)}`,
  num: (v, d = 2) => v == null ? "—" : Number(v).toFixed(d),
  pct: (v, d = 2) => v == null ? "—" : `${(v).toFixed(d)}%`,
  pctRaw: (v, d = 2) => v == null ? "—" : `${(v * 100).toFixed(d)}%`,
  cents: (v) => v == null ? "—" : `${v >= 0 ? "+" : "-"}${Math.abs(v).toFixed(1)}¢`,
  ms: (v) => v == null ? "—" : `${Math.round(v)}ms`,
  uptime: (s) => {
    if (!s) return "—";
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  },
  short: (s, n = 14) => !s ? "—" : (s.length > n ? s.slice(0, n) + "…" : s),
  addr: (a) => !a ? "—" : `${a.slice(0, 6)}…${a.slice(-4)}`,
  time: (iso) => {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      return `${hh}:${mm}:${ss}`;
    } catch { return iso.slice(11, 19); }
  },
};

function setText(id, text) { const el = document.getElementById(id); if (el) el.textContent = text; }
function setHTML(id, html) { const el = document.getElementById(id); if (el) el.innerHTML = html; }
function setClass(id, cls) {
  const el = document.getElementById(id); if (!el) return;
  el.className = el.className.split(/\s+/).filter(c => !c.match(/^(pos|neg|warn|ok|err)$/)).join(" ");
  if (cls) el.classList.add(cls);
}

function toast(msg, kind = "ok") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = `toast show ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.className = "toast", TOAST_TIMEOUT);
}

async function api(path) {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

/* ========== Navigation ========== */
document.querySelectorAll(".nav-item").forEach(btn => {
  btn.addEventListener("click", () => {
    const view = btn.dataset.view;
    document.querySelectorAll(".nav-item").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
    const target = document.getElementById(`view-${view}`);
    if (target) target.classList.add("active");
  });
});

/* ========== Drawer ========== */
const drawerEl = document.getElementById("drawer");
const drawerMask = document.getElementById("drawer-mask");
function openDrawer(rows) {
  const body = document.getElementById("drawer-body");
  body.innerHTML = rows.map(([k, v]) =>
    `<div class="drawer-row"><span class="k">${k}</span><span class="v">${v == null ? "—" : v}</span></div>`
  ).join("");
  drawerEl.classList.add("open");
  drawerMask.classList.add("open");
}
function closeDrawer() {
  drawerEl.classList.remove("open");
  drawerMask.classList.remove("open");
}
document.getElementById("drawer-close").addEventListener("click", closeDrawer);
drawerMask.addEventListener("click", closeDrawer);

/* ========== Render: Overview / Topbar ========== */
function renderSummary(summary) {
  const r = summary.readiness || {};
  const s = summary.score || {};
  const mode = summary.mode || "paper";

  // Mode pill
  const pill = document.getElementById("mode-pill");
  pill.textContent = mode.toUpperCase();
  pill.className = `mode-pill mode-${mode}`;

  // Topbar
  setText("t-bank", fmt.usd(r.bank_total));
  const pnlEl = document.getElementById("t-pnl");
  pnlEl.textContent = fmt.usd(r.total_pnl);
  pnlEl.className = "tstat-value mono " + (r.total_pnl > 0 ? "pos" : r.total_pnl < 0 ? "neg" : "");
  setText("t-dd", fmt.pct(r.current_drawdown_pct, 2));
  setText("t-score", `${s.score ?? "—"}`);
  setText("t-uptime", fmt.uptime(summary.uptime_seconds));

  // Hero cards
  setText("m-init", `$${(r.starting_bank_usd ?? 0).toFixed(0)}`);
  setText("m-bank", `$${(r.bank_total ?? 0).toFixed(2)}`);
  const mPnl = document.getElementById("m-pnl");
  mPnl.innerHTML = `${fmt.usd(r.total_pnl)}<span class="unit">USD</span>`;
  mPnl.className = "card-value mono " + (r.total_pnl > 0 ? "pos" : r.total_pnl < 0 ? "neg" : "");
  setText("m-roi", `ROI ${fmt.pct(r.roi_pct, 2)}`);
  setText("m-dd", fmt.pct(r.current_drawdown_pct, 2));
  setText("m-dd-max", `peak ${fmt.pct(r.max_drawdown_pct, 2)}`);
  setText("m-winrate", fmt.pctRaw(r.win_rate, 1));
  setText("m-winloss", `${r.wins ?? 0} W / ${r.losses ?? 0} L`);
  setText("m-score", `${s.score ?? "—"}`);
  const verdict = s.recommendation || "—";
  const mv = document.getElementById("m-verdict");
  mv.textContent = verdict;
  mv.className = "card-sub " + (verdict === "GO" ? "pos" : verdict === "NO-GO" ? "neg" : "warn");

  // Telemetry
  setText("t-cash", fmt.usd(r.cash_available));
  setText("t-inv", fmt.usd(r.invested_usd));
  setText("t-mv", fmt.usd(r.market_value_usd));
  setText("t-open", `${r.open_positions ?? 0}`);
  const sj = r.signals_24h || {};
  const total = sj.total ?? 0;
  setText("t-sigs", `${total}`);
  const journal = r.journal_24h || {};
  const exec = (journal.executed || 0) + (journal.partial || 0);
  const skip = journal.skipped || 0;
  setText("t-exec", `${exec}`);
  setText("t-skip", `${skip}`);
  setText("t-part", `${journal.partial || 0}`);
  const rate = total > 0 ? (exec / total * 100) : 0;
  setText("t-rate", `${rate.toFixed(0)}%`);

  // Latencies
  const lat = r.latencies || {};
  setText("t-lat-clob", fmt.ms(lat.clob?.p50_ms));
  setText("t-lat-gamma", fmt.ms(lat.gamma?.p50_ms));
  setText("t-lat-data", fmt.ms(lat.data_api?.p50_ms));

  // Health LEDs
  const rtdsEl = document.getElementById("h-rtds");
  rtdsEl.className = "health-led ok";
  const clobEl = document.getElementById("h-clob");
  if (lat.clob?.p50_ms != null) {
    clobEl.className = "health-led " + (lat.clob.p50_ms < 200 ? "ok" : lat.clob.p50_ms < 500 ? "warn" : "err");
  } else {
    clobEl.className = "health-led";
  }
  document.getElementById("h-geo").className = "health-led ok";
}

function renderReadinessScore(s) {
  if (!s) return;
  const score = s.score ?? 0;
  setText("score-num", score);
  const verdict = s.recommendation || "PENDING";
  const vEl = document.getElementById("score-verdict");
  vEl.textContent = verdict;

  const orb = document.getElementById("score-circle");
  orb.className = "score-circle";
  const numEl = document.getElementById("score-num");
  numEl.className = "score-num mono";
  if (verdict === "GO") {
    orb.classList.add("go"); numEl.classList.add("pos");
    vEl.style.color = "var(--plasma)";
  } else if (verdict === "NO-GO") {
    orb.classList.add("no-go"); numEl.classList.add("neg");
    vEl.style.color = "var(--red)";
  } else {
    orb.classList.add("caution"); numEl.classList.add("warn");
    vEl.style.color = "var(--amber)";
  }
  // Arc dashoffset (circumference 2π·86 ≈ 540)
  const arc = document.getElementById("score-arc");
  const total = 540;
  arc.setAttribute("stroke-dashoffset", total - (score / 100) * total);

  // Checklist
  const components = s.components || {};
  const max = { roi: 20, drawdown: 15, slippage: 15, audit: 10, api_health: 10,
                skip_rate: 10, sample_size: 10, pnl_concentration: 10 };
  const labels = {
    roi: "ROI 24h Positive",
    drawdown: "Drawdown < 5%",
    slippage: "Slippage < 2%",
    audit: "All Skips Audited",
    api_health: "Zero Critical API Errors",
    skip_rate: "Skip Rate Coherent",
    sample_size: "Sample Size ≥ 20",
    pnl_concentration: "PnL Not Outlier-Concentrated",
  };
  const checklist = document.getElementById("checklist");
  checklist.innerHTML = Object.entries(labels).map(([k, label]) => {
    const v = components[k] ?? 0;
    const m = max[k];
    const pct = v / m;
    const cls = pct >= 0.8 ? "ok" : pct > 0 ? "warn" : "err";
    const icon = pct >= 0.8 ? "✓" : pct > 0 ? "~" : "✗";
    return `<div class="check-row">
      <div class="check-icon ${cls}">${icon}</div>
      <div class="check-label">${label}</div>
      <div class="check-points">${v}</div>
      <div class="check-max">/${m}</div>
    </div>`;
  }).join("");

  // No-go flags
  const flags = s.no_go_flags || {};
  const active = Object.entries(flags).filter(([_, v]) => v).map(([k]) => k);
  const ng = document.getElementById("nogo-area");
  if (active.length === 0) {
    ng.innerHTML = "";
  } else {
    ng.innerHTML = `<div class="nogo-panel">
      <div class="title">⚠ NO-GO Triggers Active</div>
      <ul>${active.map(f => `<li>${f.replace(/_/g, " ").toUpperCase()}</li>`).join("")}</ul>
    </div>`;
  }
}

/* ========== Render: Execution Quality ========== */
function renderExecutionQuality(eq) {
  const buy = eq.BUY || {};
  const sell = eq.SELL || {};
  setText("eq-slip-buy", fmt.pctRaw(buy.avg_slippage, 2));
  setText("eq-slip-sell", fmt.pctRaw(sell.avg_slippage, 2));
  const maxSlip = Math.max(buy.max_slippage || 0, sell.max_slippage || 0);
  setText("eq-slip-max", fmt.pctRaw(maxSlip, 2));
  const avgSpread = (buy.avg_spread || 0) + (sell.avg_spread || 0);
  const spreadVal = ((buy.count || sell.count) ? avgSpread / 2 : 0);
  setText("eq-spread", `${(spreadVal * 100).toFixed(1)}`);

  // Skip reasons breakdown
  const skips = eq.skip_reasons || [];
  const skipsEl = document.getElementById("eq-skips");
  if (skips.length === 0) {
    skipsEl.innerHTML = '<div class="empty-state">no skips</div>';
  } else {
    const total = skips.reduce((s, x) => s + x.count, 0);
    skipsEl.innerHTML = skips.map(s => {
      const pct = (s.count / total) * 100;
      const cls = ["DEPTH_EMPTY", "SLIPPAGE_HIGH", "SPREAD_WIDE"].includes(s.reason) ? "warn" : "";
      return `<div class="risk-bar-row">
        <div class="label">${s.reason}</div>
        <div class="risk-bar-track"><div class="risk-bar-fill ${cls}" style="width:${pct}%"></div></div>
        <div class="value">${s.count}</div>
      </div>`;
    }).join("");
  }

  // Whale vs bot table
  const cmp = eq.whale_vs_bot || [];
  const cmpBody = document.getElementById("eq-comparison");
  if (cmp.length === 0) {
    cmpBody.innerHTML = `<tr><td colspan="6" class="dim" style="text-align:center;padding:18px;">no executed trades</td></tr>`;
  } else {
    cmpBody.innerHTML = cmp.map(c => {
      const sideBadge = c.side === "BUY"
        ? '<span class="badge badge-info">BUY</span>'
        : '<span class="badge badge-warn">SELL</span>';
      const slipCls = c.slippage_pct > 2 ? "neg" : c.slippage_pct > 1 ? "warn" : "";
      return `<tr><td>${sideBadge}</td>
        <td class="num">${c.whale_price.toFixed(4)}</td>
        <td class="num">${c.simulated_price.toFixed(4)}</td>
        <td class="num">${fmt.cents(c.diff_cents)}</td>
        <td class="num ${slipCls}">${c.slippage_pct.toFixed(2)}%</td>
        <td>${c.status}</td></tr>`;
    }).join("");
  }
}

/* ========== Render: Trader Attribution ========== */
function renderTraders(traders) {
  const list = document.getElementById("trader-list");
  if (!traders || traders.length === 0) {
    list.innerHTML = '<div class="empty-state">— awaiting whale signals —</div>';
    return;
  }
  // Sort by demo_pnl desc
  const sorted = [...traders].sort((a, b) => (b.demo_pnl || 0) - (a.demo_pnl || 0));
  list.innerHTML = sorted.map((t, i) => {
    const verdictBadge = t.verdict === "strong" ? '<span class="badge badge-ok">STRONG</span>'
      : t.verdict === "underperforming" ? '<span class="badge badge-err">UNDER</span>'
      : '<span class="badge badge-neutral">WATCH</span>';
    const pnlCls = t.demo_pnl > 0 ? "pos" : t.demo_pnl < 0 ? "neg" : "";
    return `<div class="trader-row">
      <div class="trader-rank">#${i + 1}</div>
      <div class="trader-id">
        <span class="name">${t.name || fmt.short(t.wallet, 16)}</span>
        <span class="addr">${fmt.addr(t.wallet)}</span>
      </div>
      <div class="trader-metric"><span class="ml">Score</span><span class="mv">${(t.score || 0).toFixed(2)}</span></div>
      <div class="trader-metric"><span class="ml">Signals</span><span class="mv">${t.signals_detected}</span></div>
      <div class="trader-metric"><span class="ml">Exec %</span><span class="mv">${(t.execution_rate * 100).toFixed(0)}</span></div>
      <div class="trader-metric"><span class="ml">Slip%</span><span class="mv">${(t.avg_slippage * 100).toFixed(2)}</span></div>
      <div class="trader-metric"><span class="ml">Demo PnL</span><span class="mv ${pnlCls}">${fmt.usd(t.demo_pnl)}</span></div>
      <div class="trader-metric"><span class="ml">Public PnL</span><span class="mv">${fmt.usd(t.pnl_public)}</span></div>
      <div>${verdictBadge}</div>
    </div>`;
  }).join("");
}

/* ========== Render: Risk ========== */
function renderRisk(rx) {
  const byMarket = rx.by_market || [];
  const byTrader = rx.by_trader || [];
  const stale = rx.stale_positions || [];

  const mEl = document.getElementById("risk-by-market");
  if (byMarket.length === 0) {
    mEl.innerHTML = '<div class="empty-state">no open positions</div>';
  } else {
    const maxVal = Math.max(...byMarket.map(m => m.market_value_usd), 1);
    mEl.innerHTML = byMarket.map(m => {
      const pct = (m.market_value_usd / maxVal) * 100;
      return `<div class="risk-bar-row">
        <div class="label">${fmt.short(m.market_title || m.condition_id, 30)}</div>
        <div class="risk-bar-track"><div class="risk-bar-fill" style="width:${pct}%"></div></div>
        <div class="value">$${m.market_value_usd.toFixed(2)}</div>
      </div>`;
    }).join("");
  }

  const tEl = document.getElementById("risk-by-trader");
  if (byTrader.length === 0) {
    tEl.innerHTML = '<div class="empty-state">no open positions</div>';
  } else {
    const maxVal = Math.max(...byTrader.map(t => t.exposure_usd), 1);
    tEl.innerHTML = byTrader.map(t => {
      const pct = (t.exposure_usd / maxVal) * 100;
      return `<div class="risk-bar-row">
        <div class="label">${fmt.addr(t.wallet)}</div>
        <div class="risk-bar-track"><div class="risk-bar-fill" style="width:${pct}%"></div></div>
        <div class="value">$${t.exposure_usd.toFixed(2)}</div>
      </div>`;
    }).join("");
  }

  const sBody = document.getElementById("risk-stale");
  if (stale.length === 0) {
    sBody.innerHTML = `<tr><td colspan="6" class="dim" style="text-align:center;padding:18px;">— no stale positions —</td></tr>`;
  } else {
    sBody.innerHTML = stale.map(s => `<tr>
      <td class="cell-trunc">${s.market_title || "—"}</td>
      <td>${s.outcome || "—"}</td>
      <td class="num">${(s.size || 0).toFixed(2)}</td>
      <td class="num">${(s.entry || 0).toFixed(4)}</td>
      <td class="num">${s.current != null ? s.current.toFixed(4) : "—"}</td>
      <td class="dim">${fmt.short(s.opened_at || "", 19)}</td>
    </tr>`).join("");
  }
}

/* ========== Render: Positions ========== */
function renderPositions(positions) {
  const tbody = document.getElementById("positions-tbl");
  if (!positions || positions.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="dim" style="text-align:center;padding:18px;">— no open positions —</td></tr>`;
    return;
  }
  tbody.innerHTML = positions.map(p => {
    const unrealCls = p.unrealized_pnl > 0 ? "pos" : p.unrealized_pnl < 0 ? "neg" : "";
    return `<tr>
      <td class="cell-trunc">${p.market_title || "—"}</td>
      <td>${p.outcome || "—"}</td>
      <td class="num">${(p.size || 0).toFixed(2)}</td>
      <td class="num">${(p.avg_entry_price || 0).toFixed(4)}</td>
      <td class="num">${p.current_price != null ? p.current_price.toFixed(4) : "—"}</td>
      <td class="num ${unrealCls}">${fmt.usd(p.unrealized_pnl)}</td>
    </tr>`;
  }).join("");
}

/* ========== Render: Journal ========== */
let journalFilters = { status: null, side: null };

function renderJournal(rows) {
  const tbody = document.getElementById("journal-tbl");
  if (!rows || rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="11" class="dim" style="text-align:center;padding:18px;">— journal empty —</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(r => {
    let badge;
    if (r.status === "executed") badge = '<span class="badge badge-ok">FILL</span>';
    else if (r.status === "partial") badge = '<span class="badge badge-warn">PART</span>';
    else if (r.status === "skipped") badge = '<span class="badge badge-neutral">SKIP</span>';
    else badge = '<span class="badge badge-err">FAIL</span>';
    if (r.is_emergency) badge += ' <span class="badge badge-warn">⚠</span>';
    const sideBadge = r.side === "BUY"
      ? '<span class="badge badge-info">B</span>'
      : '<span class="badge badge-warn">S</span>';
    const slipCls = (r.slippage * 100) > 2 ? "neg" : (r.slippage * 100) > 1 ? "warn" : "";
    const reason = r.skip_reason || "—";
    return `<tr data-row='${JSON.stringify(r).replace(/'/g, "&#39;")}'>
      <td class="dim">${fmt.time(r.timestamp)}</td>
      <td>${sideBadge}</td>
      <td class="cell-trunc">${r.market_title || "—"}</td>
      <td>${r.outcome || "—"}</td>
      <td class="num">${(r.whale_price || 0).toFixed(4)}</td>
      <td class="num">${(r.simulated_price || 0).toFixed(4)}</td>
      <td class="num ${slipCls}">${(r.slippage * 100).toFixed(2)}%</td>
      <td class="num">${(r.spread * 100).toFixed(1)}¢</td>
      <td class="num">${(r.simulated_size || 0).toFixed(1)}/${(r.intended_size || 0).toFixed(1)}</td>
      <td>${badge}</td>
      <td class="dim">${reason}</td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll("tr[data-row]").forEach(tr => {
    tr.addEventListener("click", () => {
      try {
        const r = JSON.parse(tr.dataset.row.replace(/&#39;/g, "'"));
        openDrawer([
          ["TIME", r.timestamp],
          ["WALLET", r.wallet],
          ["MARKET", r.market_title],
          ["OUTCOME", r.outcome],
          ["SIDE", r.side],
          ["WHALE PRICE", `${(r.whale_price || 0).toFixed(4)}`],
          ["REF PRICE", `${(r.ref_price || 0).toFixed(4) || "—"}`],
          ["SIM PRICE", `${(r.simulated_price || 0).toFixed(4)}`],
          ["SPREAD", `${(r.spread * 100).toFixed(2)}¢`],
          ["SLIPPAGE", `${(r.slippage * 100).toFixed(2)}%`],
          ["INTENDED SIZE", `${(r.intended_size || 0).toFixed(2)} tokens`],
          ["SIM SIZE", `${(r.simulated_size || 0).toFixed(2)} tokens`],
          ["DEPTH USD", fmt.usd(r.depth_usd)],
          ["STATUS", r.status],
          ["SKIP REASON", r.skip_reason || "—"],
          ["EMERGENCY", r.is_emergency ? "yes" : "no"],
          ["SOURCE", r.source],
          ["CONFLUENCE", r.confluence_count],
          ["SIGNAL ID", r.signal_id],
        ]);
      } catch (e) { console.error(e); }
    });
  });
}

document.querySelectorAll("[data-f-status]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-f-status]").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    const v = btn.dataset.fStatus;
    journalFilters.status = v === "all" ? null : v;
    refreshJournal();
  });
});
document.querySelectorAll("[data-f-side]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-f-side]").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    const v = btn.dataset.fSide;
    journalFilters.side = v === "all" ? null : v;
    refreshJournal();
  });
});

async function refreshJournal() {
  try {
    const params = new URLSearchParams({ limit: 200 });
    if (journalFilters.status) params.set("status", journalFilters.status);
    if (journalFilters.side) params.set("side", journalFilters.side);
    const data = await api(`/api/demo/journal?${params}`);
    renderJournal(data.rows || []);
  } catch (e) {
    console.error("journal", e);
  }
}

/* ========== Equity Curve canvas ========== */
function drawEquityCurve(points) {
  const canvas = document.getElementById("equity-canvas");
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);

  if (!points || points.length < 2) {
    ctx.fillStyle = "rgba(170, 180, 196, 0.4)";
    ctx.font = "11px JetBrains Mono, monospace";
    ctx.textAlign = "center";
    ctx.fillText("— awaiting equity data —", W / 2, H / 2);
    setText("ec-range", "no data");
    return;
  }

  const banks = points.map(p => p.bank);
  const minB = Math.min(...banks);
  const maxB = Math.max(...banks);
  const range = maxB - minB || 1;
  const pad = 16;

  // Bank line — cyan glow
  ctx.beginPath();
  ctx.strokeStyle = "#06d4ff";
  ctx.lineWidth = 1.6;
  ctx.shadowColor = "rgba(6, 212, 255, 0.6)";
  ctx.shadowBlur = 8;
  points.forEach((p, i) => {
    const x = pad + (i / (points.length - 1)) * (W - pad * 2);
    const y = H - pad - ((p.bank - minB) / range) * (H - pad * 2);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.shadowBlur = 0;

  // Filled area below bank line
  ctx.lineTo(W - pad, H - pad);
  ctx.lineTo(pad, H - pad);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, "rgba(6, 212, 255, 0.18)");
  grad.addColorStop(1, "rgba(6, 212, 255, 0)");
  ctx.fillStyle = grad;
  ctx.fill();

  // Drawdown overlay (dashed red)
  const maxDD = Math.max(...points.map(p => p.dd_pct), 5);
  ctx.beginPath();
  ctx.strokeStyle = "rgba(255, 77, 109, 0.55)";
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 4]);
  points.forEach((p, i) => {
    const x = pad + (i / (points.length - 1)) * (W - pad * 2);
    const y = pad + (p.dd_pct / maxDD) * ((H - pad * 2) * 0.4);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.setLineDash([]);

  setText("ec-range", `${points.length} pts · $${minB.toFixed(2)} → $${maxB.toFixed(2)}`);
}

async function refreshEquity() {
  try {
    const data = await api("/api/demo/equity-curve?hours=24");
    drawEquityCurve(data.points || []);
  } catch (e) { console.error("equity", e); }
}

/* ========== Per-section refresh ========== */
async function refreshOverview() {
  try {
    const summary = await api("/api/demo/summary");
    renderSummary(summary);
    renderReadinessScore(summary.score);
  } catch (e) { console.error("summary", e); toast(`API error: ${e.message}`, "err"); }
}

async function refreshExecution() {
  try {
    const eq = await api("/api/demo/execution-quality");
    renderExecutionQuality(eq);
  } catch (e) { console.error("execution", e); }
}

async function refreshTraders() {
  try {
    const data = await api("/api/demo/trader-attribution");
    renderTraders(data.traders || []);
  } catch (e) { console.error("traders", e); }
}

async function refreshRisk() {
  try {
    const rx = await api("/api/demo/risk-exposure");
    renderRisk(rx);
  } catch (e) { console.error("risk", e); }
}

async function refreshPositions() {
  try {
    const data = await api("/api/positions");
    renderPositions(data.positions || data || []);
  } catch (e) { console.error("positions", e); }
}

async function refreshAll() {
  await Promise.all([
    refreshOverview(),
    refreshExecution(),
    refreshTraders(),
    refreshRisk(),
    refreshPositions(),
    refreshJournal(),
    refreshEquity(),
  ]);
}

/* ========== Init + auto-refresh loop ========== */
refreshAll();
setInterval(refreshAll, REFRESH_MS);
window.addEventListener("resize", () => refreshEquity());
