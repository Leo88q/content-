#!/usr/bin/env python3
"""Шаг 3.5 фабрики: вертикальные 15-секундные mp4 (TikTok/Shorts/Reels).

Формат, который смотрят без звука: анимированный спарклайн + крупные
субтитры-нарратив + водяной знак. Рендер: Pillow (кадры) + ffmpeg (склейка).
Если ffmpeg нет — шаг вежливо пропускается (локальные прогоны, форки).

Выход:
  site/videos/<YYYY-MM-DD>/<SYM>.mp4   — архив дня (топ-3 по |h24|)
  site/videos/latest/*.mp4 + manifest.json — витрина терминала
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from narrative import fmt_pct as format_pct, fmt_usd, narrative
from pixelart import wrap

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:
    PIL_OK = False
if PIL_OK:
    from pixelart import (ACC, BG, GREEN, MUT, PANEL, RED, WHITE,
                          dither_bg, pixel_bars, pixelate)

FR, SEC = 12, 15                 # 12 fps, 15 секунд = 180 кадров
W, H = 540, 960                  # вертикаль 9:16
PS = 3                           # «жирный» пиксель: кадры рисуются в 1/3
SW, SH = W // PS, H // PS        # 180 × 320
BG = (10, 14, 20)
ACC = (124, 240, 61)
GREEN = (38, 208, 124)
RED = (255, 77, 106)
MUT = (139, 152, 165)
WHITE = (232, 237, 242)

FONT = os.path.join(config.FONTS_DIR, "DejaVuSans.ttf")
FONT_B = os.path.join(config.FONTS_DIR, "DejaVuSans-Bold.ttf")


def render_frame(p, t, closes, fragments, fonts, path):
    """Кадр в низком разрешении → NEAREST-апскейл → палитра."""
    f_sym, f_pct, f_cap, f_small = fonts
    img = Image.new("RGB", (SW, SH), BG)
    d = ImageDraw.Draw(img)
    dither_bg(d, 0, 0, SW, SH, PANEL, 4)
    frame_col = GREEN if ((p.get("change") or {}).get("h24") or 0) >= 0 else RED
    d.rectangle([0, 0, SW - 1, SH - 1], outline=frame_col)
    d.rectangle([2, 2, SW - 3, SH - 3], outline=PANEL)
    d.rectangle([4, 4, 34, 6], fill=ACC)

    c24 = (p.get("change") or {}).get("h24") or 0
    col = GREEN if c24 >= 0 else RED
    d.text((10, 16), str(p.get("base_symbol", "?")).upper()[:10], font=f_sym, fill=WHITE)
    d.text((10, 34), format_pct(c24), font=f_pct, fill=col)
    d.text((10, 52), f'VOL {fmt_usd(p.get("volume_h24"))}'[:26], font=f_small, fill=MUT)

    # Прогрессивный спарклайн «дорисовывается» за первые 10 секунд.
    # Нет данных — говорим об этом прямо, а не рисуем плоскую линию: плоская
    # линия — это выдуманная история.price
    if len(closes) >= 2:
        n = max(2, int(len(closes) * min(1.0, t / 10.0)))
        pixel_bars(d, closes[:n], 10, 150, SW - 10, 210, c24 >= 0, GREEN, RED)
    else:
        d.text((10, 170), "НЕТ ДАННЫХ ГРАФИКА", font=f_small, fill=MUT)

    # субтитры-нарратив: фрагмент i появляется с t >= 1.5 + 3.5*i
    y = 224
    for i, frag in enumerate(fragments[:3]):
        if t < 1.5 + 3.5 * i:
            break
        for ln in frag[:2]:
            d.text((10, y), ln, font=f_cap, fill=WHITE)
            y += 9
        y += 4

    d.text((10, SH - 12), str(config.CARD_MARK)[:34], font=f_small, fill=ACC)
    pixelate(img, (W, H)).save(path, "PNG")


def main():
    if not PIL_OK:
        print("SKIP: нет Pillow — видеощаг пропущен (карточки и SEO не затронуты)")
        return
    if not shutil.which("ffmpeg"):
        print("SKIP: нет ffmpeg в PATH — видеощаг пропущен (карточки и SEO не затронуты)")
        return
    with open(config.SNAPSHOT) as f:
        snap = json.load(f)
    pools = snap.get("pools", [])
    if not pools:
        print("snapshot пуст", file=sys.stderr)
        sys.exit(1)

    # Размеры — под низкое разрешение (SW × SH): ×3 на выходе.
    fonts = (
        ImageFont.truetype(FONT_B, 17),
        ImageFont.truetype(FONT_B, 15),
        ImageFont.truetype(FONT, 8),
        ImageFont.truetype(FONT, 6),
    )
    dummy = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    ranked = sorted(pools, key=lambda p: abs((p.get("change") or {}).get("h24") or 0), reverse=True)
    ranked = ranked[:config.VIDEOS_TOP_N]
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_dir = os.path.join(config.VIDEOS_DIR, day)
    latest = os.path.join(config.VIDEOS_DIR, "latest")
    os.makedirs(day_dir, exist_ok=True)
    os.makedirs(latest, exist_ok=True)
    for old in os.listdir(latest):
        os.remove(os.path.join(latest, old))

    manifest = []
    for p in ranked:
        closes = [c[4] for c in (p.get("ohlcv_h1") or [])]
        closes.reverse()
        # Выдуманную плоскую линию не рисуем: данных нет — кадр так и скажет.
        if len(closes) < 2:
            closes = []
        text, _ = narrative(p)
        fragments = [wrap(dummy, s.strip(), fonts[2], SW - 20) for s in text.split(". ") if s.strip()][:3]

        with tempfile.TemporaryDirectory() as td:
            for fno in range(FR * SEC):
                render_frame(p, fno / FR, closes, fragments, fonts, os.path.join(td, f"f{fno:04d}.png"))
            sym = (p.get("base_symbol") or "TOKEN").replace("/", "-").replace(" ", "")
            out = os.path.join(day_dir, f"{sym}.mp4")
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-framerate", str(FR), "-i", os.path.join(td, "f%04d.png"),
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", out,
            ], check=True)
        shutil.copy(out, os.path.join(latest, f"{sym}.mp4"))
        c24 = (p.get("change") or {}).get("h24") or 0
        manifest.append({
            "file": f"videos/{day}/{sym}.mp4",
            "latest_file": f"videos/latest/{sym}.mp4",
            "symbol": p.get("base_symbol"),
            "chg_h24": c24,
        })
        print(f"  video: {sym} ({format_pct(c24)})")

    with open(os.path.join(latest, "manifest.json"), "w") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "videos": manifest}, f, ensure_ascii=False, indent=1)
    print(f"OK: {len(manifest)} видео -> videos/{day}/ + videos/latest/")


if __name__ == "__main__":
    main()
