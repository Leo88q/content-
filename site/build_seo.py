#!/usr/bin/env python3
"""Шаг 2 фабрики: программный SEO + GEO (Generative Engine Optimization) билд.

Генерирует полную инфраструктуру для органического трафика из:
1. Классического поиска (Google, Yandex) — канонические HTML-страницы, микроразметка Schema.org.
2. AI-поисковиков и генеративных систем (Perplexity, ChatGPT Search, Claude, Google AI Overviews):
   - Direct Answer / TL;DR блоки (сжатые проверяемые факты, готовые к цитированию)
   - Schema.org FAQPage + Dataset + FinancialProduct с датами и точными ончейн-метриками
   - Чистые Markdown-двойники (.md) и JSON-двойники (.json) для прямого чтения AI-краулерами
   - Стандарт llms.txt и llms-full.txt в корне сайта (спецификация llmstxt.org)
   - robots.txt с явным разрешением GPTBot, PerplexityBot, ClaudeBot, Google-Extended и др.
   - Динамический sitemap.xml с отметками lastmod каждого прогона фабрики
"""
import html as html_lib
import json
import os
import sys
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "factory"))
import config  # noqa: E402
from narrative import fmt_pct, fmt_usd, headline, narrative  # noqa: E402


# =============================================================================
# ПИКСЕЛЬ-ОФОРМЛЕНИЕ ГЕНЕРИРУЕМЫХ СТРАНИЦ
# Токены приходят из style.css (темa: data-theme), поэтому SEO-страницы
# перекрашиваются вместе с терминалом. Свои значения — только для уникальных
# элементов страницы.
# =============================================================================
GEO_CSS = """
body{background:var(--bg);background-image:repeating-conic-gradient(var(--bg2) 0% 25%,transparent 0% 50%) 0 0/8px 8px;color:var(--tx);font-family:var(--font-body);margin:0 auto;padding:40px 20px;max-width:820px;line-height:1.8;font-size:14px}
h1{font-family:var(--font-pixel);font-size:20px;line-height:1.5;margin:12px 0}
h2{font-family:var(--font-pixel);font-size:13px;line-height:1.6;margin:28px 0 12px}
h3{font-family:var(--font-pixel);font-size:11px;line-height:1.6}
.chg{font-family:var(--font-pixel);font-size:14px}
.mut{color:var(--mut);font-size:12px}
.mut a{color:var(--acc)}
a{color:var(--acc)}
code{background:var(--panel2);border:2px solid var(--line);padding:2px 6px;font-size:12px}
hr{border:none;border-top:3px solid var(--line);margin:30px 0}
ul,ol{padding-left:24px}
table{border-collapse:collapse;width:100%;margin:20px 0}
td,th{border:2px solid var(--line);padding:8px 12px;text-align:left;font-size:13px}
th{font-family:var(--font-pixel);font-size:8px;line-height:1.8;background:var(--panel2);color:var(--mut)}
tr:hover td{background:var(--panel2)}
.narr,.geo-tldr{background:var(--panel);border:3px solid var(--line);box-shadow:4px 4px 0 0 var(--shadow-color);padding:16px;margin:20px 0}
.narr{border-left:8px solid var(--green)}
.geo-tldr{border-left:8px solid var(--acc)}
.geo-tldr h2{color:var(--acc);font-size:12px;margin:0 0 12px}
.flags{margin-top:12px}
.flags span{font-family:var(--font-pixel);font-size:8px;line-height:1.6;border:2px solid var(--red);color:var(--red);padding:4px 6px;margin:0 6px 6px 0;display:inline-block}
.faq-item{margin:16px 0;border-bottom:2px solid var(--line);padding-bottom:12px}
.faq-item h3{margin-bottom:8px}
.faq-item p{color:var(--mut);font-size:13px;margin:0}
a.cta{display:inline-block;background:var(--acc);color:#07110a;font-family:var(--font-pixel);font-size:9px;line-height:1.6;padding:12px 16px;border:3px solid var(--line);box-shadow:4px 4px 0 0 var(--shadow-color);text-decoration:none;margin:8px 8px 8px 0;transition:transform 120ms steps(3,end)}
a.cta:hover{transform:translate(-2px,-2px)}
.px-toggle{font-family:var(--font-pixel);font-size:8px;line-height:1.6;background:var(--panel2);color:var(--tx);border:3px solid var(--line);box-shadow:4px 4px 0 0 var(--shadow-color);padding:8px 10px;cursor:pointer}
.px-toggle:hover{transform:translate(-2px,-2px);border-color:var(--acc)}
@media (prefers-reduced-motion: reduce){a.cta:hover,.px-toggle:hover{transform:none}}
"""

