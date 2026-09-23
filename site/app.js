/* ============================================================
   TalkChart — «графики, которые разговаривают»
   Vanilla JS, 0 зависимостей. Живые данные GeckoTerminal API
   из браузера посетителя; snapshot.json — офлайн-фолбэк.
   ============================================================ */
"use strict";

const GT = "https://api.geckoterminal.com/api/v2";
const REFRESH_MS = 60000;

const state = {
  pools: [],
  selected: null,
  ohlcv: [],          // [[ts,o,h,l,c,v],...] старые -> новые
  lang: localStorage.getItem("tc_lang") || "ru",
  live: false,
  switches: 0,
  alerts: JSON.parse(localStorage.getItem("tc_alerts") || "[]"),
  firedAlerts: new Set(),
};
window.__SLOT_CLICKS__ = [];

/* ---------- утилиты ---------- */
const $ = (s) => document.querySelector(s);
const fmtUsd = (n) => {
  if (n == null || isNaN(n)) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return "$" + (n / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return "$" + (n / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return "$" + (n / 1e3).toFixed(1) + "K";
  if (a >= 1) return "$" + n.toFixed(2);
  if (a >= 0.01) return "$" + n.toFixed(4);
  return "$" + n.toPrecision(3);
};
const fmtPct = (n) => (n > 0 ? "+" : "") + Number(n).toFixed(1) + "%";
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));


/* ============================================================
   ПИКСЕЛЬ-РЕНДЕР
   ------------------------------------------------------------
   Канвас рисуется в уменьшенном разрешении (PS пикселей на «жирную» точку)
   и растягивается CSS'ом с image-rendering: pixelated. Отсюда правила:
   - все координаты квантуются по сетке PS;
   - сглаживание выключено, линии — целочисленные прямоугольники;
   - цвета берутся из CSS-переменных темы, поэтому чарт перекрашивается
     вместе со светлой/тёмной темой.
   ============================================================ */
const PIXEL_FONT = '"Press Start 2P", ui-monospace, "Courier New", monospace';
const PS = 2;                       // «жирный» пиксель: 2 CSS-px

function cssVar(name, fallback) {
  try {
    if (typeof getComputedStyle !== "function" || !document.documentElement) return fallback;
    const v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  } catch (e) {
    return fallback;
  }
}

function themeColors() {
  return {
    up: cssVar("--green", "#3ddc84"),
    down: cssVar("--red", "#ff4d6a"),
    acc: cssVar("--acc", "#7cf03d"),
    acc2: cssVar("--acc2", "#ffd23f"),
    grid: cssVar("--line", "#2b394d"),
    mut: cssVar("--mut", "#8b98a5"),
    tx: cssVar("--tx", "#e8edf2"),
    bg: cssVar("--bg", "#0b0f18"),
    panel: cssVar("--panel", "#131a26"),
  };
}

/* Готовит канвас: backing store = CSS-размер / PS, система координат — CSS-px. */
function pixelCanvas(cv, wCss, hCss, ps) {
  const scale = ps || PS;
  const w = Math.max(80, Math.round(wCss / scale));
  const h = Math.max(60, Math.round(hCss / scale));
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  const ctx = cv.getContext("2d");
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(1 / scale, 1 / scale);
  ctx.imageSmoothingEnabled = false;
  return { ctx: ctx, q: (v) => Math.round(v / scale) * scale, scale: scale };
}

/* Пунктир «в клетку»: ручная штриховка вместо setLineDash — так она остаётся
   пиксельной и одинаковой при любом DPR. */
function pixelDashedH(ctx, x0, x1, y, step, color, thickness) {
  ctx.fillStyle = color;
  const t = thickness || 2;
  for (let x = x0; x < x1; x += step * 2) {
    ctx.fillRect(Math.round(x), Math.round(y), Math.min(step, x1 - x), t);
  }
}

/* Шахматный «полутон» — ретро-заливка без градиентов. */
function ditherRect(ctx, x, y, w, h, color, cell) {
  const c = cell || 4;
  ctx.fillStyle = color;
  for (let yy = Math.round(y); yy < y + h; yy += c) {
    const shift = (Math.round((yy - y) / c) % 2) ? c : 0;
    for (let xx = Math.round(x) + shift; xx < x + w; xx += c * 2) {
      ctx.fillRect(xx, yy, Math.min(c, x + w - xx), Math.min(c, y + h - yy));
    }
  }
}

