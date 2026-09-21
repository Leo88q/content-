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
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  const data = state.ohlcv;
  if (!data || data.length < 2) return;

  const pad = { t: 10, r: 60, b: 24, l: 8 };
  const cw = W - pad.l - pad.r, chh = (H - pad.t - pad.b) * 0.74, vh = (H - pad.t - pad.b) * 0.2;
  const hi = Math.max(...data.map((d) => d[2]));
  const lo = Math.min(...data.map((d) => d[3]));
  const span = hi - lo || hi * 0.01 || 1;
  const y = (price) => pad.t + chh - ((price - lo) / span) * chh;
  const bw = cw / data.length;

  // сетка
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.fillStyle = "rgba(255,255,255,0.45)";
  ctx.font = "10px monospace";
  for (let i = 0; i <= 4; i++) {
    const price = lo + (span * i) / 4;
    const yy = y(price);
    ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(pad.l + cw, yy); ctx.stroke();
    ctx.fillText(fmtUsd(price), pad.l + cw + 6, yy + 3);
  }

  // свечи
  const maxV = Math.max(...data.map((d) => d[5])) || 1;
  data.forEach((d, i) => {
    const [, o, h, l, c, v] = d;
    const x = pad.l + i * bw + bw / 2;
    const up = c >= o;
    ctx.strokeStyle = ctx.fillStyle = up ? "#26d07c" : "#ff4d6a";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, y(h)); ctx.lineTo(x, y(l)); ctx.stroke();
    const top = y(Math.max(o, c)), bot = y(Math.min(o, c));
    ctx.fillRect(x - bw * 0.32, top, bw * 0.64, Math.max(1, bot - top));
    // объём
    ctx.globalAlpha = 0.35;
    const vhh = (v / maxV) * vh;
    ctx.fillRect(x - bw * 0.32, pad.t + chh + 8 + (vh - vhh), bw * 0.64, vhh);
    ctx.globalAlpha = 1;
  });

  // линия последней цены
  const last = data[data.length - 1][4];
  ctx.strokeStyle = "rgba(124,240,61,0.5)";
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(pad.l, y(last)); ctx.lineTo(pad.l + cw, y(last)); ctx.stroke();
  ctx.setLineDash([]);
}
window.addEventListener("resize", drawChart);

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
  ctx.fillStyle = "#0a0e14"; ctx.fillRect(0, 0, W, H);
  ctx.strokeStyle = "#7cf03d"; ctx.lineWidth = 3; ctx.strokeRect(8, 8, W - 16, H - 16);

  ctx.fillStyle = "#fff"; ctx.font = "bold 72px sans-serif";
  ctx.fillText(p.base_symbol, 60, 110);
  ctx.fillStyle = p.change.h24 >= 0 ? "#26d07c" : "#ff4d6a";
  ctx.font = "bold 64px sans-serif";
  ctx.fillText(fmtPct(p.change.h24), 60, 190);
  ctx.fillStyle = "rgba(255,255,255,0.6)"; ctx.font = "28px sans-serif";
  ctx.fillText(`${p.name} · ${p.dex} · Vol ${fmtUsd(p.volume_h24)}`, 60, 240);

  // мини-график
  const data = state.ohlcv.slice(-48);
  if (data.length > 1) {
    const closes = data.map((d) => d[4]);
    const hi = Math.max(...closes), lo = Math.min(...closes), span = hi - lo || 1;
    ctx.strokeStyle = closes[closes.length - 1] >= closes[0] ? "#26d07c" : "#ff4d6a";
    ctx.lineWidth = 5; ctx.beginPath();
    closes.forEach((c, i) => {
      const x = 60 + (i / (closes.length - 1)) * (W - 120);
      const y = 500 - ((c - lo) / span) * 190;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
  }

  const { text } = narrative(p, state.lang);
  ctx.fillStyle = "#fff"; ctx.font = "30px sans-serif";
  wrapText(ctx, text.slice(0, 150), 60, 300, W - 120, 40);

  // водяной знак
  ctx.fillStyle = "#7cf03d"; ctx.font = "bold 30px sans-serif";
  ctx.fillText("📈 talkchart — графики, которые разговаривают", 60, H - 40);

  const a = document.createElement("a");
  a.download = `${p.base_symbol}-talkchart.png`;
  a.href = cv.toDataURL("image/png");
  a.click();
  track("share_card", p.address);
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
  $("#games").innerHTML = window.GAMES.map((g) => `
    <a class="game-card" href="${esc(g.url)}" target="_blank" rel="noopener" data-game="${g.id}" style="--accent:${g.accent}">
      <div class="game-name">${esc(g.name)}</div>
      <div class="game-tag">${esc(g.tagline)}</div>
      <span class="game-cta">${esc(g.cta)} →</span>
    </a>`).join("");
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
  cv.width = 320; cv.height = 240;
  const ctx = cv.getContext("2d");
  let score = 0, t0 = Date.now(), candles = [], done = false;
  const spawn = () => candles.push({ x: 20 + Math.random() * 280, y: -20, v: 1.5 + Math.random() * 2.5, green: Math.random() < 0.4 });
  cv.onclick = (e) => {
    const r = cv.getBoundingClientRect();
    const mx = (e.clientX - r.left) * (cv.width / r.width), my = (e.clientY - r.top) * (cv.height / r.height);
    candles.forEach((c, i) => {
      if (Math.abs(c.x - mx) < 16 && Math.abs(c.y - my) < 24) {
        if (c.green) { score += 10; candles.splice(i, 1); }
        else { score = Math.max(0, score - 5); candles.splice(i, 1); }
      }
    });
  };
  const loop = () => {
    const el = (Date.now() - t0) / 1000;
    if (el > 6 && !done) { done = true; $("#ist-score").textContent = `Счёт: ${score}. `; }
    if (done) { ctx.clearRect(0, 0, 320, 240); return; }
    if (Math.random() < 0.08) spawn();
    ctx.clearRect(0, 0, 320, 240);
    candles.forEach((c, i) => {
      c.y += c.v;
      ctx.fillStyle = c.green ? "#26d07c" : "#ff4d6a";
      ctx.fillRect(c.x - 4, c.y - 18, 8, 36);
      ctx.fillRect(c.x - 9, c.y - 8, 18, 16);
      if (c.y > 260) candles.splice(i, 1);
    });
    ctx.fillStyle = "#fff"; ctx.font = "bold 16px sans-serif";
    ctx.fillText(`ЛОВИ ЗЕЛЁНЫЕ СВЕЧИ · ${Math.max(0, 6 - Math.floor(el))}с · счёт ${score}`, 14, 24);
    requestAnimationFrame(loop);
  };
  loop();
}
function hideInterstitial() { $("#interstitial").classList.remove("show"); }

/* ---------- трекинг (заглушка пикселя → будущий wallet-CRM) ---------- */
function track(evt, id) {
  const rec = { evt, id, ts: Date.now(), pool: state.selected?.address };
  window.__SLOT_CLICKS__.push(rec);
  // TODO(prod): navigator.sendBeacon("/api/track", JSON.stringify(rec));
  console.info("[track]", rec);
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
  ctx.fillStyle = "#0a0e14"; ctx.fillRect(0, 0, W, H);
  ctx.strokeStyle = "#7cf03d"; ctx.lineWidth = 3; ctx.strokeRect(8, 8, W - 16, H - 16);
  ctx.fillStyle = "#fff"; ctx.font = "bold 64px sans-serif";
  ctx.fillText("Мои прогнозы на TalkChart", 60, 130);
  ctx.fillStyle = "#7cf03d"; ctx.font = "bold 120px sans-serif";
  ctx.fillText(`${Math.round((st.wins / st.n) * 100)}%`, 60, 300);
  ctx.fillStyle = "rgba(255,255,255,0.7)"; ctx.font = "34px sans-serif";
  ctx.fillText(`угаданных свечей: ${st.wins} из ${st.n} · лучшая серия: ${st.best}`, 60, 370);
  ctx.fillText("бумажные прогнозы, часовой таймфрейм, без денег — только скилл", 60, 420);
  ctx.fillStyle = "#7cf03d"; ctx.font = "bold 30px sans-serif";
  ctx.fillText("📈 talkchart — графики, которые разговаривают", 60, H - 40);
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

/* ---------- init ---------- */
async function init() {
  $("#lang-btn").textContent = state.lang === "ru" ? "EN" : "RU";
  $("#lang-btn").addEventListener("click", toggleLang);
  $("#share-btn").addEventListener("click", shareCard);
  $("#alert-btn").addEventListener("click", addAlert);
  $("#ist-close").addEventListener("click", hideInterstitial);
  $("#ist-skip").addEventListener("click", hideInterstitial);
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