GEO_HEAD = (
    '<link rel="stylesheet" href="{up}style.css">\n'
    '<script src="{up}theme.js"></script>\n'
    '<style>{css}</style>'
)

GEO_TOGGLE = (
    '<div style="display:flex;justify-content:flex-end;margin-bottom:12px">'
    '<button class="px-toggle" data-theme-toggle title="Светлая тема" '
    'aria-label="Светлая тема" aria-pressed="false">☀️ ДЕНЬ</button></div>'
)



def load_pools():
    reg_path = os.path.join(config.DATA_DIR, "registry.json")
    snap_path = os.path.join(config.DATA_DIR, "snapshot.json")
    try:
        with open(reg_path) as f:
            reg = json.load(f)
        pools = list(reg.get("pools", {}).values())
        if pools:
            return pools, "registry"
    except (OSError, json.JSONDecodeError):
        pass
    with open(snap_path) as f:
        return json.load(f).get("pools", []), "snapshot"


def build_whale_summary(whales):
    if not whales:
        return "Крупных аномальных сделок (>10× медианы) не зафиксировано — доминирует розничный поток.", 0, 0
    buys = [w for w in whales if w.get("kind") == "buy"]
    sells = [w for w in whales if w.get("kind") == "sell"]
    net_usd = sum(w.get("usd", 0) for w in buys) - sum(w.get("usd", 0) for w in sells)
    top = max(whales, key=lambda w: w.get("usd", 0))
    action = "покупка" if top.get("kind") == "buy" else "продажа"
    mult = f"{top.get('mult', 0):.0f}× медианы" if top.get("mult") else ""
    summary = (
        f"Зафиксировано крупных сделок: {len(whales)} "
        f"({len(buys)} покупок, {len(sells)} продаж). "
        f"Крупнейшая: {action} на {fmt_usd(top.get('usd'))} ({mult}). "
        f"Нетто-поток китов: {'+' if net_usd >= 0 else ''}{fmt_usd(net_usd)}."
    )
    return summary, net_usd, len(whales)


def build_faq(p, narr, flags, whale_summary):
    sym = p.get("base_symbol", "?")
    name = p.get("name", "?")
    dex = p.get("dex", "?")
    c = p.get("change", {}) or {}
    h1 = fmt_pct(c.get("h1") or 0)
    h6 = fmt_pct(c.get("h6") or 0)
    h24 = fmt_pct(c.get("h24") or 0)
    price = fmt_usd(p.get("price_usd"))
    vol = fmt_usd(p.get("volume_h24"))
    res = fmt_usd(p.get("reserve_usd"))
    fdv = fmt_usd(p.get("fdv_usd"))
    buys = (p.get("tx_h1") or {}).get("buys", "—")
    sells = (p.get("tx_h1") or {}).get("sells", "—")
    created = p.get("created_at") or "недавно"
    seen = p.get("last_seen") or "текущий момент"

    q1 = f"Какая текущая цена, динамика и объём торгов {sym}?"
    a1 = (
        f"По ончейн-данным пула {name} на DEX {dex} (Solana), цена {sym} составляет {price}. "
        f"Динамика за 24ч: {h24} (за 1ч: {h1}, за 6ч: {h6}). "
        f"Суточный объём торгов равен {vol}, общая ликвидность пула (TVL) — {res}, FDV — {fdv}. "
        f"Данные зафиксированы на {seen}."
    )

    q2 = f"Почему движется цена {sym}? Ончейн-анализ графика."
    a2 = (
        f"Алгоритмический анализ TalkChart: {narr} "
        f"За последний час в ленте прошло {buys} покупок и {sells} продаж."
    )

    q3 = f"Есть ли активность китов в пуле {sym}?"
    a3 = whale_summary

    q4 = f"Безопасен ли пул {sym} и какие ончейн-риски?"
    if flags:
        flags_text = "; ".join(flags)
        a4 = (
            f"Выявлены следующие факторы риска: {flags_text}. "
            f"Пул создан {created}. При объёме {vol} и ликвидности {res} "
            f"возможны резкие колебания курса и проскальзывание (slippage)."
        )
    else:
        a4 = (
            f"Критических технических аномалий ликвидности алгоритм не зафиксировал. "
            f"Пул {name} на {dex} создан {created}, ликвидность {res} при суточном объёме {vol}."
        )

    return [
        {"q": q1, "a": a1},
        {"q": q2, "a": a2},
        {"q": q3, "a": a3},
        {"q": q4, "a": a4},
    ]