/* ---------- данные ---------- */
async function fetchJson(url) {
  const r = await fetch(url, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

async function loadPools() {
  try {
    const j = await fetchJson(`${GT}/networks/solana/trending_pools?page=1`);
    state.pools = j.data.slice(0, 12).map((d) => {
      const a = d.attributes;
      return {
        address: a.address,
        name: a.name,
        base_symbol: a.name.split(" / ")[0],
        base_address: d.relationships.base_token.data.id.split("_")[1],
        dex: d.relationships.dex.data.id,
        price_usd: parseFloat(a.base_token_price_usd),
        created_at: a.pool_created_at,
        fdv_usd: a.fdv_usd ? parseFloat(a.fdv_usd) : null,
        reserve_usd: a.reserve_in_usd ? parseFloat(a.reserve_in_usd) : null,
        volume_h24: a.volume_usd.h24 ? parseFloat(a.volume_usd.h24) : null,
        change: {
          h1: parseFloat(a.price_change_percentage?.h1 ?? 0),
          h6: parseFloat(a.price_change_percentage?.h6 ?? 0),
          h24: parseFloat(a.price_change_percentage?.h24 ?? 0),
        },
        tx_h1: { buys: a.transactions?.h1?.buys ?? 0, sells: a.transactions?.h1?.sells ?? 0 },
      };
    });
    state.live = true;
  } catch (e) {
    const snap = await fetchJson("data/snapshot.json");
    state.pools = snap.pools;
    state.live = false;
  }
  if (!state.selected || !state.pools.find((p) => p.address === state.selected.address)) {
    state.selected = state.pools[0];
  }
  renderList();
  renderStatus();
}

async function loadOhlcv(pool) {
  if (state.live) {
    try {
      const j = await fetchJson(`${GT}/networks/solana/pools/${pool.address}/ohlcv/hour?aggregate=1&limit=48`);
      const list = j.data.attributes.ohlcv_list.slice().reverse();
      state.ohlcv = list;
      drawChart();
      renderNarrative();
      return;
    } catch (e) { /* падаем в фолбэк */ }
  }
  if (pool.ohlcv_h1) {
    state.ohlcv = pool.ohlcv_h1.slice().reverse();
  } else {
    state.ohlcv = synthCandles(pool);
  }
  drawChart();
  renderNarrative();
}

// Синтетические свечи из цены и изменений — только для офлайн-демо.
function synthCandles(pool) {
  const out = [];
  const now = Math.floor(Date.now() / 1000);
  let p = pool.price_usd / (1 + pool.change.h24 / 100);
  const drift = pool.price_usd / 48 - p / 48;
  let seed = pool.address.charCodeAt(0) + pool.address.charCodeAt(5);
  const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };
  for (let i = 47; i >= 0; i--) {
    const o = p;
    p = p + drift + p * (rnd() - 0.5) * 0.04;
    const h = Math.max(o, p) * (1 + rnd() * 0.015);
    const l = Math.min(o, p) * (1 - rnd() * 0.015);
    out.push([now - i * 3600, o, h, l, p, 1000 + rnd() * 5000]);
  }
  return out;
}

/* ---------- движок нарративов («график, который разговаривает») ---------- */
function narrative(pool, lang) {
  const c = pool.change, tx = pool.tx_h1 || { buys: 0, sells: 0 };
  const buyRatio = tx.buys + tx.sells ? tx.buys / (tx.buys + tx.sells) : 0.5;
  const ageDays = pool.created_at ? Math.max(0, Math.round((Date.now() - new Date(pool.created_at)) / 86400000)) : null;
  const turnover = pool.fdv_usd ? pool.volume_h24 / pool.fdv_usd : null;
  const ru = lang === "ru";
  const parts = [];

  // направление
  if (c.h24 >= 30) parts.push(ru ? `${pool.base_symbol} разрывает: ${fmtPct(c.h24)} за 24ч` : `${pool.base_symbol} is ripping: ${fmtPct(c.h24)} in 24h`);
  else if (c.h24 >= 8) parts.push(ru ? `${pool.base_symbol} растёт на ${fmtPct(c.h24)} за сутки` : `${pool.base_symbol} is up ${fmtPct(c.h24)} over 24h`);
  else if (c.h24 <= -25) parts.push(ru ? `${pool.base_symbol} в обвале: ${fmtPct(c.h24)} за 24ч` : `${pool.base_symbol} is getting dumped: ${fmtPct(c.h24)} in 24h`);
  else if (c.h24 <= -8) parts.push(ru ? `${pool.base_symbol} теряет ${fmtPct(c.h24)} за сутки` : `${pool.base_symbol} is down ${fmtPct(c.h24)} over 24h`);
  else parts.push(ru ? `${pool.base_symbol} в боковике: ${fmtPct(c.h24)} за сутки` : `${pool.base_symbol} is chopping sideways: ${fmtPct(c.h24)} in 24h`);

  // моментум h1 против h6
  if (c.h1 > 3 && c.h6 > c.h1) parts.push(ru ? "ускорение в последнем часе" : "accelerating in the last hour");
  else if (c.h1 < -3 && c.h6 < 0) parts.push(ru ? "давление продаж нарастает" : "sell pressure is building");

  // покупатели/продавцы + дивергенции «поток сделок vs цена»
  if (buyRatio >= 0.62) {
    parts.push(ru ? `покупатели доминируют (${Math.round(buyRatio * 100)}% сделок за час)` : `buyers in control (${Math.round(buyRatio * 100)}% of 1h trades)`);
    if (c.h1 < -3) parts.push(ru ? "но цена всё равно падает — кто-то разгружается в стакан" : "yet price keeps dropping — someone is unloading into the book");
  } else if (buyRatio <= 0.38) {
    parts.push(ru ? `продают в рынок (${Math.round((1 - buyRatio) * 100)}% сделок за час)` : `sellers in control (${Math.round((1 - buyRatio) * 100)}% of 1h trades)`);
    if (c.h24 >= 15) parts.push(ru ? "суточный памп остывает" : "the 24h pump is cooling off");
  }

  // объём/ликвидность
  if (turnover != null && turnover > 1.5) parts.push(ru ? `объём ${fmtUsd(pool.volume_h24)} больше капы в ${turnover.toFixed(1)}× — бумага в огне` : `volume ${fmtUsd(pool.volume_h24)} is ${turnover.toFixed(1)}× market cap — this thing is on fire`);
  if (pool.reserve_usd != null && pool.reserve_usd < 250000) parts.push(ru ? `ликвидность тонкая (${fmtUsd(pool.reserve_usd)}) — движения будут резкими` : `liquidity is thin (${fmtUsd(pool.reserve_usd)}) — expect violent moves`);
  else if (pool.reserve_usd != null && pool.reserve_usd > 3e6) parts.push(ru ? `стакан глубокий: ${fmtUsd(pool.reserve_usd)} ликвидности` : `deep book: ${fmtUsd(pool.reserve_usd)} in liquidity`);

  // возраст
  if (ageDays != null && ageDays <= 3) parts.push(ru ? `пулу ${ageDays} дн. — чистая рулетка` : `pool is ${ageDays}d old — pure roulette`);
  else if (ageDays != null && ageDays <= 14) parts.push(ru ? `пулу ${ageDays} дн., история короткая` : `pool is only ${ageDays}d old`);

  const flags = [];
  if (pool.reserve_usd != null && pool.reserve_usd < 250000) flags.push(ru ? "тонкая ликвидность" : "thin liquidity");
  if (ageDays != null && ageDays <= 7) flags.push(ru ? "молодой пул" : "young pool");
  if (buyRatio <= 0.38) flags.push(ru ? "давление продаж" : "sell pressure");
  if (c.h24 >= 50) flags.push(ru ? "перегрет за 24ч" : "overheated 24h");

  return { text: parts.join(". ") + ".", flags };
}

/* ---------- рендер ---------- */
function renderStatus() {
  $("#status").innerHTML = state.live
    ? '<span class="dot live"></span>LIVE · GeckoTerminal'
    : '<span class="dot snap"></span>SNAPSHOT 2026-09-21 · демо-данные';
}

function renderList() {
  $("#pool-list").innerHTML = state.pools.map((p) => {
    const sel = state.selected && p.address === state.selected.address;
    const cls = p.change.h24 >= 0 ? "up" : "down";
    return `<button class="pool ${sel ? "sel" : ""}" data-addr="${p.address}">
      <span class="pool-name">${esc(p.base_symbol)}<small>/${esc(p.name.split(" / ")[1] || "SOL")}</small></span>
      <span class="pool-px">${fmtUsd(p.price_usd)}</span>
      <span class="pool-chg ${cls}">${fmtPct(p.change.h24)}</span>
    </button>`;
  }).join("");
  document.querySelectorAll(".pool").forEach((b) =>
    b.addEventListener("click", () => selectPool(b.dataset.addr))
  );
}

function selectPool(addr) {
  const p = state.pools.find((x) => x.address === addr);
  if (!p || (state.selected && p.address === state.selected.address)) return;
  state.selected = p;
  state.switches++;
  markNavStep("pool_selected");
  renderList();
  renderHeader();
  loadOhlcv(p);
  checkAlerts(p);
  fetchWhales(p);
  if (state.switches % window.INTERSTITIAL_EVERY === 0) showInterstitial();
}

function renderHeader() {
  const p = state.selected;
  if (!p) return;
  $("#hdr-name").textContent = p.name;
  $("#hdr-dex").textContent = `${p.dex} · ${esc(p.base_address.slice(0, 4))}…${esc(p.base_address.slice(-4))}`;
  $("#hdr-price").textContent = fmtUsd(p.price_usd);
  const ch = $("#hdr-change");
  ch.textContent = fmtPct(p.change.h24) + " / 24ч";
  ch.className = "hdr-change " + (p.change.h24 >= 0 ? "up" : "down");
  $("#hdr-stats").innerHTML = [
    ["Vol 24ч", fmtUsd(p.volume_h24)],
    ["FDV", fmtUsd(p.fdv_usd)],
    ["Ликвидность", fmtUsd(p.reserve_usd)],
    ["Покупки/продажи 1ч", `${p.tx_h1?.buys ?? "—"} / ${p.tx_h1?.sells ?? "—"}`],
  ].map(([k, v]) => `<div class="stat"><span>${k}</span><b>${v}</b></div>`).join("");
}

function renderNarrative() {
  const p = state.selected;
  if (!p) return;
  const { text, flags } = narrative(p, state.lang);
  $("#narrative").innerHTML =
    `<div class="narr-head">🗣 ${state.lang === "ru" ? "Что говорит график" : "What the chart says"}</div>
     <p>${esc(text)}</p>
     ${flags.length ? `<div class="flags">${flags.map((f) => `<span class="flag">⚠ ${esc(f)}</span>`).join("")}</div>` : ""}`;
}

/* ---------- чарт (canvas, без библиотек) ---------- */
function drawChart() {
  const cv = $("#chart");
  if (!cv) return;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H) return;
  const pc = pixelCanvas(cv, W, H, PS);
  const ctx = pc.ctx, q = pc.q;
  const C = themeColors();
  ctx.clearRect(0, 0, W, H);
  const data = state.ohlcv;
  if (!data || data.length < 2) return;

  const pad = { t: 12, r: 64, b: 28, l: 8 };
  const cw = W - pad.l - pad.r, chh = (H - pad.t - pad.b) * 0.74, vh = (H - pad.t - pad.b) * 0.2;
  const hi = Math.max.apply(null, data.map((d) => d[2]));
  const lo = Math.min.apply(null, data.map((d) => d[3]));
  const span = hi - lo || hi * 0.01 || 1;
  const y = (price) => q(pad.t + chh - ((price - lo) / span) * chh);
  const bw = cw / data.length;

  // сетка — пунктир из точек, а не линия
  ctx.font = "9px " + PIXEL_FONT;
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const price = lo + (span * i) / 4;
    const yy = y(price);
    for (let x = pad.l; x < pad.l + cw; x += 8) {
      ctx.fillStyle = C.grid;
      ctx.fillRect(q(x), yy, 2, 2);
    }
    ctx.fillStyle = C.mut;
    ctx.fillText(fmtUsd(price), pad.l + cw + 8, yy);
  }

  // свечи: тело целочисленной ширины, «чернильная» подложка под телом
  const maxV = Math.max.apply(null, data.map((d) => d[5])) || 1;
  const bodyW = Math.max(PS * 2, q(bw * 0.64));
  data.forEach((d, i) => {
    const o = d[1], h = d[2], l = d[3], c = d[4], v = d[5];
    const x = q(pad.l + i * bw + bw / 2);
    const up = c >= o;
    const col = up ? C.up : C.down;

    ctx.fillStyle = col;                                  // фитиль
    ctx.fillRect(x - PS, y(h), PS * 2, Math.max(PS, y(l) - y(h)));
    const top = y(Math.max(o, c)), bot = y(Math.min(o, c));
    const bh = Math.max(PS * 2, bot - top);
    ctx.fillRect(x - bodyW / 2, top, bodyW, bh);          // тело
    ctx.fillStyle = C.tx;                                 // блик одной полосой
    ctx.globalAlpha = 0.18;
    ctx.fillRect(x - bodyW / 2, top, PS, bh);
    ctx.globalAlpha = 1;

    const vhh = (v / maxV) * vh;                          // объём полутоном
    ditherRect(ctx, x - bodyW / 2, pad.t + chh + 10 + (vh - vhh), bodyW, vhh, col, PS * 2);
  });

  // линия последней цены + «табличка» с ценой
  const last = data[data.length - 1][4];
  const ly = y(last);
  pixelDashedH(ctx, pad.l, pad.l + cw, ly, PS * 3, C.acc, PS);
  const label = fmtUsd(last);
  const tw = ctx.measureText(label).width + 10;
  ctx.fillStyle = C.acc;
  ctx.fillRect(pad.l + cw + 4, ly - 8, tw, 16);
  ctx.fillStyle = C.bg;
  ctx.fillText(label, pad.l + cw + 9, ly);

  // рамка кадра, как у игрового HUD
  ctx.fillStyle = C.grid;
  ctx.fillRect(pad.l, q(pad.t), cw, PS);
  ctx.fillRect(pad.l, q(pad.t + chh), cw, PS);
}

