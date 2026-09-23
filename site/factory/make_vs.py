#!/usr/bin/env python3
"""Шаг 6 фабрики: генератор ончейн-баттлов «X vs Y» (SEO + GEO + карточки).

Сравнивает пары трендовых активов суток:
- Битва лидеров суток (#1 vs #2 по росту)
- Памп против объёма (#1 по росту vs гигант по объёму)
- Лидер против дампа (#1 гейнер vs #1 лузер)

Выход:
  site/vs/<sym1>-vs-<sym2>.html   — сравнительная страница с FAQ, таблицей и вердиктом
  site/vs/<sym1>-vs-<sym2>.md     — Markdown-двойник для Perplexity/ChatGPT
  site/vs/<sym1>-vs-<sym2>.json   — машиночитаемые метрики
  site/vs/latest.html             — главный баттл дня
  site/vs/index.html              — каталог баттлов
  site/data/vs_latest.json        — данные для виджета голосования в терминале
  site/cards/vs/<slug>.png        — 1200x630 PNG-баттл-карточка (Pillow)
"""
import html as html_lib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from narrative import fmt_pct, fmt_usd, headline, narrative

# Пиксель-оформление страниц баттлов: токены из style.css (тема: data-theme).
VS_CSS = """
body{background:var(--bg);background-image:repeating-conic-gradient(var(--bg2) 0% 25%,transparent 0% 50%) 0 0/8px 8px;color:var(--tx);font-family:var(--font-body);margin:0 auto;padding:40px 20px;max-width:840px;line-height:1.8;font-size:14px}
h1{font-family:var(--font-pixel);font-size:18px;line-height:1.5;margin:12px 0}
h2{font-family:var(--font-pixel);font-size:13px;line-height:1.6;margin:28px 0 12px}
h3{font-family:var(--font-pixel);font-size:11px;line-height:1.6}
.mut{color:var(--mut);font-size:12px}
a{color:var(--acc)}
code{background:var(--panel2);border:2px solid var(--line);padding:2px 6px;font-size:12px}
hr{border:none;border-top:3px solid var(--line);margin:30px 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:20px 0}
.box{background:var(--panel);border:3px solid var(--line);box-shadow:4px 4px 0 0 var(--shadow-color);padding:18px}
.box.token1{border-top:8px solid var(--green)} .box.token2{border-top:8px solid var(--info,#3dd9f0)}
.verdict{background:var(--panel);border:3px solid var(--line);border-left:8px solid var(--warn,#fba43a);box-shadow:4px 4px 0 0 var(--shadow-color);padding:16px;margin:20px 0}
.verdict h3{color:var(--warn,#fba43a)}
table{width:100%;border-collapse:collapse;margin:20px 0}
td,th{border:2px solid var(--line);padding:10px 14px;text-align:left;font-size:13px}
th{font-family:var(--font-pixel);font-size:8px;line-height:1.8;background:var(--panel2);color:var(--mut)}
a.cta{display:inline-block;background:var(--acc);color:#07110a;font-family:var(--font-pixel);font-size:9px;line-height:1.6;padding:12px 16px;border:3px solid var(--line);box-shadow:4px 4px 0 0 var(--shadow-color);text-decoration:none;margin:8px 8px 8px 0;transition:transform 120ms steps(3,end)}
a.cta:hover{transform:translate(-2px,-2px)}
img.card{width:100%;max-width:700px;border:3px solid var(--line);box-shadow:4px 4px 0 0 var(--shadow-color);display:block;margin:20px 0}
.px-toggle{font-family:var(--font-pixel);font-size:8px;line-height:1.6;background:var(--panel2);color:var(--tx);border:3px solid var(--line);box-shadow:4px 4px 0 0 var(--shadow-color);padding:8px 10px;cursor:pointer}
.px-toggle:hover{transform:translate(-2px,-2px);border-color:var(--acc)}
@media (prefers-reduced-motion: reduce){a.cta:hover,.px-toggle:hover{transform:none}}
"""

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False
if HAS_PILLOW:
    from pixelart import (ACC as ACC_VS, AMBER as AMBER_VS, BG as BG_VS,
                          CYAN as CYAN_VS, GREEN as GREEN_VS, INK as INK_VS,
                          MUT as MUT_VS, PANEL as PANEL_VS, RED as RED_VS,
                          WHITE as WHITE_VS, dither_bg, pixelate)


