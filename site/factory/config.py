"""Общая конфигурация фабрики контента."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.dirname(HERE)                      # site/
DATA_DIR = os.path.join(SITE_DIR, "data")
CARDS_DIR = os.path.join(SITE_DIR, "cards")
DIGESTS_DIR = os.path.join(SITE_DIR, "digests")
POOLS_DIR = os.path.join(SITE_DIR, "pools")
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
GT_RATE_SLEEP = 1.2         # сек между запросами API (free tier: 30/мин)

WATERMARK = "📈 talkchart — графики, которые разговаривают"
# Для PNG-рендера (Pillow/DejaVu без emoji-глифов — был бы «тофу»):
CARD_MARK = "talkchart — графики, которые разговаривают"