/* ---------- алерты ---------- */
function checkAlerts(p) {
  state.alerts.forEach((a, i) => {
    if (a.addr !== p.address || state.firedAlerts.has(i)) return;
    const hit = (a.dir === "above" && p.price_usd >= a.price) || (a.dir === "below" && p.price_usd <= a.price);
    if (hit) {
      state.firedAlerts.add(i);
      banner(`🔔 ${p.base_symbol}: цена ${fmtUsd(p.price_usd)} — ваш алерт (${a.dir === "above" ? "выше" : "ниже"} ${fmtUsd(a.price)}) сработал`);
    }
  });
}
function addAlert() {
  const p = state.selected;
  const raw = prompt(`Алерт по ${p.base_symbol} (цена сейчас ${fmtUsd(p.price_usd)}).\nФормат: >0.0004  или  <0.0003`);
  if (!raw) return;
  const m = raw.trim().match(/^([<>])\s*([\d.]+)$/);
  if (!m) return banner("Формат алерта: >цена или <цена");
  state.alerts.push({ addr: p.address, sym: p.base_symbol, dir: m[1] === ">" ? "above" : "below", price: parseFloat(m[2]) });
  localStorage.setItem("tc_alerts", JSON.stringify(state.alerts));
  banner(`Алерт сохранён: ${p.base_symbol} ${m[1]} ${m[2]}. Проверка каждую минуту (локально).`);
}
function banner(msg) {
  const b = $("#banner");
  b.textContent = msg;
  b.classList.add("show");
  clearTimeout(b._t);
  b._t = setTimeout(() => b.classList.remove("show"), 6000);
}