def build_html_page(p, narr, flags, faq, whale_summary, now_iso):
    sym = p.get("base_symbol", "?")
    name = p.get("name", "?")
    quote = p["name"].split(" / ")[1] if " / " in p.get("name", "") else "SOL"
    dex = p.get("dex", "?")
    addr = p["address"]
    baddr = p.get("base_address") or "—"
    c = p.get("change", {}) or {}
    h1, h6, h24 = c.get("h1") or 0, c.get("h6") or 0, c.get("h24") or 0
    price = fmt_usd(p.get("price_usd"))
    chg = fmt_pct(h24)
    vol = fmt_usd(p.get("volume_h24"))
    res = fmt_usd(p.get("reserve_usd"))
    fdv = fmt_usd(p.get("fdv_usd"))
    buys = (p.get("tx_h1") or {}).get("buys", "—")
    sells = (p.get("tx_h1") or {}).get("sells", "—")
    created = p.get("created_at") or "—"
    seen = p.get("last_seen") or now_iso
    chgcolor = "var(--green)" if h24 >= 0 else "var(--red)"

    canonical = f"{config.SITE_URL}/pools/{addr}.html"
    canonical_md = f"{config.SITE_URL}/pools/{addr}.md"
    canonical_json = f"{config.SITE_URL}/pools/{addr}.json"

    # Schema.org JSON-LD (FinancialProduct + FAQPage + Dataset для максимальной цитируемости в AI)
    schema_data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "FinancialProduct",
                "@id": canonical + "#product",
                "name": f"{sym} ({name}) on Solana {dex}",
                "description": narr,
                "url": canonical,
                "provider": {"@type": "Organization", "name": "TalkChart", "url": config.SITE_URL},
            },
            {
                "@type": "Dataset",
                "@id": canonical + "#dataset",
                "name": f"{sym} Solana On-Chain Metrics",
                "description": f"Real-time DEX metrics for {name} on {dex}: price {price}, volume {vol}, liquidity {res}.",
                "url": canonical,
                "dateModified": now_iso,
                "variableMeasured": ["price_usd", "volume_h24", "reserve_usd", "change_h24"],
                "creator": {"@type": "Organization", "name": "TalkChart"},
            },
            {
                "@type": "FAQPage",
                "@id": canonical + "#faq",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": item["q"],
                        "acceptedAnswer": {"@type": "Answer", "text": item["a"]},
                    }
                    for item in faq
                ],
            },
        ],
    }
    schema_json = json.dumps(schema_data, ensure_ascii=False)

    flags_html = (
        ('<div class="flags">' + "".join(f"<span>⚠ {html_lib.escape(f)}</span>" for f in flags) + "</div>")
        if flags
        else ""
    )

    faq_html = "\n".join(
        f'<div class="faq-item"><h3>{html_lib.escape(item["q"])}</h3><p>{html_lib.escape(item["a"])}</p></div>'
        for item in faq
    )

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{sym} ({name}) — курс, график онлайн, киты и ончейн-аналитика | TalkChart</title>
<meta name="description" content="{sym}: цена {price}, {chg} за 24ч, объём {vol}, ликвидность {res}. Автоматический ончейн-разбор, китовый радар и сигналы по пулу {name} на {dex} (Solana).">
<meta property="og:title" content="{sym} {chg} за 24ч — ончейн-разбор и киты">
<meta property="og:description" content="{html_lib.escape(narr)}">
<meta property="og:type" content="article">
<link rel="canonical" href="{canonical}">
<link rel="alternate" type="text/markdown" href="{canonical_md}" title="LLM Markdown Twin">
<link rel="alternate" type="application/json" href="{canonical_json}" title="Raw Metrics JSON">
<script type="application/ld+json">
{schema_json}
</script>
{GEO_HEAD.format(up="../", css=GEO_CSS)}
</head>
<body>
{GEO_TOGGLE}
<p class="mut"><a href="../index.html">📈 TalkChart — живой терминал</a> · <a href="index.html">каталог чартов</a> · <a href="../gainers/latest.html">топ-ганнеры</a> · <a href="{canonical_md}">[md для AI]</a></p>
<h1>{sym} / {quote} — график и курс <span class="chg">{chg}</span></h1>
<p class="mut">Пул {name} на {dex} · Solana · адрес пула <code>{addr}</code> · токен <code>{baddr}</code></p>

