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
from narrative import fmt_pct, fmt_usd, narrative

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:
    PIL_OK = False

FR, SEC = 12, 15                 # 12 fps, 15 секунд = 180 кадров
W, H = 540, 960                  # вертикаль 9:16
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


def render_frame(p, t, closes, fragments, fonts, path):
    f_sym, f_pct, f_cap, f_small = fonts
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([6, 6, W - 7, H - 7], outline=ACC, width=3)

    c24 = (p.get("change") or {}).get("h24") or 0
    col = GREEN if c24 >= 0 else RED
    d.text((40, 44), p.get("base_symbol", "?"), font=f_sym, fill=WHITE)
    d.text((40, 116), fmt_pct(c24), font=f_pct, fill=col)
    d.text((40, 182), f'{p.get("name", "")} · Vol {fmt_usd(p.get("volume_h24"))}', font=f_small, fill=MUT)

    # прогрессивный спарклайн: линия «дорисовывается» за первые 10 секунд
    if len(closes) > 1:
        n = max(2, int(len(closes) * min(1.0, t / 10.0)))
        seg = closes[:n]
        hi, lo = max(closes), min(closes)
        span = hi - lo or hi * 0.01 or 1
        pts = [(40 + (i / (len(closes) - 1)) * (W - 80),
                560 - ((c - lo) / span) * 260) for i, c in enumerate(seg)]
        d.line(pts, fill=col, width=6, joint="curve")
        if pts:
            x, y = pts[-1]
            d.ellipse([x - 8, y - 8, x + 8, y + 8], fill=col)

    # субтитры-нарратив: фрагмент i появляется с t >= 1.5 + 3.5*i
    y = 640
    for i, frag in enumerate(fragments[:3]):
        if t < 1.5 + 3.5 * i:
            break
        for ln in frag[:2]:
            d.text((40, y), ln, font=f_cap, fill=WHITE)
            y += 40
        y += 16

    d.text((40, H - 60), config.CARD_MARK, font=f_small, fill=ACC)
    img.save(path, "PNG")


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

    fonts = (
        ImageFont.truetype(FONT_B, 56),
        ImageFont.truetype(FONT_B, 48),
        ImageFont.truetype(FONT, 30),
        ImageFont.truetype(FONT, 22),
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
        if len(closes) < 4:
            closes = [p.get("price_usd") or 1] * 8  # плоская линия лучше, чем ничего
        text, _ = narrative(p)
        fragments = [wrap(dummy, s.strip(), fonts[2], W - 90) for s in text.split(". ") if s.strip()][:3]

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
        print(f"  video: {sym} ({fmt_pct(c24)})")

    with open(os.path.join(latest, "manifest.json"), "w") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "videos": manifest}, f, ensure_ascii=False, indent=1)
    print(f"OK: {len(manifest)} видео -> videos/{day}/ + videos/latest/")


if __name__ == "__main__":
    main()