/* ---------- шаринг-карточка (водяной знак = бесплатная дистрибуция) ---------- */
function shareCard() {
  const p = state.selected;
  const W = 1200, H = 630;
  const cv = document.createElement("canvas");
  cv.width = W; cv.height = H;
  const ctx = cv.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  const C = themeColors();
  const up = (p.change.h24 || 0) >= 0;
  const col = up ? C.up : C.down;
  const font = (size) => size + "px " + PIXEL_FONT;

  ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
  ditherRect(ctx, 0, 0, W, H, C.grid, 16);                 // фон «в клетку»
  ctx.fillStyle = C.panel; ctx.fillRect(16, 16, W - 32, H - 32);
  ctx.fillStyle = col;                                       // двойная рамка
  ctx.fillRect(16, 16, W - 32, 8);
  ctx.fillRect(16, H - 24, W - 32, 8);
  ctx.fillRect(16, 16, 8, H - 32);
  ctx.fillRect(W - 24, 16, 8, H - 32);
  ctx.fillStyle = C.acc;
  ctx.fillRect(32, 32, W - 64, 6);

  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = C.tx; ctx.font = font(56);
  ctx.fillText(String(p.base_symbol).toUpperCase(), 56, 140);
  ctx.fillStyle = col; ctx.font = font(48);
  ctx.fillText(fmtPct(p.change.h24), 56, 216);
  ctx.fillStyle = C.mut; ctx.font = font(18);
  ctx.fillText(`${p.name} · ${p.dex} · VOL ${fmtUsd(p.volume_h24)}`, 56, 262);

  // блочный спарклайн: столбики, а не сглаженная линия
  const data = state.ohlcv.slice(-48);
  if (data.length > 1) {
    const closes = data.map((d) => d[4]);
    const hi = Math.max.apply(null, closes), lo = Math.min.apply(null, closes), span = hi - lo || 1;
    const x0 = 56, y0 = 320, w = W - 112, h = 150;
    const step = Math.max(4, Math.floor(w / closes.length));
    closes.forEach((c, i) => {
      const bh = Math.max(4, Math.round(((c - lo) / span) * h));
      ctx.fillStyle = closes[closes.length - 1] >= closes[0] ? C.up : C.down;
      ctx.fillRect(x0 + i * step, y0 + (h - bh), step - 2, bh);
    });
  }

  const { text } = narrative(p, state.lang);
  ctx.fillStyle = C.tx; ctx.font = font(18);
  wrapText(ctx, text.slice(0, 150), 56, 520, W - 112, 30);

  ctx.fillStyle = C.acc; ctx.font = font(20);
  ctx.fillText("TALKCHART — ГРАФИКИ, КОТОРЫЕ РАЗГОВАРИВАЮТ", 56, H - 56);

  const a = document.createElement("a");
  a.download = `${p.base_symbol}-talkchart.png`;
  a.href = cv.toDataURL("image/png");
  a.click();
  track("share_card", p.address);
  markNavStep("card_shared");
}

/* ---------- Solana-нативные интеграции: Jupiter Swap + Solana Blinks ---------- */
function openJupiterSwap() {
  const p = state.selected;
  if (!p) return;
  const mint = p.base_address;
  if (window.Jupiter && typeof window.Jupiter.init === "function") {
    try {
      window.Jupiter.init({
        displayMode: "modal",
        endpoint: "https://api.mainnet-beta.solana.com",
        formProps: {
          initialOutputMint: mint || undefined,
        },
      });
      track("open_jupiter_modal", p.address);
      markNavStep("swap_opened");
      return;
    } catch (e) {
      console.warn("Jupiter Terminal init fallback:", e);
    }
  }
  const url = mint ? `https://jup.ag/swap/SOL-${mint}` : "https://jup.ag";
  window.open(url, "_blank", "noopener");
  track("open_jupiter_link", p.address);
}

function copyBlinkUrl() {
  const p = state.selected;
  if (!p) return;
  const origin = window.location.origin;
  const path = window.location.pathname.replace(/\/index\.html$/, "").replace(/\/$/, "");
  const actionApi = `${origin}${path}/api/actions/${p.address}.json`;
  const blinkUrl = `https://dial.to/?action=solana-action:${encodeURIComponent(actionApi)}`;

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(blinkUrl).then(() => {
      alert(`🔗 Solana Blink скопирован!\n\nВставь эту ссылку в пост на X (Twitter), чтобы развернуть интерактивный виджет:\n\n${blinkUrl}`);
    }).catch(() => {
      prompt("Скопируй Solana Blink для X:", blinkUrl);
    });
  } else {
    prompt("Скопируй Solana Blink для X:", blinkUrl);
  }
  track("copy_blink", p.address);
}

function wrapText(ctx, text, x, y, maxW, lh) {
  const words = text.split(" ");
  let line = "";
  for (const w of words) {
    const t = line + w + " ";
    if (ctx.measureText(t).width > maxW && line) { ctx.fillText(line, x, y); line = w + " "; y += lh; }
    else line = t;
  }
  ctx.fillText(line, x, y);
}

/* ---------- инвентарь игр ---------- */
function renderGames() {
  let tiplinkHtml = "";
  if (window.TIPLINK_CONFIG && window.TIPLINK_CONFIG.enabled) {
    const tc = window.TIPLINK_CONFIG;
    tiplinkHtml = `
      <div class="tiplink-box">
        <div class="tiplink-title">${esc(tc.title)}</div>
        <div class="tiplink-sub">${esc(tc.description)}</div>
        <a class="tiplink-cta" href="${esc(tc.claimUrl)}" target="_blank" rel="noopener" id="tiplink-claim-btn">${esc(tc.cta)}</a>
      </div>
    `;
  }
  $("#games").innerHTML = tiplinkHtml + window.GAMES.map((g) => `
    <a class="game-card" href="${esc(g.url)}" target="_blank" rel="noopener" data-game="${g.id}" style="--accent:${g.accent}">
      <div class="game-name">${esc(g.name)}</div>
      <div class="game-tag">${esc(g.tagline)}</div>
      <span class="game-cta">${esc(g.cta)} →</span>
    </a>`).join("");
  const tlBtn = $("#tiplink-claim-btn");
  if (tlBtn) tlBtn.addEventListener("click", () => track("click_tiplink", "sidebar"));
  document.querySelectorAll(".game-card").forEach((a) =>
    a.addEventListener("click", () => track("click_slot", a.dataset.game))
  );
}