def load_font(size, bold=False):
    if not HAS_PILLOW:
        return None
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    path = os.path.join(config.FONTS_DIR, name)
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


CARD_W, CARD_H = 1200, 630


def render_vs_card(slug, p1, p2, out_path):
    """Пиксель-рендер баттл-карточки: низкое разрешение → NEAREST → палитра."""
    if not HAS_PILLOW:
        return None
    W, H = CARD_W, CARD_H
    ps = 5
    sw, sh = W // ps, H // ps              # 240 × 126
    img = Image.new("RGB", (sw, sh), BG_VS)
    draw = ImageDraw.Draw(img)
    dither_bg(draw, 0, 0, sw, sh, PANEL_VS, 4)

    f_sym = load_font(13, bold=True)
    f_pct = load_font(11, bold=True)
    f_sub = load_font(6, bold=False)
    f_vs = load_font(7, bold=True)
    f_mark = load_font(6, bold=False)

    s1, s2 = p1.get("base_symbol", "?"), p2.get("base_symbol", "?")
    c1 = (p1.get("change") or {}).get("h24") or 0
    c2 = (p2.get("change") or {}).get("h24") or 0
    col1 = GREEN_VS if c1 >= 0 else RED_VS
    col2 = GREEN_VS if c2 >= 0 else RED_VS

    draw.rectangle([0, 0, sw - 1, sh - 1], outline=ACC_VS)          # рамка
    draw.rectangle([4, 10, sw // 2 - 4, sh - 18], outline=col1)     # левый токен
    draw.rectangle([sw // 2 + 4, 10, sw - 5, sh - 18], outline=col2)  # правый токен
    draw.rectangle([4, 4, 44, 6], fill=ACC_VS)                      # «ушко» HUD

    # центральная плашка VS — прямоугольная, углы прямые
    bx0, bx1 = sw // 2 - 13, sw // 2 + 13
    draw.rectangle([bx0, 12, bx1, 24], fill=AMBER_VS)
    draw.text((bx0 + 4, 13), "VS", fill=INK_VS, font=f_vs)

    def column(x, token, p, chg, col):
        draw.text((x, 30), str(token)[:9], fill=WHITE_VS, font=f_sym)
        draw.text((x, 46), fmt_pct(chg), fill=col, font=f_pct)
        rows = [
            f"VOL {fmt_usd(p.get('volume_h24'))}",
            f"LIQ {fmt_usd(p.get('reserve_usd'))}",
            f"DEX {str(p.get('dex', '?'))[:12]}",
            f"FDV {fmt_usd(p.get('fdv_usd'))}",
        ]
        y = 62
        for r in rows:
            draw.text((x, y), r, fill=WHITE_VS if y < 78 else MUT_VS, font=f_sub)
            y += 8

    column(10, s1, p1, c1, col1)
    column(sw // 2 + 10, s2, p2, c2, col2)

    draw.text((10, sh - 12), str(config.CARD_MARK)[:40], fill=ACC_VS, font=f_mark)
    host = config.SITE_URL.replace("https://", "")[:22]
    draw.text((sw - 10 - int(draw.textlength(host, font=f_mark)), sh - 12), host,
              fill=MUT_VS, font=f_mark)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    pixelate(img, (W, H)).save(out_path, format="PNG", optimize=True)
    return out_path


def compute_verdict(p1, p2):
    s1 = p1.get("base_symbol", "Токен A")
    s2 = p2.get("base_symbol", "Токен B")
    c1 = (p1.get("change") or {}).get("h24") or 0
    c2 = (p2.get("change") or {}).get("h24") or 0
    v1 = p1.get("volume_h24") or 0
    v2 = p2.get("volume_h24") or 0
    r1 = max(1, p1.get("reserve_usd") or 1)
    r2 = max(1, p2.get("reserve_usd") or 1)
    vr1 = v1 / r1
    vr2 = v2 / r2

    reasons = []
    if c1 > c2:
        reasons.append(f"{s1} опережает по ценовому импульсу ({fmt_pct(c1)} против {fmt_pct(c2)})")
    else:
        reasons.append(f"{s2} лидирует по суточному росту ({fmt_pct(c2)} против {fmt_pct(c1)})")

    if r1 > r2 * 2:
        reasons.append(f"у {s1} существенно глубже пул ликвидности ({fmt_usd(r1)} против {fmt_usd(r2)}), что снижает риск проскальзывания")
    elif r2 > r1 * 2:
        reasons.append(f"у {s2} более надёжная ликвидность ({fmt_usd(r2)} против {fmt_usd(r1)})")

    if vr1 > 10 and vr2 <= 10:
        reasons.append(f"у {s1} объём превышает ликвидность в {vr1:.1f}× — признак экстремального хайпа и волатильности")
    elif vr2 > 10 and vr1 <= 10:
        reasons.append(f"у {s2} соотношение объёма к TVL {vr2:.1f}× указывает на повышенный спекулятивный накал")

    summary = ". Кроме того, ".join(reasons) + "."
    leader = s1 if (c1 > c2 and r1 >= r2 * 0.4) else s2
    return f"По ончейн-метрикам TalkChart: {summary} Краткосрочный фаворит по совокупности факторов: {leader}."


def main():
    if not os.path.exists(config.SNAPSHOT):
        print("make_vs.py: snapshot не найден", file=sys.stderr)
        return

    with open(config.SNAPSHOT, "r", encoding="utf-8") as f:
        snap = json.load(f)
    pools = snap.get("pools", [])
    if len(pools) < 2:
        print("make_vs.py: недостаточно пулов для баттлов", file=sys.stderr)
        return

    # Ранжируем
    gainers = sorted(pools, key=lambda p: (p.get("change") or {}).get("h24") or 0, reverse=True)
    by_vol = sorted(pools, key=lambda p: p.get("volume_h24") or 0, reverse=True)

    # 3 баттл-пары
    battles = [
        ("leaders", gainers[0], gainers[1], "Битва лидеров суток Solana"),
    ]
    if len(gainers) >= 3:
        battles.append(("momentum", gainers[0], gainers[2], "Столкновение мем-хайпа"))
    if by_vol and by_vol[0]["address"] != gainers[0]["address"]:
        battles.append(("volume-vs-pump", gainers[0], by_vol[0], "Взрывной памп против гиганта объёма"))

    vs_dir = config.VS_DIR
    os.makedirs(vs_dir, exist_ok=True)
    cards_vs_dir = os.path.join(config.CARDS_DIR, "vs")
    os.makedirs(cards_vs_dir, exist_ok=True)

    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    battle_links = []
    latest_battle_data = None

    for category, p1, p2, cat_title in battles:
        s1 = p1.get("base_symbol", "?")
        s2 = p2.get("base_symbol", "?")
        slug = f"{s1.lower()}-vs-{s2.lower()}"
        card_rel = f"cards/vs/{slug}.png"
        card_abs = os.path.join(config.SITE_DIR, card_rel)

        render_vs_card(slug, p1, p2, card_abs)

        verdict = compute_verdict(p1, p2)
        c1 = (p1.get("change") or {}).get("h24") or 0
        c2 = (p2.get("change") or {}).get("h24") or 0

        canonical = f"{config.SITE_URL}/vs/{slug}.html"
        canonical_md = f"{config.SITE_URL}/vs/{slug}.md"

        # Schema.org FAQPage для баттла
        faq_data = [
            {
                "q": f"Что лучше выбрать: {s1} или {s2} на Solana прямо сейчас?",
                "a": verdict,
            },
            {
                "q": f"У какого токена выше суточный объём и ликвидность: {s1} или {s2}?",
                "a": f"У {s1} суточный объём {fmt_usd(p1.get('volume_h24'))} при ликвидности {fmt_usd(p1.get('reserve_usd'))}. У {s2} объём {fmt_usd(p2.get('volume_h24'))} при ликвидности {fmt_usd(p2.get('reserve_usd'))}.",
            },
            {
                "q": f"Какая динамика курса за последние 24 часа?",
                "a": f"{s1} показал {fmt_pct(c1)} за сутки, в то время как {s2} изменился на {fmt_pct(c2)}.",
            },
        ]
        schema_json = json.dumps({
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": item["q"], "acceptedAnswer": {"@type": "Answer", "text": item["a"]}}
                for item in faq_data
            ]
        }, ensure_ascii=False)

        faq_items_html = "".join(f'<div class="faq-item"><h3>{item["q"]}</h3><p style="color:var(--mut);margin:6px 0">{item["a"]}</p></div>' for item in faq_data)
        faq_items_md = "\n\n".join(f"### Q: {item['q']}\n{item['a']}" for item in faq_data)

        html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{s1} против {s2} — сравнение токенов Solana DEX онлайн | TalkChart</title>
<meta name="description" content="Ончейн-сравнение {s1} ({fmt_pct(c1)}) и {s2} ({fmt_pct(c2)}): цена, ликвидность, объём, активность китов и вердикт алгоритма.">
<link rel="canonical" href="{canonical}">
<link rel="alternate" type="text/markdown" href="{canonical_md}">
<script type="application/ld+json">
{schema_json}
</script>
<link rel="stylesheet" href="../style.css">
<script src="../theme.js"></script>
<style>{VS_CSS}</style>
</head>
<body>
<div style="display:flex;justify-content:flex-end;margin-bottom:12px"><button class="px-toggle" data-theme-toggle title="Светлая тема" aria-label="Светлая тема" aria-pressed="false">☀️ ДЕНЬ</button></div>
<p class="mut"><a href="../index.html">📈 TalkChart</a> · <a href="index.html">каталог баттлов</a> · <a href="{canonical_md}">[md для AI]</a></p>
<h1>⚔️ {s1} vs {s2} — ончейн-сравнение ({cat_title})</h1>
<p class="mut">Срез данных: {now_str} · сеть Solana DEX</p>

<img class="card" src="../{card_rel}" alt="Карточка баттла {s1} против {s2}">

<section class="verdict">
<h3 style="margin-top:0;color:#fba43a">🧠 Вердикт алгоритма TalkChart</h3>
<p>{html_lib.escape(verdict)}</p>
</section>

<table>
<tr><th>Параметр</th><th>{s1}</th><th>{s2}</th><th>Лидер</th></tr>
<tr><td>Суточный рост (24ч)</td><td><b style="color:{'var(--green)' if c1 >= 0 else 'var(--red)'}">{fmt_pct(c1)}</b></td><td><b style="color:{'var(--green)' if c2 >= 0 else 'var(--red)'}">{fmt_pct(c2)}</b></td><td>{s1 if c1 > c2 else s2}</td></tr>
<tr><td>Объём 24ч</td><td>{fmt_usd(p1.get('volume_h24'))}</td><td>{fmt_usd(p2.get('volume_h24'))}</td><td>{s1 if (p1.get('volume_h24') or 0) > (p2.get('volume_h24') or 0) else s2}</td></tr>
<tr><td>Глубина пула (TVL)</td><td>{fmt_usd(p1.get('reserve_usd'))}</td><td>{fmt_usd(p2.get('reserve_usd'))}</td><td>{s1 if (p1.get('reserve_usd') or 0) > (p2.get('reserve_usd') or 0) else s2}</td></tr>
<tr><td>Отношение Объём / TVL</td><td>{((p1.get('volume_h24') or 0) / max(1, p1.get('reserve_usd') or 1)):.1f}×</td><td>{((p2.get('volume_h24') or 0) / max(1, p2.get('reserve_usd') or 1)):.1f}×</td><td>—</td></tr>
<tr><td>DEX биржа</td><td>{p1.get('dex', '?')}</td><td>{p2.get('dex', '?')}</td><td>—</td></tr>
<tr><td>Адрес пула</td><td><code>{p1['address'][:8]}…</code></td><td><code>{p2['address'][:8]}…</code></td><td>—</td></tr>
</table>

<h2>Часто задаваемые вопросы (FAQ)</h2>
{faq_items_html}

<div style="margin-top:24px">
<a class="cta" href="../index.html#pool={p1['address']}">Открыть чарт {s1} →</a>
<a class="cta" href="../index.html#pool={p2['address']}" style="background:#3dd9f0;color:#04222b">Открыть чарт {s2} →</a>
<a class="cta" href="../index.html#games" style="background:#fba43a;color:#1a1400">🎁 Забрать TipLink бонус в игры →</a>
</div>

<hr>
<p class="mut">Сгенерировано фабрикой контента TalkChart. Обновляется каждые 4 часа. Не является финансовой рекомендацией.</p>
</body>
</html>"""

        with open(os.path.join(vs_dir, f"{slug}.html"), "w", encoding="utf-8") as f:
            f.write(html)

        md = f"""# {s1} vs {s2} — Solana On-Chain Battle & Comparison

> Algorithmic Verdict: {verdict}

## Head-to-Head Metrics (Snapshot: {now_str})
| Metric | {s1} | {s2} | Edge |
|---|---|---|---|
| 24h Price Change | {fmt_pct(c1)} | {fmt_pct(c2)} | {s1 if c1 > c2 else s2} |
| 24h Trading Volume | {fmt_usd(p1.get('volume_h24'))} | {fmt_usd(p2.get('volume_h24'))} | {s1 if (p1.get('volume_h24') or 0) > (p2.get('volume_h24') or 0) else s2} |
| Total Liquidity | {fmt_usd(p1.get('reserve_usd'))} | {fmt_usd(p2.get('reserve_usd'))} | {s1 if (p1.get('reserve_usd') or 0) > (p2.get('reserve_usd') or 0) else s2} |
| DEX | {p1.get('dex', '?')} | {p2.get('dex', '?')} | - |
| Pool Address | `{p1['address']}` | `{p2['address']}` | - |

## Frequently Asked Questions
{faq_items_md}

---
*Source: TalkChart On-Chain Factory ({canonical})*
"""
        with open(os.path.join(vs_dir, f"{slug}.md"), "w", encoding="utf-8") as f:
            f.write(md)

        battle_links.append((slug, s1, s2, c1, c2, cat_title))

        if not latest_battle_data:
            latest_battle_data = {
                "slug": slug,
                "token1": {"symbol": s1, "change_24h": c1, "address": p1["address"], "volume": p1.get("volume_h24")},
                "token2": {"symbol": s2, "change_24h": c2, "address": p2["address"], "volume": p2.get("volume_h24")},
                "verdict": verdict,
                "updated_at": now_str,
            }

    # Каталог site/vs/index.html
    items_html = "\n".join(
        f'<li><a href="{slug}.html"><b>{s1} ({fmt_pct(c1)}) vs {s2} ({fmt_pct(c2)})</b></a> — {title} '
        f'<small><a href="{slug}.md" style="color:var(--mut)">[md]</a></small></li>'
        for slug, s1, s2, c1, c2, title in battle_links
    )
    catalog_html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Баттлы токенов Solana — сравнение пар активов | TalkChart</title>
<link rel="canonical" href="{config.SITE_URL}/vs/index.html">
<link rel="stylesheet" href="../style.css">
<script src="../theme.js"></script>
<style>{VS_CSS}</style>
</head>
<body>
<div style="display:flex;justify-content:flex-end;margin-bottom:12px"><button class="px-toggle" data-theme-toggle title="Светлая тема" aria-label="Светлая тема" aria-pressed="false">☀️ ДЕНЬ</button></div>
<p class="mut"><a href="../index.html">📈 TalkChart</a> / ончейн-баттлы токенов</p>
<h1>Ончейн-баттлы токенов Solana</h1>
<ul>{items_html}</ul>
<p class="mut">Обновляется автоматически каждые 4 часа через GeckoTerminal DEX tape.</p>
</body></html>"""

    with open(os.path.join(vs_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(catalog_html)

    # site/vs/latest.html -> редирект или копия главного баттла
    if battle_links:
        top_slug = battle_links[0][0]
        with open(os.path.join(vs_dir, f"{top_slug}.html"), "r", encoding="utf-8") as f:
            top_content = f.read()
        with open(os.path.join(vs_dir, "latest.html"), "w", encoding="utf-8") as f:
            f.write(top_content)

    # Сохраняем vs_latest.json для виджета терминала
    if latest_battle_data:
        data_vs_path = os.path.join(config.DATA_DIR, "vs_latest.json")
        with open(data_vs_path, "w", encoding="utf-8") as f:
            json.dump(latest_battle_data, f, ensure_ascii=False, indent=2)

    print(f"OK: создано {len(battles)} ончейн-баттлов + карточки + каталог -> site/vs/")


if __name__ == "__main__":
    main()