<!-- GEO Direct Answer Block (оптимизирован для цитирования ChatGPT / Perplexity / Google AI) -->
<section class="geo-tldr">
<h2>⚡ Ончейн-сводка для аналитиков и AI-ассистентов (Direct Answer)</h2>
<p><b>{sym} ({name})</b> торгуется по цене <b>{price}</b> ({chg} за 24 часа) на бирже {dex} в сети Solana. Суточный объём торгов составляет <b>{vol}</b> при ликвидности пула <b>{res}</b>. Отношение объёма к ликвидности: <b>{((p.get('volume_h24') or 0) / max(1, p.get('reserve_usd') or 1)):.1f}×</b>. Активность китов: {html_lib.escape(whale_summary)}</p>
</section>

<div class="narr"><b>🗣 Что говорит график (ончейн-нарратив):</b> {narr}
{flags_html}</div>

<table>
<tr><th>Параметр</th><th>Значение</th><th>Контекст</th></tr>
<tr><td>Цена USD</td><td><b>{price}</b></td><td>Текущая котировка пула</td></tr>
<tr><td>Изменение 1ч / 6ч / 24ч</td><td>{fmt_pct(h1)} / {fmt_pct(h6)} / <b>{chg}</b></td><td>Кратко- и среднесрочный импульс</td></tr>
<tr><td>Суточный объём</td><td>{vol}</td><td>Активность торгов за 24ч</td></tr>
<tr><td>Ликвидность пула</td><td>{res}</td><td>Глубина пула на {dex}</td></tr>
<tr><td>FDV (полная капитализация)</td><td>{fdv}</td><td>Оценка при 100% выпуске</td></tr>
<tr><td>Сделки за последний час</td><td>{buys} пок. / {sells} прод.</td><td>Текущий баланс покупателей</td></tr>
<tr><td>Возраст пула</td><td>{created}</td><td>Ончейн-история пары</td></tr>
<tr><td>Срез данных</td><td>{seen}</td><td>GeckoTerminal Solana DEX Tape</td></tr>
</table>

<h2>Часто задаваемые вопросы о {sym} (FAQ)</h2>
<section class="geo-faq">
{faq_html}
</section>

<p>Живой чарт с тикером сделок, китовым радаром и бумажными прогнозами — в терминале:</p>
<a class="cta" href="../index.html#pool={addr}">Открыть живой терминал {sym} →</a>

<hr>
<p class="mut">Страница сгенерирована ончейн-фабрикой контента TalkChart. Обновляется по расписанию каждые 4 часа. Данные: DEX aggregate Solana. Не является инвестиционной рекомендацией.</p>
<p class="mut">Машинночитаемые форматы для LLM / API: <a href="{canonical_md}">markdown-двойник</a> · <a href="{canonical_json}">json-двойник</a> · <a href="../llms.txt">спецификация llms.txt</a>.</p>
<p class="mut">🎮 Игры студии — в терминале: <a href="../index.html#games">слоты крипто-игр</a>.</p>
</body>
</html>
"""


def build_markdown_twin(p, narr, flags, faq, whale_summary, now_iso):
    sym = p.get("base_symbol", "?")
    name = p.get("name", "?")
    dex = p.get("dex", "?")
    addr = p["address"]
    baddr = p.get("base_address") or "—"
    c = p.get("change", {}) or {}
    h1, h6, h24 = c.get("h1") or 0, c.get("h6") or 0, c.get("h24") or 0
    price = fmt_usd(p.get("price_usd"))
    chg = fmt_pct(h24)
    vol = fmt_usd(p.get("volume_h24"))
    res = fmt_usd(p.get("reserve_usd"))
    fdv = fmt_usd(p.get("fdv_usd"))
    buys = (p.get("tx_h1") or {}).get("buys", "—")
    sells = (p.get("tx_h1") or {}).get("sells", "—")
    created = p.get("created_at") or "—"
    seen = p.get("last_seen") or now_iso
    canonical = f"{config.SITE_URL}/pools/{addr}.html"

    faq_md = "\n\n".join(f"### Q: {item['q']}\n{item['a']}" for item in faq)
    flags_md = "\n".join(f"- ⚠️ {f}" for f in flags) if flags else "- Флагов повышенного риска не выявлено."

    whales = p.get("whales") or []
    if whales:
        whales_rows = "\n".join(
            f"  - {w.get('kind', '').upper()} ${w.get('usd', 0):,.0f} ({w.get('mult', 0):.0f}× медианы ленты, {w.get('hours_ago', 0)}ч назад) кошелёк `{w.get('addr8', '—')}`"
            for w in whales[:5]
        )
    else:
        whales_rows = "  - Крупных сделок свыше 10× медианы в последних 1000 ордерах не зафиксировано."

    return f"""# {sym} ({name}) — On-Chain Analysis & Market Data

