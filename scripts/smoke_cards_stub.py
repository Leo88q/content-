#!/usr/bin/env python3
"""Смоук make_cards.py без Pillow: стаб PIL проверяет логику конвейера
(ранжирование, имена файлов, manifest, каталоги). Настоящий рендер PNG
проверяется в CI (ubuntu runner, pillow==10.4.0)."""
import json
import os
import sys
import types

calls = {"text": 0, "draw": 0, "save": 0, "resize": 0}

# --- стаб PIL ---
class FakeFont:
    def __init__(self, size): self.size = size

class FakeDraw:
    def text(self, xy, s, font=None, fill=None):
        assert isinstance(s, str) and s, "пустой текст в карточке"
        calls["text"] += 1
    def textlength(self, s, font=None): return len(s) * 12.0
    def rectangle(self, *a, **k): calls["draw"] += 1
    def rounded_rectangle(self, *a, **k): calls["draw"] += 1
    def ellipse(self, *a, **k): calls["draw"] += 1
    def point(self, *a, **k): calls["draw"] += 1
    def line(self, pts, **k):
        assert len(pts) >= 2, "спарклайн без точек"
        calls["draw"] += 1

class FakeImage:
    """Мини-эмуляция PIL.Image для пиксель-конвейера (resize/quantize/convert)."""
    def __init__(self, size=(1200, 630)): self.size = size
    def resize(self, size, resample=None):
        calls["resize"] += 1
        return FakeImage(tuple(size))
    def putpalette(self, data, rawmode=None): self._palette = data
    def quantize(self, **k): return self
    def convert(self, mode=None, **k): return self
    def save(self, path, fmt=None, **k):
        assert path.endswith(".png")
        calls["save"] += 1
        with open(path, "wb") as f:
            f.write(b"stub-png")

pil = types.ModuleType("PIL")
pil_image = types.ModuleType("PIL.Image")
pil_image.new = lambda *a, **k: FakeImage()
pil_image.NEAREST = 0        # константы ресемплинга: в стабе они не важны,
pil_image.LANCZOS = 1        # важен сам факт вызова resize()


class _Dither:                # режимы дизеринга (для quantize(dither=...))
    NONE = 0
    FLOYDSTEINBERG = 1


pil_image.Dither = _Dither
pil_draw = types.ModuleType("PIL.ImageDraw")
pil_draw.Draw = lambda img: FakeDraw()
pil_font = types.ModuleType("PIL.ImageFont")
pil_font.truetype = lambda path, size: FakeFont(size)
pil.Image, pil.ImageDraw, pil.ImageFont = pil_image, pil_draw, pil_font
sys.modules["PIL"] = pil
sys.modules["PIL.Image"] = pil_image
sys.modules["PIL.ImageDraw"] = pil_draw
sys.modules["PIL.ImageFont"] = pil_font

# --- прогон фабрики ---
here = os.path.dirname(os.path.abspath(__file__))
factory = os.path.join(here, "..", "site", "factory")
sys.path.insert(0, factory)

import fetch_data  # noqa: E402  (импортируемость модуля)
fetch_data.main.__defaults__ = None
sys.argv = ["fetch_data.py", "--offline"]
fetch_data.main()

sys.path.insert(0, os.path.join(here, "..", "site"))
import build_seo  # noqa: E402
build_seo.main()

import make_digest  # noqa: E402
make_digest.main()

import make_cards  # noqa: E402
make_cards.main()

import make_vs  # noqa: E402
make_vs.main()

import make_videos  # noqa: E402
make_videos.main()  # без Pillow/ffmpeg — вежливый скип внутри main()

import post_x  # noqa: E402
post_x.main()  # вежливый скип без TWITTER_* секретов

import prune  # noqa: E402
prune.main()

# --- проверки результата ---
import config  # noqa: E402
manifest = json.load(open(os.path.join(config.CARDS_DIR, "latest", "manifest.json")))
assert manifest["cards"], "манифест пуст"
assert calls["save"] >= len(manifest["cards"]), (calls["save"], len(manifest["cards"]))
assert all(c["file"].startswith("cards/") for c in manifest["cards"])
assert sum(1 for c in manifest["cards"] if c.get("latest_file")) == min(3, len(manifest["cards"]))
latest_pngs = [f for f in os.listdir(os.path.join(config.CARDS_DIR, "latest")) if f.endswith(".png")]
assert len(latest_pngs) == min(3, len(manifest["cards"])), latest_pngs
digests = os.listdir(config.DIGESTS_DIR)
assert any(d.endswith(".txt") for d in digests) and any(d.endswith(".md") for d in digests), digests
seo_pages = [f for f in os.listdir(config.POOLS_DIR) if f != "index.html"]
assert len(seo_pages) >= 5, seo_pages
assert calls["text"] >= len(manifest["cards"]) * 6, calls  # >=6 текстовых элементов на карточку

print(f"SMOKE STUB OK: карточек {calls['save']}, текстовых вызовов {calls['text']}, "
      f"SEO-страниц {len(seo_pages)}, дайджесты {digests}, prune отработал")
