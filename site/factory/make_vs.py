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

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


def load_font(size, bold=False):
    if not HAS_PILLOW:
        return None
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    path = os.path.join(config.FONTS_DIR, name)
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def render_vs_card(slug, p1, p2, out_path):
    """Рендер 1200x630 баттл-карточки (Pillow)."""
    if not HAS_PILLOW:
        return None
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), "#0a0e14")
    draw = ImageDraw.Draw(img)

    # Разделитель и акценты
    draw.rectangle([8, 8, W - 9, H - 9], outline="#1c2530", width=2)
    # Левая колонка (Token 1)
    draw.rectangle([20, 20, W // 2 - 10, H - 70], fill="#0f1912", outline="#26d07c", width=2)
    # Правая колонка (Token 2)
    draw.rectangle([W // 2 + 10, 20, W - 20, H - 70], fill="#141924", outline="#3dd9f0", width=2)

    f_sym = load_font(52, bold=True)
    f_pct = load_font(44, bold=True)
    f_sub = load_font(20, bold=False)
    f_vs = load_font(60, bold=True)
    f_mark = load_font(18, bold=False)

    s1, s2 = p1.get("base_symbol", "?"), p2.get("base_symbol", "?")
    c1 = (p1.get("change") or {}).get("h24") or 0
    c2 = (p2.get("change") or {}).get("h24") or 0

    # Левый токен
    draw.text((50, 50), s1, fill="#ffffff", font=f_sym)
    draw.text((50, 120), fmt_pct(c1) + " (24ч)", fill="#26d07c" if c1 >= 0 else "#ff4d6a", font=f_pct)
    draw.text((50, 190), f"Объём: {fmt_usd(p1.get('volume_h24'))}", fill="#e8edf2", font=f_sub)
    draw.text((50, 230), f"Ликвидность: {fmt_usd(p1.get('reserve_usd'))}", fill="#e8edf2", font=f_sub)
    draw.text((50, 270), f"DEX: {p1.get('dex', '?')}", fill="#8b98a5", font=f_sub)
    draw.text((50, 310), f"FDV: {fmt_usd(p1.get('fdv_usd'))}", fill="#8b98a5", font=f_sub)

    # Правый токен
    draw.text((W // 2 + 40, 50), s2, fill="#ffffff", font=f_sym)
    draw.text((W // 2 + 40, 120), fmt_pct(c2) + " (24ч)", fill="#26d07c" if c2 >= 0 else "#ff4d6a", font=f_pct)
    draw.text((W // 2 + 40, 190), f"Объём: {fmt_usd(p2.get('volume_h24'))}", fill="#e8edf2", font=f_sub)
    draw.text((W // 2 + 40, 230), f"Ликвидность: {fmt_usd(p2.get('reserve_usd'))}", fill="#e8edf2", font=f_sub)
    draw.text((W // 2 + 40, 270), f"DEX: {p2.get('dex', '?')}", fill="#8b98a5", font=f_sub)
    draw.text((W // 2 + 40, 310), f"FDV: {fmt_usd(p2.get('fdv_usd'))}", fill="#8b98a5", font=f_sub)

    # Круг VS по центру
    cx, cy, r = W // 2, 280, 55
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#fba43a", outline="#ffffff", width=3)
    draw.text((cx - 42, cy - 35), "VS", fill="#000000", font=f_vs)

    # Водяной знак
    draw.text((30, H - 42), config.CARD_MARK, fill="#7cf03d", font=f_mark)
    draw.text((W - 380, H - 42), config.SITE_URL.replace("https://", ""), fill="#8b98a5", font=f_mark)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)
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

        faq_items_html = "".join(f'<div style="margin:16px 0"><b>{item["q"]}</b><p style="color:#b0bcc8;margin:6px 0">{item["a"]}</p></div>' for item in faq_data)
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
<style>
body{{background:#0a0e14;color:#e8edf2;font-family:ui-monospace,Menlo,Consolas,monospace;margin:0;padding:40px 20px;max-width:840px;margin:auto;line-height:1.7}}
h1{{font-size:26px}} .mut{{color:#8b98a5;font-size:13px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:20px 0}}
.box{{background:#121820;border:1px solid #1c2530;border-radius:10px;padding:18px}}
.box.token1{{border-top:4px solid #26d07c}} .box.token2{{border-top:4px solid #3dd9f0}}
.verdict{{background:#141a24;border:1px solid #253347;border-left:4px solid #fba43a;border-radius:8px;padding:16px;margin:20px 0}}
table{{width:100%;border-collapse:collapse;margin:20px 0}}td,th{{border:1px solid #1c2530;padding:10px 14px;text-align:left}}
a.cta{{display:inline-block;background:#7cf03d;color:#000;font-weight:bold;padding:12px 22px;border-radius:8px;text-decoration:none;margin:8px 8px 8px 0}}
a{{color:#7cf03d}}
img.card{{width:100%;max-width:700px;border-radius:10px;border:1px solid #1c2530;display:block;margin:20px 0}}
</style>
</head>
<body>
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
<tr><td>Суточный рост (24ч)</td><td><b style="color:{'#26d07c' if c1 >= 0 else '#ff4d6a'}">{fmt_pct(c1)}</b></td><td><b style="color:{'#26d07c' if c2 >= 0 else '#ff4d6a'}">{fmt_pct(c2)}</b></td><td>{s1 if c1 > c2 else s2}</td></tr>
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
<a class="cta" href="../index.html#pool={p2['address']}" style="background:#3dd9f0">Открыть чарт {s2} →</a>
<a class="cta" href="../index.html#games" style="background:#fba43a">🎁 Забрать TipLink бонус в игры →</a>
</div>

<hr style="border-color:#1c2530;margin:30px 0">
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
        f'<small><a href="{slug}.md" style="color:#8b98a5">[md]</a></small></li>'
        for slug, s1, s2, c1, c2, title in battle_links
    )
    catalog_html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Баттлы токенов Solana — сравнение пар активов | TalkChart</title>
<link rel="canonical" href="{config.SITE_URL}/vs/index.html">
</head>
<body style="background:#0a0e14;color:#e8edf2;font-family:monospace;max-width:820px;margin:auto;padding:40px 20px;line-height:2">
<p><a href="../index.html" style="color:#7cf03d">📈 TalkChart</a> / ончейн-баттлы токенов</p>
<h1>Ончейн-баттлы токенов Solana (Сравнение активов)</h1>
<ul>{items_html}</ul>
<p style="color:#8b98a5">Обновляется автоматически каждые 4 часа через GeckoTerminal DEX tape.</p>
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
