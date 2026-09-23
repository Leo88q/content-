#!/usr/bin/env python3
"""Пиксель-рендер артефактов фабрики: карточки, кадры видео, баттл-карточки.

Идея одна для всех артефактов: **рисуем маленько, отдаём крупно**.

1. Сцена рисуется в низком разрешении (в `scale` раз меньше итогового).
2. Затем NEAREST-апскейл — получаем «квадратные» пиксели, а не мыло.
3. Затем приведение к фиксированной палитре БЕЗ дизеринга: это убирает
   полутона антиалиасинга на краях текста и линий. Без этого шага картинка
   остаётся «увеличением», а не пиксель-артом.

Палитра общая для всех артефактов — это и есть визуальный язык: одни и те же
8–10 цветов в карточках, видео и на сайте.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

# --- палитра (совпадает с CSS-токенами site/style.css) ------------------------
BG = (10, 14, 20)          # --bg
PANEL = (19, 26, 38)       # --panel
WHITE = (232, 237, 242)    # --tx
MUT = (139, 152, 165)      # --mut
GREEN = (38, 208, 124)     # --green
RED = (255, 77, 106)       # --red
ACC = (124, 240, 61)       # --acc
AMBER = (251, 164, 58)     # --warn
CYAN = (61, 217, 240)      # --info
INK = (0, 0, 0)            # «чернила» для контрастных подложек

PALETTE = (BG, PANEL, WHITE, MUT, GREEN, RED, ACC, AMBER, CYAN, INK)


def pixelate(img, target_size, palette=PALETTE):
    """NEAREST-апскейл до target_size + приведение к палитре без дизеринга."""
    img = img.resize(target_size, Image.NEAREST)
    flat = []
    for color in palette:
        flat.extend(color)
    flat.extend((0, 0, 0) * (256 - len(palette)))
    pal_img = Image.new("P", (1, 1))
    pal_img.putpalette(flat)
    return img.quantize(palette=pal_img, dither=Image.Dither.NONE).convert("RGB")


def dither_bg(d, x0, y0, x1, y1, color=PANEL, cell=4):
    """Шахматный полутон: ретро-фон вместо градиента."""
    for yy in range(y0, y1, cell):
        offset = cell if ((yy - y0) // cell) % 2 else 0
        for xx in range(x0 + offset, x1, cell * 2):
            d.rectangle([xx, yy, min(xx + cell - 1, x1 - 1), min(yy + cell - 1, y1 - 1)],
                        fill=color)


def pixel_bars(d, closes, x0, y0, x1, y1, up=True, up_color=GREEN, down_color=RED):
    """Блочный спарклайн: столбики по клетке, а не сглаженная кривая."""
    if len(closes) < 2:
        return
    color = up_color if up else down_color
    hi, lo = max(closes), min(closes)
    span = hi - lo or hi * 0.01 or 1
    n = len(closes)
    step = max(1, (x1 - x0) // n)
    for i, c in enumerate(closes):
        h = max(1, int(((c - lo) / span) * (y1 - y0)))
        d.rectangle([x0 + i * step, y1 - h, x0 + i * step + step - 1, y1], fill=color)


def dashed_line(d, x0, x1, y, color=MUT, step=4, thickness=1):
    """Пунктир «в клетку»: ручная штриховка, а не сглаженный dash."""
    for xx in range(x0, x1, step * 2):
        d.rectangle([xx, y, min(xx + step - 1, x1 - 1), y + thickness - 1], fill=color)


def frame(d, w, h, color, width=1, inset=0):
    """Прямоугольная рамка: углы прямые, толщина в целых пикселях."""
    for i in range(width):
        d.rectangle([inset + i, inset + i, w - 1 - inset - i, h - 1 - inset - i], outline=color)


def blank(w, h, bg=BG):
    """Холст низкого разрешения + контекст рисования."""
    img = Image.new("RGB", (w, h), bg)
    return img, ImageDraw.Draw(img)


def wrap(draw, text, font, max_w):
    """Перенос по словам: в низком разрешении строку надо резать аккуратно."""
    lines, line = [], ""
    for word in str(text).split():
        candidate = (line + " " + word).strip()
        if draw.textlength(candidate, font=font) <= max_w:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines
