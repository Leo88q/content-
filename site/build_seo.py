#!/usr/bin/env python3
"""Программный SEO-билд: 1 актив = 1 статическая страница.

Читает data/snapshot.json (или свежий дамп API) и генерирует
site/pools/<address>.html — чарт-страницы с авто-нарративом,
meta/OG/JSON-LD. В проде скрипт ходит в GeckoTerminal API сам
и пересобирает страницы по расписанию (cron/GitHub Actions),
наращивая long-tail: каждый актив ниши = страница под запрос
«[token] price chart / график / курс».
"""
import json
import os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "pools")
SITE_URL = "https://leo88q.github.io/content-"  # заменится на домен


def fmt_usd(n):
    if n is None:
        return "—"
    a = abs(n)
    if a >= 1e9:
        return f"${n/1e9:.2f}B"
    if a >= 1e6:
        return f"${n/1e6:.2f}M"
    if a >= 1e3:
        return f"${n/1e3:.1f}K"
    if a >= 1:
        return f"${n:.2f}"
    if a >= 0.01:
        return f"${n:.4f}"
    return f"${n:.3g}"


def fmt_pct(n):
    return f"{n:+.1f}%"


def narrative(p):
    """Порт движка нарративов из app.js — тот же текст для SEO."""
    c = p["change"]
    tx = p.get("tx_h1", {})
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    buy_ratio = buys / (buys + sells) if buys + sells else 0.5
    created = p.get("created_at")
    age_days = None
    if created:
        age_days = max(0, round((datetime.now(timezone.utc) - datetime.fromisoformat(created.replace("Z", "+00:00"))).days))
    parts = []
    s = p["base_symbol"]
    if c["h24"] >= 30:
        parts.append(f"{s} разрывает: {fmt_pct(c['h24'])} за 24ч")
    elif c["h24"] >= 8:
        parts.append(f"{s} растёт на {fmt_pct(c['h24'])} за сутки")
    elif c["h24"] <= -25:
        parts.append(f"{s} в обвале: {fmt_pct(c['h24'])} за 24ч")
    elif c["h24"] <= -8:
        parts.append(f"{s} теряет {fmt_pct(c['h24'])} за сутки")
    else:
        parts.append(f"{s} в боковике: {fmt_pct(c['h24'])} за сутки")
    if buy_ratio >= 0.62:
        parts.append(f"покупатели доминируют ({round(buy_ratio*100)}% сделок за час)")
        if c["h1"] < -3:
            parts.append("но цена всё равно падает — кто-то разгружается в стакан")
    elif buy_ratio <= 0.38:
        parts.append(f"продают в рынок ({round((1-buy_ratio)*100)}% сделок за час)")
        if c["h24"] >= 15:
            parts.append("суточный памп остывает")
    fdv = p.get("fdv_usd")
    vol = p.get("volume_h24")
    if fdv and vol and vol / fdv > 1.5:
        parts.append(f"объём {fmt_usd(vol)} больше капы в {vol/fdv:.1f}× — бумага в огне")
    res = p.get("reserve_usd")
    if res is not None and res < 250000:
        parts.append(f"ликвидность тонкая ({fmt_usd(res)}) — движения будут резкими")
    elif res is not None and res > 3e6:
        parts.append(f"стакан глубокий: {fmt_usd(res)} ликвидности")
    if age_days is not None and age_days <= 3:
        parts.append(f"пулу {age_days} дн. — чистая рулетка")
    elif age_days is not None and age_days <= 14:
        parts.append(f"пулу {age_days} дн., история короткая")
    return ". ".join(parts) + "."


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
a.cta{{display:inline-block;background:#7cf03d;color:#000;font-weight:bold;padding:12px 22px;border-radius:8px;text-decoration:none;margin:8px 8px 8px 0}}
a{{color:#7cf03d}}
</style>
</head>
<body>
<p class="mut"><a href="../index.html">📈 TalkChart — графики, которые разговаривают</a></p>
<h1>{sym} / {quote} — график и курс <span class="chg">{chg}</span> за 24ч</h1>
<p class="mut">Пуль {name} на {dex} · Solana · адрес пула <code>{addr}</code> · токен <code>{baddr}</code></p>
<div class="narr"><b>🗣 Что говорит график:</b> {narr}</div>
<table>
<tr><th>Цена</th><td>{price}</td></tr>
<tr><th>Изменение 1ч / 6ч / 24ч</th><td>{c1} / {c6} / {c24}</td></tr>
<tr><th>Объём 24ч</th><td>{vol}</td></tr>
<tr><th>Ликвидность пула</th><td>{res}</td></tr>
<tr><th>FDV</th><td>{fdv}</td></tr>
<tr><th>Покупки / продажи за 1ч</th><td>{buys} / {sells}</td></tr>
<tr><th>Пул создан</th><td>{created}</td></tr>
</table>
<p>Живой чарт с алертами и автообновлением — в терминале:</p>
<a class="cta" href="../index.html#pool={addr}">Открыть живой чарт {sym} →</a>
<hr style="border-color:#1c2530">
<p class="mut">Страница сгенерирована автоматически из данных GeckoTerminal API {ts}.
Данные обновляются по расписанию. Не является финансовым советом.</p>
<p class="mut">Игры студии: <a href="../index.html#games">каталог</a>.</p>
</body>
</html>
"""


def main():
    with open(os.path.join(HERE, "data", "snapshot.json")) as f:
        snap = json.load(f)
    os.makedirs(OUT, exist_ok=True)
    for p in snap["pools"]:
        c = p["change"]
        quote = p["name"].split(" / ")[1] if " / " in p["name"] else "SOL"
        html = PAGE.format(
            sym=p["base_symbol"], name=p["name"], quote=quote, dex=p["dex"],
            addr=p["address"], baddr=p["base_address"],
            price=fmt_usd(p["price_usd"]), chg=fmt_pct(c["h24"]),
            chgcolor="#26d07c" if c["h24"] >= 0 else "#ff4d6a",
            c1=fmt_pct(c["h1"]), c6=fmt_pct(c["h6"]), c24=fmt_pct(c["h24"]),
            vol=fmt_usd(p.get("volume_h24")), res=fmt_usd(p.get("reserve_usd")),
            fdv=fmt_usd(p.get("fdv_usd")),
            buys=p.get("tx_h1", {}).get("buys", "—"), sells=p.get("tx_h1", {}).get("sells", "—"),
            created=p.get("created_at", "—"), narr=narrative(p),
            canonical=f"{SITE_URL}/site/pools/{p['address']}.html",
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )
        with open(os.path.join(OUT, p["address"] + ".html"), "w") as f:
            f.write(html)
    # индексная страница каталога (тоже SEO-актив)
    items = "\n".join(
        f'<li><a href="{p["address"]}.html">{p["base_symbol"]} ({p["name"]}) — график, {fmt_pct(p["change"]["h24"])} за 24ч</a></li>'
        for p in snap["pools"]
    )
    with open(os.path.join(OUT, "index.html"), "w") as f:
        f.write(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Чарты Solana — каталог пуль | TalkChart</title>
<meta name="description" content="Каталог автоматических чарт-страниц Solana: цена, объём, ликвидность и разбор движений."></head>
<body style="background:#0a0e14;color:#e8edf2;font-family:monospace;max-width:720px;margin:auto;padding:40px 20px;line-height:2">
<p><a href="../index.html" style="color:#7cf03d">📈 TalkChart</a> / каталог пуль</p>
<h1>Чарты Solana — каталог</h1>
<ul>{items}</ul>
<p style="color:#8b98a5">В проде: 2–5K таких страниц на всю нишу, обновление по cron. Сгенерировано {datetime.now(timezone.utc):%Y-%m-%d}.</p>
</body></html>""")
    print(f"OK: {len(snap['pools'])} pool pages + index -> site/pools/")


if __name__ == "__main__":
    main()
