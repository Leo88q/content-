#!/usr/bin/env python3
"""Шаг 3 фабрики: шаринг-карточки 1200×630 (PNG, Pillow) из snapshot.json.

Карточка = артефакт дистрибуции: чарт + нарратив + водяной знак со ссылкой.
Выход:
  site/cards/<YYYY-MM-DD>/<SYM>-<chg>.png   — архив дня
  site/cards/latest/*.png + manifest.json   — витрина для терминала и постов
"""
import json
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from narrative import fmt_pct, fmt_usd, narrative

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Pillow не установлен: pip install pillow", file=sys.stderr)
    sys.exit(1)

W, H = 1200, 630
BG = (10, 14, 20)
ACC = (124, 240, 61)
GREEN = (38, 208, 124)
RED = (255, 77, 106)
MUT = (139, 152, 165)
WHITE = (232, 237, 242)

FONT = os.path.join(config.FONTS_DIR, "DejaVuSans.ttf")
FONT_B = os.path.join(config.FONTS_DIR, "DejaVuSans-Bold.ttf")


def wrap(draw, text, font, max_w):
    words, lines, line = text.split(), [], ""
    for w_ in words:
        t = (line + " " + w_).strip()
        if draw.textlength(t, font=font) <= max_w:
            line = t
        else:
            if line:
                lines.append(line)
            line = w_
    if line:
        lines.append(line)
    return lines


def sparkline(draw, closes, x0, y0, x1, y1, color, width=5):
    if len(closes) < 2:
        return
    hi, lo = max(closes), min(closes)
    span = hi - lo or hi * 0.01 or 1
    pts = []
    for i, c in enumerate(closes):
        x = x0 + (i / (len(closes) - 1)) * (x1 - x0)
        y = y1 - ((c - lo) / span) * (y1 - y0)
        pts.append((x, y))
    draw.line(pts, fill=color, width=width, joint="curve")


def make_card(p, out_path, fonts):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, W - 9, H - 9], outline=ACC, width=3)

    f_sym, f_big, f_mid, f_small = fonts
    c24 = (p.get("change") or {}).get("h24") or 0
    up = c24 >= 0
    col = GREEN if up else RED

    d.text((60, 44), p.get("base_symbol", "?"), font=f_sym, fill=WHITE)
    d.text((60, 128), fmt_pct(c24), font=f_big, fill=col)
    meta = f'{p.get("name", "")} · {p.get("dex", "")} · Vol {fmt_usd(p.get("volume_h24"))} · Liq {fmt_usd(p.get("reserve_usd"))}'
    d.text((60, 212), meta, font=f_small, fill=MUT)

    def pill(x, yy, label, color):
        tw = d.textlength(label, font=f_small) + 24
        d.rounded_rectangle([x, yy + 6, x + tw, yy + 36], radius=14, outline=color, width=1)
        d.text((x + 12, yy + 12), label, font=f_small, fill=color)
        return x + tw + 10

    text, flags = narrative(p)
    lines = wrap(d, text, f_mid, W - 120)[:3]
    y = 262
    for ln in lines:
        d.text((60, y), ln, font=f_mid, fill=WHITE)
        y += 36
    fx = 60
    for fl in flags[:3]:
        fx = pill(fx, y, fl, RED)
    whales = (p.get("whales") or [])[:2]
    if whales:
        y += 40
        fx = 60
        for w in whales:
            label = f"WHALE {('BUY' if w.get('kind') == 'buy' else 'SELL')} {fmt_usd(w.get('usd'))}"
            fx = pill(fx, y, label, GREEN if w.get("kind") == "buy" else RED)

    closes = [c[4] for c in (p.get("ohlcv_h1") or [])]
    closes.reverse()  # API отдаёт новые -> старые
    sparkline(d, closes, 60, 460, W - 60, 545, col)

    d.text((60, H - 56), config.CARD_MARK, font=f_small, fill=ACC)
    d.text((W - 60 - d.textlength(config.SITE_URL.split("//")[-1], font=f_small), H - 56),
           config.SITE_URL.split("//")[-1], font=f_small, fill=MUT)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, "PNG", optimize=True)


def main():
    with open(config.SNAPSHOT) as f:
        snap = json.load(f)
    pools = snap.get("pools", [])
    if not pools:
        print("snapshot пуст", file=sys.stderr)
        sys.exit(1)

    fonts = (
        ImageFont.truetype(FONT_B, 68),
        ImageFont.truetype(FONT_B, 62),
        ImageFont.truetype(FONT, 27),
        ImageFont.truetype(FONT, 24),
    )

    ranked = sorted(pools, key=lambda p: abs((p.get("change") or {}).get("h24") or 0), reverse=True)
    ranked = ranked[:config.CARDS_TOP_N]
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_dir = os.path.join(config.CARDS_DIR, day)
    latest = os.path.join(config.CARDS_DIR, "latest")
    os.makedirs(day_dir, exist_ok=True)
    os.makedirs(latest, exist_ok=True)
    for old in os.listdir(latest):
        os.remove(os.path.join(latest, old))

    manifest = []
    for p in ranked:
        c24 = (p.get("change") or {}).get("h24") or 0
        fname = f'{p.get("base_symbol", "TOKEN").replace("/", "-")}_{int(c24):+d}.png'.replace(" ", "")
        src = os.path.join(day_dir, fname)
        make_card(p, src, fonts)
        entry = {
            "file": f"cards/{day}/{fname}",
            "symbol": p.get("base_symbol"),
            "chg_h24": c24,
            "address": p.get("address"),
            "pool_url": f"{config.SITE_URL}/pools/{p.get('address')}.html",
        }
        manifest.append(entry)
        if len(manifest) <= 3:
            shutil.copy(src, os.path.join(latest, fname))
            entry["latest_file"] = f"cards/latest/{fname}"

    with open(os.path.join(latest, "manifest.json"), "w") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "cards": manifest}, f, ensure_ascii=False, indent=1)
    print(f"OK: {len(manifest)} карточек -> cards/{day}/, топ-3 + manifest -> cards/latest/")


if __name__ == "__main__":
    main()