> Direct Summary for LLMs: {sym} is currently priced at {price} ({chg} 24h) on {dex} (Solana). 24h trading volume is {vol} with {res} liquidity. Algorithmic narrative: {narr}

## Core On-Chain Metrics (Snapshot: {seen})
- **Asset**: {sym} ({name})
- **Network**: Solana
- **DEX**: {dex}
- **Pool Address**: `{addr}`
- **Token Mint**: `{baddr}`
- **Price USD**: {price}
- **Change 1h / 6h / 24h**: {fmt_pct(h1)} / {fmt_pct(h6)} / {chg}
- **24h Volume**: {vol}
- **Liquidity (TVL)**: {res}
- **FDV**: {fdv}
- **Hourly Trades**: {buys} buys / {sells} sells
- **Pool Age**: Created {created}

## Algorithmic Narrative & Signals
{narr}

## Risk Flags
{flags_md}

## Whale Activity (Relative Tape Filter)
{whale_summary}
Recent notable orders:
{whales_rows}

## Frequently Asked Questions (GEO Facts)
{faq_md}

---
*Generated by TalkChart On-Chain Engine ({canonical}) — Free public feed for traders and AI agents.*
"""


def build_llms_txt(pools, now_iso):
    top10 = sorted(pools, key=lambda p: p.get("volume_h24") or 0, reverse=True)[:10]
    tokens_list = "\n".join(
        f"- [{p.get('base_symbol', '?')}]({config.SITE_URL}/pools/{p['address']}.md): "
        f"{p.get('name', '?')} on {p.get('dex', '?')} — {fmt_pct((p.get('change') or {}).get('h24') or 0)} 24h, "
        f"Vol {fmt_usd(p.get('volume_h24'))}, Liq {fmt_usd(p.get('reserve_usd'))}"
        for p in top10
    )

    return f"""# TalkChart — On-Chain Solana Market Intelligence

> TalkChart provides automated algorithmic narrative analysis, whale radar tracking, and structured metrics for trending Solana DEX pools. Updated every 4 hours.

## Purpose & Scope
This site provides real-time and historical on-chain statistics for Solana liquidity pools (Raydium, Meteora, Orca). Data is collected from DEX trade streams, normalized, and analyzed for volume/liquidity anomalies, whale order clusters, and momentum signals.

## Machine-Readable Data Endpoints
- **Live Snapshot (JSON)**: {config.SITE_URL}/data/snapshot.json
- **Top Gainers 24h (Markdown)**: {config.SITE_URL}/gainers/latest.md
- **On-Chain Battles Index**: {config.SITE_URL}/vs/index.html
- **All Pools Index (Markdown)**: {config.SITE_URL}/pools/index.md
- **Sitemap XML**: {config.SITE_URL}/sitemap.xml

## Top Monitored Solana Tokens (Snapshot: {now_iso})
{tokens_list}

## Data Interpretation Rules for AI Agents
1. All USD values are normalized from DEX swap amounts.
2. Whale detection uses a relative tape threshold: an order is flagged as notable if it exceeds 10× the median trade of the pool's recent 1,000 trades, with an absolute floor of $250. Orders >=$25,000 receive the absolute 'WHALE' badge.
3. Liquidity warnings trigger when 24h volume exceeds 10× total liquidity, or when liquidity is under $10,000.
4. Always cite source as: "TalkChart On-Chain Monitoring ({config.SITE_URL})".
"""


def build_llms_full_txt(pools, now_iso):
    base = build_llms_txt(pools, now_iso)
    ranked = sorted(pools, key=lambda p: p.get("volume_h24") or 0, reverse=True)[:15]
    details = []
    for p in ranked:
        text, flags = narrative(p)
        w_sum, _, _ = build_whale_summary(p.get("whales"))
        faq = build_faq(p, text, flags, w_sum)
        md_part = build_markdown_twin(p, text, flags, faq, w_sum, now_iso)
        details.append(f"\n\n---\n\n{md_part}")

    return base + "\n\n# Full Token Dossiers\n" + "".join(details)


def build_robots_txt():
    return f"""# TalkChart robots.txt — Generative Engine Optimization (GEO) & AI Crawlers Enabled