/* ---------- playable-интерстишал: «лови зелёную свечу» ---------- */
function showInterstitial() {
  const ov = $("#interstitial");
  const game = window.GAMES[Math.floor(Math.random() * window.GAMES.length)];
  $("#ist-cta").href = game.url;
  $("#ist-cta").textContent = `${game.cta} в ${game.name} →`;
  $("#ist-cta").onclick = () => track("interstitial_cta", game.id);
  ov.classList.add("show");

  const cv = $("#ist-canvas");
  const GW = 320, GH = 240;
  const pc = pixelCanvas(cv, GW, GH, 3);        // «жирный» пиксель ×3 — аркаднее
  const ctx = pc.ctx, q = pc.q;
  const C = themeColors();
  let score = 0, t0 = Date.now(), candles = [], done = false;

  const spawn = () => candles.push({
    x: q(24 + Math.random() * (GW - 48)), y: -24,
    v: 1.6 + Math.random() * 2.4, green: Math.random() < 0.4,
  });

  /* Свеча — спрайт 12×28 с обводкой: пиксели, а не «фигура из прямоугольников». */
  function drawCandle(x, y, green) {
    const w = 12, h = 28, body = 14;
    const cx = q(x) - w / 2, cy = q(y) - h / 2;
    ctx.fillStyle = C.bg;
    ctx.fillRect(cx - 3, cy - 6, w + 6, h + 12);            // «чернильная» подложка
    ctx.fillStyle = green ? C.up : C.down;
    ctx.fillRect(cx + 3, cy - 6, 6, 6);                      // верхний фитиль
    ctx.fillRect(cx + 3, cy + h - 0, 6, 6);                  // нижний фитиль
    ctx.fillRect(cx, cy, w, body);                           // тело
    ctx.fillStyle = C.tx;                                    // блик
    ctx.globalAlpha = 0.25;
    ctx.fillRect(cx, cy, 3, body);
    ctx.globalAlpha = 1;
    ctx.fillStyle = C.bg;                                    // «глаза» — пиксель-душа
    ctx.fillRect(cx + 3, cy + 4, 3, 3);
    ctx.fillRect(cx + 6, cy + 4, 3, 3);
  }

  cv.onclick = (e) => {
    const r = cv.getBoundingClientRect();
    const mx = (e.clientX - r.left) * (GW / r.width), my = (e.clientY - r.top) * (GH / r.height);
    candles.forEach((c, i) => {
      if (Math.abs(c.x - mx) < 18 && Math.abs(c.y - my) < 26) {
        score += c.green ? 10 : -5;
        if (score < 0) score = 0;
        candles.splice(i, 1);
      }
    });
  };

  const loop = () => {
    const el = (Date.now() - t0) / 1000;
    if (el > 6 && !done) { done = true; $("#ist-score").textContent = `СЧЁТ: ${score}`; }
    if (done) { ctx.clearRect(0, 0, GW, GH); return; }
    if (Math.random() < 0.08) spawn();

    ctx.clearRect(0, 0, GW, GH);
    ditherRect(ctx, 0, 0, GW, GH, C.grid, 12);               // фон «в клетку»
    candles.forEach((c, i) => {
      c.y += c.v;
      drawCandle(c.x, c.y, c.green);
      if (c.y > GH + 30) candles.splice(i, 1);
    });

    // HUD: полоса времени + счёт пиксельным шрифтом
    ctx.fillStyle = C.bg;
    ctx.fillRect(0, 0, GW, 24);
    ctx.fillStyle = C.grid;
    ctx.fillRect(0, 22, GW, 2);
    const left = Math.max(0, 6 - Math.floor(el));
    const wpx = Math.round((Math.max(0, 6 - el) / 6) * (GW - 24));
    ctx.fillStyle = left > 1 ? C.acc : C.down;
    ctx.fillRect(12, 8, wpx, 8);
    ctx.fillStyle = C.tx;
    ctx.font = "8px " + PIXEL_FONT;
    ctx.textBaseline = "middle";
    ctx.fillText(`СЧЁТ ${score}  ${left}C`, 12, 40);
    requestAnimationFrame(loop);
  };
  loop();
}
function hideInterstitial() { $("#interstitial").classList.remove("show"); }

/* ---------- трекинг & Watchtower телеметрия ---------- */
/* Consent/opt-out (PRIVACY.md): ?notrack=1 и localStorage tc_notrack=1 выключают
   отправку телеметрии немедленно и персистентно; ?notrack=0 включает обратно.
   Дополнительно уважаются navigator.doNotTrack и Global Privacy Control.      */
function trackingOptOut() {
  try {
    const q = new URLSearchParams(location.search);
    if (q.has("notrack")) {
      localStorage.setItem("tc_notrack", q.get("notrack") === "1" ? "1" : "0");
    }
    if (localStorage.getItem("tc_notrack") === "1") return true;
    if (navigator.doNotTrack === "1" || navigator.globalPrivacyControl === true) return true;
  } catch (e) {}
  return false;
}

function getPseudoSessionId() {
  let sid = localStorage.getItem("tc_session_id");
  if (!sid) {
    sid = "sess_" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    localStorage.setItem("tc_session_id", sid);
  }
  return sid;
}

function getNextSeq() {
  let s = parseInt(localStorage.getItem("tc_seq"), 10) || 0;
  s += 1;
  localStorage.setItem("tc_seq", s);
  return s;
}

/* Источник хранится КЛАССОМ (source_systems контракта), а не полным URL —
   полный referrer может содержать чувствительные query-параметры (PII). */
function classifyReferrer(r) {
  if (!r) return "direct_web";
  try {
    const h = new URL(r).hostname.toLowerCase();
    if (h.includes("t.co") || h.includes("twitter.") || h.includes("x.com")) return "x_twitter";
    if (h.includes("perplexity.")) return "perplexity_ai";
    if (h.includes("openai.") || h.includes("chatgpt.")) return "chatgpt_search";
    if (h.includes("google.") || h.includes("bing.") || h.includes("duckduckgo.")) return "google_search";
    if (h.includes("tiktok.") || h.includes("youtube.") || h.includes("youtu.be")
        || h.includes("instagram.") || h.includes("facebook.")) return "short_video";
    if (h.includes("tiplink.")) return "tiplink_referral";
  } catch (e) {}
  return "direct_web";
}

function bumpTabEventCount() {
  try {
    const n = (parseInt(sessionStorage.getItem("tc_tab_events"), 10) || 0) + 1;
    sessionStorage.setItem("tc_tab_events", String(n));
    return n;
  } catch (e) { return 1; }
}

