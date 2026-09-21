"""Движок авто-нарративов — единственный источник правды (RU).

Тот же алгоритм, что в site/app.js (нарратив в терминале) и в SEO-страницах.
Правила меняются только здесь и зеркалятся в app.js.
"""
from datetime import datetime, timezone


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
    try:
        return f"{float(n):+.1f}%"
    except (TypeError, ValueError):
        return "—"


def pool_age_days(p, now=None):
    created = p.get("created_at")
    if not created:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, round((now - dt).total_seconds() / 86400))


def narrative(p):
    """Возвращает (text, flags) — текст нарратива и список risk-флагов."""
    c = p.get("change", {}) or {}
    h1, h24 = c.get("h1", 0) or 0, c.get("h24", 0) or 0
    tx = p.get("tx_h1", {}) or {}
    buys, sells = tx.get("buys", 0) or 0, tx.get("sells", 0) or 0
    buy_ratio = buys / (buys + sells) if buys + sells else 0.5
    age = pool_age_days(p)
    s = p.get("base_symbol", "TOKEN")
    fdv, vol, res = p.get("fdv_usd"), p.get("volume_h24"), p.get("reserve_usd")
    parts, flags = [], []

    # направление за 24ч
    if h24 >= 30:
        parts.append(f"{s} разрывает: {fmt_pct(h24)} за 24ч")
    elif h24 >= 8:
        parts.append(f"{s} растёт на {fmt_pct(h24)} за сутки")
    elif h24 <= -25:
        parts.append(f"{s} в обвале: {fmt_pct(h24)} за 24ч")
    elif h24 <= -8:
        parts.append(f"{s} теряет {fmt_pct(h24)} за сутки")
    else:
        parts.append(f"{s} в боковике: {fmt_pct(h24)} за сутки")

    # моментум последнего часа
    if h1 > 3 and c.get("h6", 0) and c["h6"] > h1:
        parts.append("ускорение в последнем часе")
    elif h1 < -3 and c.get("h6", 0) and c["h6"] < 0:
        parts.append("давление продаж нарастает")

    # поток сделок + дивергенции «поток vs цена»
    if buy_ratio >= 0.62:
        parts.append(f"покупатели доминируют ({round(buy_ratio * 100)}% сделок за час)")
        if h1 < -3:
            parts.append("но цена всё равно падает — кто-то разгружается в стакан")
    elif buy_ratio <= 0.38:
        parts.append(f"продают в рынок ({round((1 - buy_ratio) * 100)}% сделок за час)")
        if h24 >= 15:
            parts.append("суточный памп остывает")

    # оборот / ликвидность
    if fdv and vol and vol / fdv > 1.5:
        parts.append(f"объём {fmt_usd(vol)} больше капы в {vol/fdv:.1f}× — бумага в огне")
    if res is not None and res < 250_000:
        parts.append(f"ликвидность тонкая ({fmt_usd(res)}) — движения будут резкими")
    elif res is not None and res > 3_000_000:
        parts.append(f"стакан глубокий: {fmt_usd(res)} ликвидности")

    # возраст пула
    if age is not None and age <= 3:
        parts.append(f"пулу {age} дн. — чистая рулетка")
    elif age is not None and age <= 14:
        parts.append(f"пулу {age} дн., история короткая")

    # китовый радар: крупные сделки за сутки
    wh = p.get("whales") or []
    if wh:
        top = wh[0]
        verb = "купил" if top.get("kind") == "buy" else "продал"
        ago = top.get("hours_ago")
        ago_s = f"{ago:.0f}ч назад" if ago is not None else "только что"
        parts.append(f"кит {verb} {fmt_usd(top.get('usd'))} ({ago_s})")
        buys_usd = sum(w.get("usd", 0) for w in wh if w.get("kind") == "buy")
        sells_usd = sum(w.get("usd", 0) for w in wh if w.get("kind") == "sell")
        net = buys_usd - sells_usd
        if net != 0:
            parts.append("нетто китов за сутки " + ("+" if net > 0 else "−") + fmt_usd(abs(net)))
        if top.get("kind") == "sell" and res and top.get("usd", 0) > res * 0.1:
            flags.append("кит разгружает позицию")

    # risk-флаги
    if res is not None and res < 250_000:
        flags.append("тонкая ликвидность")
    if age is not None and age <= 7:
        flags.append("молодой пул")
    if buy_ratio <= 0.38:
        flags.append("давление продаж")
    if h24 >= 50:
        flags.append("перегрет за 24ч")

    return ". ".join(parts) + ".", flags


def headline(p):
    """Однострочник для дайджестов/постов (<= 140 символов)."""
    text, _ = narrative(p)
    # первая и вторая фраза — обычно направление + главный драйвер
    sentences = text.split(". ")
    short = ". ".join(sentences[:2])
    return (short[:137] + "…") if len(short) > 140 else short
