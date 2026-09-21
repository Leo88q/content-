"""Общая конфигурация фабрики контента."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.dirname(HERE)                      # site/
DATA_DIR = os.path.join(SITE_DIR, "data")
CARDS_DIR = os.path.join(SITE_DIR, "cards")
DIGESTS_DIR = os.path.join(SITE_DIR, "digests")
POOLS_DIR = os.path.join(SITE_DIR, "pools")
VS_DIR = os.path.join(SITE_DIR, "vs")
FONTS_DIR = os.path.join(HERE, "fonts")

SNAPSHOT = os.path.join(DATA_DIR, "snapshot.json")
REGISTRY = os.path.join(DATA_DIR, "registry.json")

# Публичный URL сайта (GitHub Pages). Заменить на собственный домен,
# когда появится: от него зависят canonical, водяные знаки и ссылки дайджеста.
SITE_URL = os.environ.get("TALKCHART_SITE_URL", "https://leo88q.github.io/content-/site")

GT = "https://api.geckoterminal.com/api/v2"
NETWORK = "solana"

REGISTRY_CAP = 300          # сколько пулов держать в реестре (SEO long-tail)
CARDS_TOP_N = 8             # карточек за прогон
CARDS_KEEP_DAYS = 14        # pruning каталогов карточек
DIGESTS_KEEP_DAYS = 30      # pruning дайджестов
VIDEOS_DIR = os.path.join(SITE_DIR, "videos")
VIDEOS_KEEP_DAYS = 7        # mp4 тяжелее карточек — держим меньше
VIDEOS_TOP_N = 3            # вертикальных mp4 за прогон
WHALE_MIN_USD = 25_000      # абсолютный уровень метки «whale»
WHALE_TAPE_MULT = 10        # крупная сделка = >=10× медианы ленты (калибровка 2026-09-21)
WHALE_NOISE_FLOOR_USD = 250 # пол от мелкого шума
WHALE_POOLS_N = 8           # скольким пулам за прогон снимаем ленту сделок
GT_RATE_SLEEP = 1.2         # сек между запросами API (free tier: 30/мин)

WATERMARK = "📈 talkchart — графики, которые разговаривают"
# Для PNG-рендера (Pillow/DejaVu без emoji-глифов — был бы «тофу»):
CARD_MARK = "talkchart — графики, которые разговаривают"