function sendWatchtowerEvent(evtType, payload) {
  if (trackingOptOut()) return;
  const sid = getPseudoSessionId();
  const seq = getNextSeq();
  bumpTabEventCount();
  const cid = (payload && payload.campaignId) || "talkchart_interactive_radar";
  const pid = "target_terminal"; // канонический pageId; адрес пула — в payload
  const p = {
    ...(payload || {}),
    pool: state.selected?.base_symbol,
    poolAddress: state.selected?.address, // публичный on-chain адрес пула, не PII
  };
  const ev = {
    eventId: "ev_" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36),
    identity: `offchain:trafficgen:${cid}:${pid}:${sid}:${seq}`,
    chain: "offchain",
    source: "trafficgen",
    app: "trafficgen",
    eventType: evtType,
    timestamp: new Date().toISOString(),
    observedAt: new Date().toISOString(),
    campaignId: cid,
    sourceId: classifyReferrer(document.referrer),
    sourceType: "real",
    pageId: pid,
    sessionId: sid,
    seq: seq,
    payload: p,
    parserVersion: "trafficgen-v1",
    dataQuality: "complete"
  };

  try {
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/api/track", JSON.stringify(ev));
    } else {
      fetch("/api/track", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(ev),
        keepalive: true
      }).catch(() => {});
    }
  } catch (e) {}
}

/* Явное завершение сессии: реальная длительность вкладки и число событий. */
window.addEventListener("pagehide", () => {
  try {
    const start = parseInt(sessionStorage.getItem("tc_tab_start"), 10) || Date.now();
    const count = parseInt(sessionStorage.getItem("tc_tab_events"), 10) || 0;
    sendWatchtowerEvent("SessionEnded", {
      durationSeconds: Math.max(0, Math.round((Date.now() - start) / 1000)),
      eventCount: count
    });
  } catch (e) {}
});

/* ---------- NavigationCompleted: однозначный маппинг навигации ----------
   Шаги терминала фиксируются в sessionStorage; событие уходит один раз на путь,
   когда все шаги пройдены в объявленном порядке. Никаких «похоже, дошёл»:
   либо путь пройден целиком, либо события нет.                              */
const NAV_PATHS = [
  { id: "terminal_core", steps: ["pool_selected", "call_placed", "call_resolved"] },
  { id: "share_flow", steps: ["pool_selected", "card_shared"] },
  { id: "swap_flow", steps: ["pool_selected", "swap_opened"] },
];

function navSteps() {
  try { return JSON.parse(sessionStorage.getItem("tc_nav_steps") || "[]"); } catch (e) { return []; }
}

function markNavStep(step) {
  try {
    if (trackingOptOut()) return;
    const steps = navSteps();
    if (steps.some((s) => s.step === step)) return; // шаг уже отмечен
    steps.push({ step: step, ts: Date.now() });
    sessionStorage.setItem("tc_nav_steps", JSON.stringify(steps));
    checkNavPaths(steps);
  } catch (e) {}
}

function checkNavPaths(steps) {
  for (const path of NAV_PATHS) {
    let done = false;
    try { done = sessionStorage.getItem("tc_nav_done_" + path.id) === "1"; } catch (e) {}
    if (done) continue;
    let cursor = 0;
    const passed = [];
    for (const s of steps) {
      const at = path.steps.indexOf(s.step, cursor);
      if (at >= 0) { passed.push(s); cursor = at + 1; }
    }
    if (passed.length !== path.steps.length) continue;
    try { sessionStorage.setItem("tc_nav_done_" + path.id, "1"); } catch (e) {}
    sendWatchtowerEvent("NavigationCompleted", {
      path: path.id,
      steps: path.steps,
      completedSteps: passed.length,
      durationMs: passed[passed.length - 1].ts - passed[0].ts,
    });
  }
}

function track(evt, id) {
  const rec = { evt, id, ts: Date.now(), pool: state.selected?.address };
  window.__SLOT_CLICKS__.push(rec);
  try {
    const k = `tc_${evt}_cnt`;
    localStorage.setItem(k, (parseInt(localStorage.getItem(k), 10) || 0) + 1);
  } catch (e) {}
  console.info("[track]", rec);

  // Маппинг событий на канонические события Watchtower
  let wtType = "Click";
  let payload = { action: evt, target: id, pool: state.selected?.base_symbol };
  if (evt === "click_slot") {
    wtType = "CTAClicked";
    payload.campaignId = "talkchart_interactive_radar";
    payload.target = id;
  } else if (evt === "click_tiplink") {
    wtType = "CTAClicked";
    payload.campaignId = "tiplink_welcome_drop";
    payload.channel = "google_onboarding";
  } else if (evt === "interstitial_cta") {
    wtType = "CTAClicked";
    payload.source = "interstitial";
  } else if (evt === "page_view") {
    wtType = "PageView";
  } else if (evt === "call_resolved") {
    // Раньше это событие отправлялось как NavigationCompleted напрямую — это
    // был не маппинг, а подмена: разрешение прогноза不等于 завершённой
    // навигации. Теперь это шаг пути, а событие шлёт checkNavPaths().
    markNavStep("call_resolved");
  }
  sendWatchtowerEvent(wtType, payload);
}

/* ---------- язык ---------- */
function toggleLang() {
  state.lang = state.lang === "ru" ? "en" : "ru";
  localStorage.setItem("tc_lang", state.lang);
  $("#lang-btn").textContent = state.lang === "ru" ? "EN" : "RU";
  renderNarrative();
}

/* ---------- whale radar: лента крупных сделок выбранного пула ---------- */
async function fetchWhales(pool) {
  let whales = pool.whales || [];
  if (state.live) {
    try {
      const j = await fetchJson(`${GT}/networks/solana/pools/${pool.address}/trades?limit=1000`);
      const now = Date.now();
      const rows = (j.data || []).map((d) => d.attributes || {});
      const vols = rows.map((a) => parseFloat(a.volume_in_usd)).filter((v) => v > 0).sort((a, b) => a - b);
      const median = vols.length ? vols[Math.floor(vols.length / 2)] : 0;
      const thr = Math.max(250, 10 * median);   // калибровка ленты, как в фабрике
      whales = rows
        .map((a) => ({
          kind: a.kind || "?",
          usd: parseFloat(a.volume_in_usd),
          mult: median ? Math.round((parseFloat(a.volume_in_usd) / median) * 10) / 10 : null,
          whale: parseFloat(a.volume_in_usd) >= 25000,
          hours_ago: Math.max(0, (now - new Date(a.block_timestamp).getTime()) / 3600000),
          addr8: (a.tx_from_address || "?").slice(0, 8),
        }))
        .filter((w) => w.usd >= thr && w.hours_ago <= 24)
        .sort((a, b) => b.usd - a.usd)
        .slice(0, 6);
    } catch (e) { /* фолбэк: whales из snapshot фабрики */ }
  }
  renderWhales(whales);
}
function renderWhales(ws) {
  const el = $("#whale-radar");
  if (!ws.length) {
    el.innerHTML = '<div class="mut">Китовых сделок от $25K за последние 24ч по этому пулу нет.</div>';
    return;
  }
  el.innerHTML = ws.map((w) => `<div class="whale ${w.kind === "buy" ? "buy" : "sell"}">
    <span class="w-side">${w.kind === "buy" ? "BUY" : "SELL"}</span>
    <b>${fmtUsd(w.usd)}</b>
    <span class="mut">${w.whale ? "whale" : (w.mult ? w.mult + "× медианы ленты" : "")}
      ${w.hours_ago != null ? "· " + w.hours_ago.toFixed(1) + "ч назад" : ""} · ${esc(w.addr8)}…</span>
  </div>`).join("");
}

