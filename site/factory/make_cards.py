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
from pixelart import (ACC, AMBER, BG, CYAN, GREEN, INK, MUT, PANEL, RED, WHITE,
                      dither_bg, pixel_bars, pixelate, wrap)

W, H = 1200, 630
# Пиксель-рендер: рисуем в 1/PS размера, затем NEAREST-апскейл и приведение
# к палитре (pixelart.pixelate) — полутона антиалиасинга исчезают полностью.
PS = 5
SW, SH = W // PS, H // PS          # 240 × 126

FONT = os.path.join(config.FONTS_DIR, "DejaVuSans.ttf")
FONT_B = os.path.join(config.FONTS_DIR, "DejaVuSans-Bold.ttf")


def make_card(p, out_path, fonts):
    img = Image.new("RGB", (SW, SH), BG)
    d = ImageDraw.Draw(img)
    f_sym, f_big, f_mid, f_small = fonts
    c24 = (p.get("change") or {}).get("h24") or 0
    up = c24 >= 0
    col = GREEN if up else RED

    dither_bg(d, 0, 0, SW, SH, PANEL, 4)                 # фон «в клетку»
    d.rectangle([0, 0, SW - 1, SH - 1], outline=col)     # рамка цветом тренда
    d.rectangle([2, 2, SW - 3, SH - 3], outline=PANEL)
    d.rectangle([4, 4, 44, 6], fill=ACC)                 # «ушко» HUD, а не полоса во всю ширину

    d.text((10, 12), str(p.get("base_symbol", "?")).upper()[:12], font=f_sym, fill=WHITE)
    d.text((10, 28), fmt_pct(c24), font=f_big, fill=col)
    d.text((10, 44), f'{p.get("dex", "")} · VOL {fmt_usd(p.get("volume_h24"))}'[:34],
           font=f_small, fill=MUT)

    # График: либо реальные столбики, либо честная заглушка. Дорисовывать
    # линию по отсутствующим данным — значит выдавать выдумку за чарт.
    closes = [c[4] for c in (p.get("ohlcv_h1") or [])]
    closes.reverse()                                     # API отдаёт новые -> старые
    chart_x0, chart_y0, chart_x1, chart_y1 = 10, 58, SW - 10, 88
    if len(closes) >= 2:
        pixel_bars(d, closes[-48:], chart_x0, chart_y0, chart_x1, chart_y1, col, up)
    else:
        dither_bg(d, chart_x0, chart_y0 + 10, chart_x1, chart_y0 + 12, MUT, 2)
        d.text((chart_x0, chart_y0 + 14), "НЕТ ДАННЫХ ГРАФИКА", font=f_small, fill=MUT)

    text, flags = narrative(p)
    y = 94
    for ln in wrap(d, text, f_mid, SW - 20)[:2]:
        d.text((10, y), ln, font=f_mid, fill=WHITE)
        y += 9
    if flags:
        label = flags[0][:26]
        tw = int(d.textlength(label, font=f_small))
        d.rectangle([10, 108, 10 + tw + 8, 118], outline=RED)
        d.text((14, 109), label, font=f_small, fill=RED)

    mark = str(config.CARD_MARK)[:44]
    d.text((10, SH - 9), mark, font=f_small, fill=ACC)
    host = config.SITE_URL.split("//")[-1][:24]
    d.text((SW - 10 - int(d.textlength(host, font=f_small)), SH - 9), host,
           font=f_small, fill=MUT)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    pixelate(img, (W, H)).save(out_path, "PNG", optimize=True)


def main():
    with open(config.SNAPSHOT) as f:
        snap = json.load(f)
    pools = snap.get("pools", [])
    if not pools:
        print("snapshot пуст", file=sys.stderr)
        sys.exit(1)

    # Размеры — под низкое разрешение (SW × SH): после апскейла ×5 это
    # 70 / 60 / 35 / 30 px на итоговой карточке.
    fonts = (
        ImageFont.truetype(FONT_B, 14),
        ImageFont.truetype(FONT_B, 12),
        ImageFont.truetype(FONT, 7),
        ImageFont.truetype(FONT, 6),
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