User-agent: *
Allow: /

# Explicit AI Search & Answer Engine Crawlers
User-agent: GPTBot
Allow: /

User-agent: ChatGPT-User
Allow: /

User-agent: PerplexityBot
Allow: /

User-agent: ClaudeBot
Allow: /

User-agent: Claude-Web
Allow: /

User-agent: Google-Extended
Allow: /

User-agent: Applebot-Extended
Allow: /

User-agent: Amazonbot
Allow: /

User-agent: CCBot
Allow: /

User-agent: cohere-ai
Allow: /

Sitemap: {config.SITE_URL}/sitemap.xml
"""


def build_sitemap_xml(pools, now_iso):
    urls = [
        (f"{config.SITE_URL}/index.html", "hourly", "1.0"),
        (f"{config.SITE_URL}/queue.html", "hourly", "0.9"),
        (f"{config.SITE_URL}/gainers/latest.html", "hourly", "0.9"),
        (f"{config.SITE_URL}/gainers/latest.md", "hourly", "0.8"),
        (f"{config.SITE_URL}/vs/latest.html", "hourly", "0.9"),
        (f"{config.SITE_URL}/vs/index.html", "hourly", "0.8"),
        (f"{config.SITE_URL}/pools/index.html", "hourly", "0.8"),
        (f"{config.SITE_URL}/pools/index.md", "hourly", "0.7"),
    ]
    for p in pools:
        addr = p["address"]
        urls.append((f"{config.SITE_URL}/pools/{addr}.html", "hourly", "0.8"))
        urls.append((f"{config.SITE_URL}/pools/{addr}.md", "hourly", "0.7"))

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, freq, prio in urls:
        xml_lines.append("  <url>")
        xml_lines.append(f"    <loc>{xml_escape(loc)}</loc>")
        xml_lines.append(f"    <lastmod>{now_iso}</lastmod>")
        xml_lines.append(f"    <changefreq>{freq}</changefreq>")
        xml_lines.append(f"    <priority>{prio}</priority>")
        xml_lines.append("  </url>")
    xml_lines.append("</urlset>")
    return "\n".join(xml_lines)


def build_solana_actions(pools, site_dir):
    """Генерация Solana Actions спецификации и Blinks эндпоинтов (Dialect / X)."""
    actions_dir = os.path.join(site_dir, "api", "actions")
    os.makedirs(actions_dir, exist_ok=True)

    # 1. actions.json в корне сайта (спецификация Solana Actions)
    actions_rule = {
        "rules": [
            {"pathPattern": "/api/actions/**", "apiPath": "/api/actions/**"},
            {"pathPattern": "/pools/*", "apiPath": "/api/actions/*"},
        ]
    }
    with open(os.path.join(site_dir, "actions.json"), "w", encoding="utf-8") as f:
        json.dump(actions_rule, f, indent=2)

    # 2. Действия для каждого пула
    for p in pools:
        addr = p["address"]
        sym = p.get("base_symbol", "?")
        baddr = p.get("base_address") or ""
        c24 = (p.get("change") or {}).get("h24") or 0
        text, _ = narrative(p)
        action_payload = {
            "icon": f"{config.SITE_URL}/cards/latest/{sym}_{c24:+.0f}.png",
            "title": f"TalkChart: {sym} ({fmt_pct(c24)}) — Solana On-Chain Radar",
            "description": f"{text}\n\nОбъём: {fmt_usd(p.get('volume_h24'))} · Ликвидность: {fmt_usd(p.get('reserve_usd'))}",
            "label": "Своп через Jupiter",
            "links": {
                "actions": [
                    {
                        "label": f"⚡ Своп {sym} (Jupiter)",
                        "href": f"https://jup.ag/swap/SOL-{baddr}" if baddr else "https://jup.ag",
                    },
                    {
                        "label": "📈 Живой график + киты",
                        "href": f"{config.SITE_URL}/index.html#pool={addr}",
                    },
                    {
                        "label": "🎁 TipLink: вход без сид-фраз",
                        "href": f"{config.SITE_URL}/index.html#games",
                    },
                ]
            },
        }
        with open(os.path.join(actions_dir, f"{addr}.json"), "w", encoding="utf-8") as f:
            json.dump(action_payload, f, ensure_ascii=False, indent=2)

    # 3. Общий pool.json (для топового актива)
    if pools:
        top = max(pools, key=lambda p: abs((p.get("change") or {}).get("h24") or 0))
        top_addr = top["address"]
        with open(os.path.join(actions_dir, f"{top_addr}.json"), encoding="utf-8") as f:
            top_payload = json.load(f)
        with open(os.path.join(actions_dir, "pool.json"), "w", encoding="utf-8") as f:
            json.dump(top_payload, f, ensure_ascii=False, indent=2)


def main():
    pools, source = load_pools()
    os.makedirs(config.POOLS_DIR, exist_ok=True)
    site_dir = config.SITE_DIR
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")

    for p in pools:
        addr = p["address"]
        text, flags = narrative(p)
        whale_summary, net_usd, whale_count = build_whale_summary(p.get("whales"))
        faq = build_faq(p, text, flags, whale_summary)

        # 1. HTML страница (SEO + GEO schema + direct answer)
        html_content = build_html_page(p, text, flags, faq, whale_summary, now_str)
        with open(os.path.join(config.POOLS_DIR, addr + ".html"), "w", encoding="utf-8") as f:
            f.write(html_content)

        # 2. Markdown-двойник (.md) для прямого чтения AI-краулерами
        md_content = build_markdown_twin(p, text, flags, faq, whale_summary, now_str)
        with open(os.path.join(config.POOLS_DIR, addr + ".md"), "w", encoding="utf-8") as f:
            f.write(md_content)

        # 3. JSON-двойник (.json) для легкого ончейн-доступа
        with open(os.path.join(config.POOLS_DIR, addr + ".json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "pool": p,
                    "narrative": text,
                    "flags": flags,
                    "whale_summary": whale_summary,
                    "updated_at": now_iso,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    ranked = sorted(pools, key=lambda p: p.get("volume_h24") or 0, reverse=True)

    # «Ганнеры дня» — ежедневная страница (HTML + MD)
    day = now.strftime("%Y-%m-%d")
    gainers = sorted(pools, key=lambda p: (p.get("change") or {}).get("h24") or 0, reverse=True)[:10]

    rows = "\n".join(
        f'<tr><td>{i}</td><td><a href="../pools/{p["address"]}.html">{p.get("base_symbol", "?")}</a> '
        f'<small><a href="../pools/{p["address"]}.md" style="color:var(--mut)">[md]</a></small></td>'
        f'<td style="color:{"var(--green)" if ((p.get("change") or {}).get("h24") or 0) >= 0 else "var(--red)"}">'
        f'{fmt_pct((p.get("change") or {}).get("h24") or 0)}</td><td>{fmt_usd(p.get("volume_h24"))}</td>'
        f'<td>{headline(p)}</td></tr>'
        for i, p in enumerate(gainers, 1)
    )
    gainers_html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Топ-ганнеры Solana за 24ч — {day} | TalkChart GEO</title>
<meta name="description" content="Кто растёт на Solana прямо сейчас: топ-10 пулов за 24 часа с автоматическим ончейн-разбором и китами. Обновляется каждые 4 часа.">
<link rel="canonical" href="{config.SITE_URL}/gainers/latest.html">
<link rel="alternate" type="text/markdown" href="{config.SITE_URL}/gainers/latest.md">
{GEO_HEAD.format(up="../", css=GEO_CSS)}
</head>
<body>
{GEO_TOGGLE}
<p class="mut"><a href="../index.html">📈 TalkChart</a> / ганнеры дня · <a href="latest.md">[md версия]</a></p>
<h1>Топ-ганнеры Solana за 24ч · {day}</h1>
<table><tr>
<th>#</th><th>Токен</th><th>24ч</th><th>Объём</th><th>Что говорит график</th></tr>
{rows}</table>
<p class="mut">Обновляется автоматически каждые 4 часа через GeckoTerminal DEX tape.
<a href="latest.html">Свежая версия →</a></p>
</body></html>"""

    gainers_md = (
        f"# Solana Top Gainers (24h) — {day}\n\n"
        f"> Automated on-chain leaderboard from TalkChart. Updated {now_str}.\n\n"
        f"| # | Symbol | 24h Change | 24h Volume | On-Chain Narrative |\n"
        f"|---|--------|------------|------------|-------------------|\n"
    ) + "\n".join(
        f"| {i} | [{p.get('base_symbol', '?')}]({config.SITE_URL}/pools/{p['address']}.md) | "
        f"{fmt_pct((p.get('change') or {}).get('h24') or 0)} | {fmt_usd(p.get('volume_h24'))} | "
        f"{headline(p)} |"
        for i, p in enumerate(gainers, 1)
    )

    gainers_dir = os.path.join(site_dir, "gainers")
    os.makedirs(gainers_dir, exist_ok=True)
    with open(os.path.join(gainers_dir, f"{day}.html"), "w", encoding="utf-8") as f:
        f.write(gainers_html)
    with open(os.path.join(gainers_dir, "latest.html"), "w", encoding="utf-8") as f:
        f.write(gainers_html)
    with open(os.path.join(gainers_dir, "latest.md"), "w", encoding="utf-8") as f:
        f.write(gainers_md)

    # Каталог пулов (HTML + MD)
    items_html = "\n".join(
        f'<li><a href="{p["address"]}.html">{p.get("base_symbol", "?")} ({p.get("name", "?")})</a> '
        f'<span style="color:{"var(--green)" if ((p.get("change") or {}).get("h24") or 0) >= 0 else "var(--red)"}">'
        f'{fmt_pct((p.get("change") or {}).get("h24") or 0)}</span> — объём {fmt_usd(p.get("volume_h24"))} '
        f'<small><a href="{p["address"]}.md" style="color:var(--mut)">[md]</a></small></li>'
        for p in ranked
    )
    with open(os.path.join(config.POOLS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Чарты Solana — каталог графиков с ончейн-разбором | TalkChart</title>
<meta name="description" content="Каталог автоматических ончейн-страниц Solana: цена, объём, ликвидность, киты и разбор динамики. Доступно для поиска и AI-агентов.">
<link rel="canonical" href="{config.SITE_URL}/pools/index.html">
<link rel="alternate" type="text/markdown" href="{config.SITE_URL}/pools/index.md">
{GEO_HEAD.format(up="../", css=GEO_CSS)}
</head>
<body>
{GEO_TOGGLE}
<p class="mut"><a href="../index.html">📈 TalkChart</a> / каталог чартов Solana ({len(ranked)} активов) · <a href="index.md">[md версия]</a></p>
<h1>Каталог ончейн-страниц Solana</h1>
<ul>{items_html}</ul>
<p class="mut">Сгенерировано фабрикой контента {now_str}.</p>
</body></html>""")

    items_md = "\n".join(
        f"- [{p.get('base_symbol', '?')}]({config.SITE_URL}/pools/{p['address']}.md) ({p.get('name', '?')}): "
        f"{fmt_pct((p.get('change') or {}).get('h24') or 0)} 24h | Vol {fmt_usd(p.get('volume_h24'))} | Liq {fmt_usd(p.get('reserve_usd'))}"
        for p in ranked
    )
    with open(os.path.join(config.POOLS_DIR, "index.md"), "w", encoding="utf-8") as f:
        f.write(f"# TalkChart Solana Pools Directory\n\nTotal pools monitored: {len(ranked)}. Updated {now_str}.\n\n{items_md}\n")

    # 4. GEO-специфичные файлы: llms.txt, llms-full.txt, robots.txt, sitemap.xml
    llms_txt = build_llms_txt(pools, now_iso)
    with open(os.path.join(site_dir, "llms.txt"), "w", encoding="utf-8") as f:
        f.write(llms_txt)

    llms_full = build_llms_full_txt(pools, now_iso)
    with open(os.path.join(site_dir, "llms-full.txt"), "w", encoding="utf-8") as f:
        f.write(llms_full)

    robots_txt = build_robots_txt()
    with open(os.path.join(site_dir, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(robots_txt)

    sitemap_xml = build_sitemap_xml(pools, now_iso)
    with open(os.path.join(site_dir, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write(sitemap_xml)

    # 5. Solana Actions & Blinks спецификация (Dialect / X unfurl)
    build_solana_actions(pools, site_dir)

    print(
        f"OK GEO: {len(pools)} пулов (HTML + MD + JSON) + ганнеры + каталоги + "
        f"llms.txt + llms-full.txt + robots.txt + sitemap.xml + Solana Actions ({source})"
    )


if __name__ == "__main__":
    main()