/* ---------- бумажные прогнозы «угадай свечу» (1ч, paper points, streak) ---------- */
const CALLS_KEY = "tc_calls";
function loadCalls() { return JSON.parse(localStorage.getItem(CALLS_KEY) || '{"history":[],"pending":[]}'); }
function saveCalls(c) { localStorage.setItem(CALLS_KEY, JSON.stringify(c)); }
function activeCall() {
  const c = loadCalls();
  return c.pending.find((p) => Date.now() - p.ts < 3600000) || null;
}
function callStats() {
  const h = loadCalls().history;
  const wins = h.filter((x) => x.win).length;
  let streak = 0, best = 0, run = 0;
  for (const x of h) { run = x.win ? run + 1 : 0; best = Math.max(best, run); if (x.win) streak = run; else streak = 0; }
  return { n: h.length, wins, streak, best };
}
function placeCall(dir) {
  const p = state.selected;
  if (!p || activeCall()) return;
  const c = loadCalls();
  c.pending.push({ addr: p.address, sym: p.base_symbol, dir, entry: p.price_usd, ts: Date.now() });
  saveCalls(c);
  track("call_placed", p.address + ":" + dir);
  markNavStep("call_placed");
  banner(`Прогноз принят: ${p.base_symbol} ${dir === "up" ? "ВВЕРХ" : "ВНИЗ"} на час. Разрешится по живой цене, пока терминал открыт.`);
  renderCallUI();
}
function resolveCalls() {
  const c = loadCalls();
  if (!c.pending.length) return;
  const now = Date.now();
  let changed = false;
  c.pending = c.pending.filter((pc) => {
    if (now - pc.ts < 3600000) return true;
    const pool = state.pools.find((x) => x.address === pc.addr);
    if (!pool) return true; // пул вне тренда — подождём следующего появления
    const up = pool.price_usd > pc.entry;
    const win = (pc.dir === "up") === up;
    c.history.push({ ...pc, resolved_ts: now, exit: pool.price_usd, win });
    changed = true;
    banner(`${win ? "✅ Угадали" : "❌ Мимо"}: ${pc.sym} за час ${up ? "вырос" : "упал"} (${fmtUsd(pc.entry)} → ${fmtUsd(pool.price_usd)})`);
    if (win && c.history.slice(-3).every((h) => h.win) && c.history.length >= 3) {
      const bonus = (window.TIPLINK_CONFIG && window.TIPLINK_CONFIG.streakBonusCredits) || 500;
      banner(`🔥 СЕРИЯ 3 ПОБЕДЫ! Открыт TipLink-бонус: +${bonus} кредитов к играм студии!`);
    }
    track("call_resolved", pc.addr + ":" + (win ? "win" : "loss"));
    return false;
  });
  if (changed) { saveCalls(c); renderCallUI(); }
}
function renderCallUI() {
  const ui = $("#call-ui");
  if (!ui) return;
  const st = callStats();
  const act = activeCall();
  const statsHtml = st.n
    ? `<span class="call-stats" title="точность ${Math.round((st.wins / st.n) * 100)}%">🎯 ${st.wins}/${st.n}${st.streak > 1 ? " · серия " + st.streak : ""}</span>`
    : "";
  if (act) {
    const left = Math.max(0, 60 - (Date.now() - act.ts) / 60000);
    ui.innerHTML = `${statsHtml}<span class="call-chip ${act.dir}">${esc(act.sym)} ${act.dir === "up" ? "UP" : "DOWN"} · ${left.toFixed(0)} мин</span>`;
  } else {
    ui.innerHTML = `${statsHtml}<span class="mut" style="font-size:11px">свеча 1ч:</span>
      <button class="btn call up" data-dir="up">UP</button>
      <button class="btn call down" data-dir="down">DOWN</button>
      ${st.n ? '<button class="btn ghost" id="call-share" title="Карточка результатов">🏆</button>' : ""}`;
    ui.querySelectorAll("[data-dir]").forEach((b) => (b.onclick = () => placeCall(b.dataset.dir)));
    const sh = ui.querySelector("#call-share");
    if (sh) sh.onclick = shareCallCard;
  }
}
function shareCallCard() {
  const st = callStats();
  if (!st.n) return;
  const W = 1200, H = 630;
  const cv = document.createElement("canvas");
  cv.width = W; cv.height = H;
  const ctx = cv.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  const C = themeColors();
  const font = (size) => size + "px " + PIXEL_FONT;

  ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
  ditherRect(ctx, 0, 0, W, H, C.grid, 16);
  ctx.fillStyle = C.panel; ctx.fillRect(16, 16, W - 32, H - 32);
  ctx.fillStyle = C.acc;
  ctx.fillRect(16, 16, W - 32, 8); ctx.fillRect(16, H - 24, W - 32, 8);
  ctx.fillRect(16, 16, 8, H - 32); ctx.fillRect(W - 24, 16, 8, H - 32);

  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = C.tx; ctx.font = font(40);
  ctx.fillText("МОИ ПРОГНОЗЫ", 56, 140);
  ctx.fillStyle = C.acc2; ctx.font = font(96);
  ctx.fillText(`${Math.round((st.wins / st.n) * 100)}%`, 56, 280);
  ctx.fillStyle = C.mut; ctx.font = font(18);
  ctx.fillText(`УГАДАНО ${st.wins} ИЗ ${st.n} · СЕРИЯ ${st.best}`, 56, 330);
  ctx.fillText("БУМАЖНЫЕ ПРОГНОЗЫ · ЧАСОВОЙ ТАЙМФРЕЙМ", 56, 370);
  if (st.best >= 3) {
    ctx.fillStyle = C.acc2; ctx.font = font(20);
    ctx.fillText("* РАЗБЛОКИРОВАН БОНУС К ИГРАМ СТУДИИ", 56, 430);
  }
  // «пиксельные сердечки» — прогресс вместо текста
  const hearts = Math.min(5, Math.max(1, Math.round((st.wins / st.n) * 5)));
  for (let i = 0; i < 5; i++) {
    ctx.fillStyle = i < hearts ? C.acc : C.grid;
    const hx = 56 + i * 56;
    ctx.fillRect(hx, 480, 12, 12); ctx.fillRect(hx + 24, 480, 12, 12);
    ctx.fillRect(hx + 12, 492, 12, 12); ctx.fillRect(hx + 6, 504, 24, 12);
    ctx.fillRect(hx + 12, 516, 12, 12);
  }
  ctx.fillStyle = C.acc; ctx.font = font(20);
  ctx.fillText("TALKCHART — ГРАФИКИ, КОТОРЫЕ РАЗГОВАРИВАЮТ", 56, H - 56);

  const a = document.createElement("a");
  a.download = "talkchart-my-calls.png";
  a.href = cv.toDataURL("image/png");
  a.click();
  track("share_call_card", "stats");
}

