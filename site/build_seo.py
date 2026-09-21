#!/usr/bin/env python3
"""Шаг 2 фабрики: программный SEO-билд — 1 актив = 1 статическая страница.

Источник: site/data/registry.json (реестр всех виденных пулов — long-tail
накапливается: пул выпал из trending, страница осталась и ловит поиск).
Фолбэк: site/data/snapshot.json.

Нарратив — из site/factory/narrative.py (единый движок с терминалом и карточками).
В проде запускается из .github/workflows/factory.yml по расписанию, страницы
пересобираются со свежими данными; количество растёт с каждым прогоном.
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "factory"))
import config  # noqa: E402
from narrative import fmt_pct, fmt_usd, narrative  # noqa: E402

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{sym} ({name}) — график и курс онлайн | TalkChart</title>
<meta name="description" content="{sym}: цена {price}, {chg} за 24ч, объём {vol}, ликвидность {res}. Живой чарт {name} на {dex} + автоматический разбор: что двигает цену.">
<meta property="og:title" content="{sym} {chg} за 24ч — что говорит график">
<meta property="og:description" content="{narr}">
<meta property="og:type" content="website">
<link rel="canonical" href="{canonical}">
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"FinancialProduct","name":"{name}","description":"{narr}","url":"{canonical}"}}
</script>
<style>
body{{background:#0a0e14;color:#e8edf2;font-family:ui-monospace,Menlo,Consolas,monospace;margin:0;padding:40px 20px;max-width:760px;margin:auto;line-height:1.7}}
h1{{font-size:26px}} .chg{{color:{chgcolor};font-weight:bold}} .mut{{color:#8b98a5;font-size:13px}}
table{{border-collapse:collapse;margin:18px 0}}td,th{{border:1px solid #1c2530;padding:8px 14px;text-align:left}}
.narr{{background:#101a10;border:1px solid #24331f;border-radius:10px;padding:16px;margin:18px 0}}
.flags span{{background:rgba(255,77,106,.12);border:1px solid rgba(255,77,106,.4);color:#ff8fa3;padding:3px 10px;border-radius:20px;font-size:12px;margin-right:6px}}
a.cta{{display:inline-block;background:#7cf03d;color:#000;font-weight:bold;padding:12px 22px;border-radius:8px;text-decoration:none;margin:8px 8px 8px 0}}
a{{color:#7cf03d}}
</style>
</head>
<body>
<p class="mut"><a href="../index.html">📈 TalkChart — графики, которые разговаривают</a> · <a href="index.html">каталог чартов</a></p>
<h1>{sym} / {quote} — график и курс <span class="chg">{chg}</span> за 24ч</h1>
<p class="mut">Пуль {name} на {dex} · Solana · адрес пула <code>{addr}</code> · токен <code>{baddr}</code></p>
<div class="narr"><b>🗣 Что говорит график:</b> {narr}
{flags_html}</div>
<table>
<tr><th>Цена</th><td>{price}</td></tr>
<tr><th>Изменение 1ч / 6ч / 24ч</th><td>{c1} / {c6} / {c24}</td></tr>
<tr><th>Объём 24ч</th><td>{vol}</td></tr>
<tr><th>Ликвидность пула</th><td>{res}</td></tr>
<tr><th>FDV</th><td>{fdv}</td></tr>
<tr><th>Покупки / продажи за 1ч</th><td>{buys} / {sells}</td></tr>
<tr><th>Пул создан</th><td>{created}</td></tr>
<tr><th>Данные обновлены</th><td>{seen}</td></tr>
</table>
<p>Живой чарт с алертами, автообновлением и карточкой для шеринга — в терминале:</p>
<a class="cta" href="../index.html#pool={addr}">Открыть живой чарт {sym} →</a>
<hr style="border-color:#1c2530">
<p class="mut">Страница сгенерирована автоматически фабрикой контента из данных GeckoTerminal API {ts}.
Обновляется по расписанию. Не является финансовым советом.</p>
<p class="mut">🎮 Игры студии — в терминале: <a href="../index.html#games">слоты игр</a>.</p>
</body>
</html>
"""


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


def main():
    pools, source = load_pools()
    os.makedirs(config.POOLS_DIR, exist_ok=True)
    now = datetime.now(timezone.utc)
    for p in pools:
        c = p.get("change", {}) or {}
        h1, h6, h24 = c.get("h1") or 0, c.get("h6") or 0, c.get("h24") or 0
        quote = p["name"].split(" / ")[1] if " / " in p.get("name", "") else "SOL"
        text, flags = narrative(p)
        addr = p["address"]
        html = PAGE.format(
            sym=p.get("base_symbol", "?"), name=p.get("name", "?"), quote=quote,
            dex=p.get("dex", "?"), addr=addr, baddr=p.get("base_address") or "—",
            price=fmt_usd(p.get("price_usd")), chg=fmt_pct(h24),
            chgcolor="#26d07c" if h24 >= 0 else "#ff4d6a",
            c1=fmt_pct(h1), c6=fmt_pct(h6), c24=fmt_pct(h24),
            vol=fmt_usd(p.get("volume_h24")), res=fmt_usd(p.get("reserve_usd")),
            fdv=fmt_usd(p.get("fdv_usd")),
            buys=(p.get("tx_h1") or {}).get("buys", "—"),
            sells=(p.get("tx_h1") or {}).get("sells", "—"),
            created=p.get("created_at") or "—",
            seen=p.get("last_seen") or "—",
            narr=text,
            flags_html=('<div class="flags">' + "".join(f"<span>⚠ {f}</span>" for f in flags) + "</div>") if flags else "",
            canonical=f"{config.SITE_URL}/pools/{addr}.html",
            ts=now.strftime("%Y-%m-%d %H:%M UTC"),
        )
        with open(os.path.join(config.POOLS_DIR, addr + ".html"), "w") as f:
            f.write(html)

    ranked = sorted(pools, key=lambda p: p.get("volume_h24") or 0, reverse=True)
    items = "\n".join(
        f'<li><a href="{p["address"]}.html">{p.get("base_symbol", "?")} ({p.get("name", "?")}) — '
        f'график, {fmt_pct((p.get("change") or {}).get("h24") or 0)} за 24ч, объём {fmt_usd(p.get("volume_h24"))}</a></li>'
        for p in ranked
    )
    with open(os.path.join(config.POOLS_DIR, "index.html"), "w") as f:
        f.write(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Чарты Solana — каталог графиков с разбором | TalkChart</title>
<meta name="description" content="Каталог автоматических чарт-страниц Solana: цена, объём, ликвидность и разбор того, что двигает каждый актив. Обновляется по расписанию.">
<link rel="canonical" href="{config.SITE_URL}/pools/index.html">
</head>
<body style="background:#0a0e14;color:#e8edf2;font-family:monospace;max-width:760px;margin:auto;padding:40px 20px;line-height:2">
<p><a href="../index.html" style="color:#7cf03d">📈 TalkChart</a> / каталог чартов Solana ({len(ranked)} активов)</p>
<h1>Графики, которые разговаривают — каталог</h1>
<ul>{items}</ul>
<p style="color:#8b98a5">Каталог растёт автоматически: каждый прогон фабрики добавляет новые активы
и обновляет данные существующих. Сгенерировано {now:%Y-%m-%d %H:%M UTC}.</p>
</body></html>""")
    print(f"OK: {len(pools)} SEO-страниц + каталог -> site/pools/ (источник: {source})")


if __name__ == "__main__":
    main()