/* ---------- витрина вертикальных видео фабрики ---------- */
async function renderVideoStrip() {
  try {
    const j = await fetchJson("videos/latest/manifest.json");
    const vs = (j.videos || []).slice(0, 3);
    if (!vs.length) return;
    // mp4 живут в CI-артефактах (конвенция репо: *.mp4 вне git); на Pages их нет —
    // onerror прячет витрину, в локальном превью файлы есть и играют
    $("#videos-strip").innerHTML = vs.map((v) =>
      `<video src="${esc(v.latest_file || v.file)}" muted loop playsinline preload="metadata"
        title="${esc(v.symbol)} ${fmtPct(v.chg_h24)} — 15с история"
        onerror="this.parentElement && this.parentElement.childElementCount <= 1 ? document.getElementById('videos-wrap').remove() : this.remove()"
        onclick="this.paused ? this.play() : this.pause()"></video>`).join("");
    $("#videos-wrap").classList.add("has-cards");
  } catch (e) { /* фабрика ещё не рендерила видео — витрина скрыта */ }
}

/* ---------- витрина карточек фабрики (cards/latest/manifest.json) ---------- */
async function renderCardStrip() {
  try {
    const j = await fetchJson("cards/latest/manifest.json");
    const cards = (j.cards || []).slice(0, 8);
    if (!cards.length) return;
    $("#cards-strip").innerHTML = cards.map((c) =>
      `<a href="${esc(c.file)}" target="_blank" rel="noopener" title="${esc(c.symbol)} ${fmtPct(c.chg_h24)}">
         <img loading="lazy" src="${esc(c.latest_file || c.file)}" alt="Карточка ${esc(c.symbol)}">
       </a>`).join("");
    $("#cards-wrap").classList.add("has-cards");
  } catch (e) { /* фабрика ещё не запускалась — витрина скрыта */ }
}

/* ---------- Ончейн-баттл дня (голосование + переход в игры) ---------- */
async function renderVsWidget() {
  const wrap = $("#vs-wrap");
  const box = $("#vs-box");
  if (!wrap || !box) return;
  try {
    const res = await fetch(`data/vs_latest.json?t=${Date.now()}`);
    if (!res.ok) return;
    const vs = await res.json();
    if (!vs || !vs.token1 || !vs.token2) return;

    const voteKey = `tc_vs_vote_${vs.slug}`;
    const myVote = localStorage.getItem(voteKey);

    box.innerHTML = `
      <div class="vs-header">
        <div class="vs-title">⚔️ ${esc(vs.token1.symbol)} (${fmtPct(vs.token1.change_24h)}) vs ${esc(vs.token2.symbol)} (${fmtPct(vs.token2.change_24h)})</div>
        <a href="vs/${esc(vs.slug)}.html" style="font-size:12px;color:#7cf03d">полный разбор →</a>
      </div>
      <div class="vs-buttons">
        <button class="vs-btn ${myVote === '1' ? 'voted' : ''}" id="vs-vote-1">
          Голос за ${esc(vs.token1.symbol)}
        </button>
        <button class="vs-btn ${myVote === '2' ? 'voted' : ''}" id="vs-vote-2">
          Голос за ${esc(vs.token2.symbol)}
        </button>
      </div>
      <div class="vs-verdict"><b>🧠 Вердикт алгоритма:</b> ${esc(vs.verdict)}</div>
    `;

    box.querySelector("#vs-vote-1").onclick = () => {
      localStorage.setItem(voteKey, "1");
      banner(`🗳 Вы проголосовали за ${vs.token1.symbol}! Заберите TipLink-бонус к играм студии в левой колонке.`);
      track("vs_vote", `${vs.slug}:token1`);
      renderVsWidget();
    };
    box.querySelector("#vs-vote-2").onclick = () => {
      localStorage.setItem(voteKey, "2");
      banner(`🗳 Вы проголосовали за ${vs.token2.symbol}! Заберите TipLink-бонус к играм студии в левой колонке.`);
      track("vs_vote", `${vs.slug}:token2`);
      renderVsWidget();
    };

    wrap.style.display = "block";
  } catch (e) {
    // баттл ещё не собран — блок скрыт
  }
}

/* ---------- init ---------- */
async function init() {
  $("#lang-btn").textContent = state.lang === "ru" ? "EN" : "RU";
  $("#lang-btn").addEventListener("click", toggleLang);
  $("#share-btn").addEventListener("click", shareCard);
  $("#swap-btn").addEventListener("click", openJupiterSwap);
  $("#blink-btn").addEventListener("click", copyBlinkUrl);
  $("#alert-btn").addEventListener("click", addAlert);
  $("#ist-close").addEventListener("click", hideInterstitial);
  $("#ist-skip").addEventListener("click", hideInterstitial);
  // Переключение темы перекрашивает чарт: цвета он берёт из CSS-переменных,
  // поэтому достаточно перерисовать после смены атрибута data-theme.
  try {
    const themeBtn = document.getElementById("theme-btn");
    if (themeBtn && themeBtn.addEventListener) {
      themeBtn.addEventListener("click", () => setTimeout(drawChart, 0));
    }
  } catch (e) {}

  try {
    if (!sessionStorage.getItem("tc_session_started")) {
      sessionStorage.setItem("tc_session_started", "1");
      sessionStorage.setItem("tc_tab_start", String(Date.now()));
      sendWatchtowerEvent("SessionStarted", { referrerSource: classifyReferrer(document.referrer) });
    }
    sendWatchtowerEvent("PageView", { path: location.pathname + location.hash, title: document.title });
  } catch (e) {}
  renderGames();
  await loadPools();
  // deep link из SEO-страниц: index.html#pool=<address>
  const m = location.hash.match(/^#pool=([A-Za-z0-9]+)$/);
  if (m && state.pools.find((p) => p.address === m[1])) {
    state.selected = state.pools.find((p) => p.address === m[1]);
  }
  renderHeader();
  await loadOhlcv(state.selected);
  renderCardStrip();
  renderVideoStrip();
  renderCallUI();
  fetchWhales(state.selected);
  renderVsWidget();
  setInterval(async () => {
    await loadPools();
    renderHeader();
    await loadOhlcv(state.selected);
    checkAlerts(state.selected);
    resolveCalls();
    renderCallUI();
    fetchWhales(state.selected);
  }, REFRESH_MS);
}
document.addEventListener("DOMContentLoaded", init);
