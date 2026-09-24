#!/usr/bin/env python3
"""Шаг фабрики: публикация дневного дайджеста в X (Twitter), API v2.

Только официальный API разработчика (POST /2/tweets + media upload v1.1), никаких
серых сессий и паролей. Секреты — из окружения CI (GitHub Secrets):
  TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET

Что изменено по сравнении с «публикуем каждый прогон»:
  • идемпотентность: фабрика запускается по cron каждые 4 часа, а дайджест один на день.
    Журнал site/data/x_posts.json хранит ключ отправленного текста — повторный прогон
    даёт SKIP вместо второго одинакового твита (--force перебивает);
  • dry-run: `--dry-run` (или X_POST_DRY_RUN=1) собирает текст и считает длину,
    но в сеть не ходит — так можно репетировать креатив;
  • ретраи только на преходящее (429/5xx) с backoff'ом и уважением Retry-After /
    x-rate-limit-reset; 4xx — постоянная ошибка, не долбим API;
  • длина по правилам X: URL считается 23 символами, не-Latin-символы — двумя;
    обрезка не рвёт ссылку и сохраняет строку со ссылкой;
  • ничего не логируется из секретов; в журнале только tweet id, длина и ключ.

Код возврата: 0 — опубликовано или пропущено; 2 — публикация не удалась
(в CI шаг с continue-on-error, чтобы падение X API не ломало сборку контента;
для строгих прогонов --strict → тоже ненулевой выход, как и без флага).
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

LEDGER_PATH = os.path.join(config.DATA_DIR, "x_posts.json")
API_TWEETS = "https://api.twitter.com/2/tweets"
API_MEDIA = "https://upload.twitter.com/1.1/media/upload.json"
MAX_WEIGHTED = 280
URL_WEIGHT = 23
URL_RE = re.compile(r"https?://\S+")


# ───────────────────────────────────────────────────── длина по правилам X ──
def weighted_len(text):
    """Длина по весам X: URL → 23, «лёгкие» символы (U+0000…U+10FF: латиница, греческий,
    кириллица, иврит, арабский) → 1, остальное (CJK, хангыль, эмодзи) → 2.
    Граница 0x10FF — из публичной конфигурации весов API, а не на глаз."""
    total, index = 0, 0
    while index < len(text):
        match = URL_RE.match(text, index)
        if match:
            total += URL_WEIGHT
            index = match.end()
            continue
        total += 1 if ord(text[index]) <= 0x10FF else 2
        index += 1
    return total


def fits(text):
    return weighted_len(text) <= MAX_WEIGHTED


def clamp_digest(text, limit=MAX_WEIGHTED):
    """Сокращает дайджест до лимита X, сохраняя первую строку и строку со ссылкой."""
    if fits(text):
        return text, False
    lines = text.split("\n")
    head = lines[0]
    keep = [line for line in lines[1:] if line.startswith("https://") or line.startswith("⚡")]
    body = [line for line in lines[1:] if line not in keep]
    out = head
    tail = "\n".join(keep)
    for line in body:
        candidate = f"{out}\n{line}" if out else line
        # greedy: строку длиннее лимита пропускаем, но следующие пробуем взять —
        # иначе обрезка съедала половину бюджета (обрывки строк нам не нужны)
        if fits(f"{candidate}\n{tail}" if tail else candidate):
            out = candidate
    if keep:
        out = f"{out}\n" + "\n".join(keep)
    while not fits(out) and len(out) > 10:
        head, sep, tail_line = out.rpartition("\n")
        if not sep:      # одна строка длиннее лимита: резать по строкам больше нечего
            break
        out = head       # режем по строкам: ссылку не рвём
    if not fits(out):
        # последняя защита: жёсткая обрезка с эллипсисом (такое возможно, только если одна строка длиннее лимита)
        while out and not fits(out + "…"):
            out = out[:-1]
        out += "…"
    return out, True


# ────────────────────────────────────────────────────────────── журнал ──────
def load_ledger():
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"entries": []}
    except (OSError, json.JSONDecodeError):
        return {"entries": []}


def save_ledger(ledger):
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, LEDGER_PATH)


def post_key(text, media_id=None):
    return hashlib.sha256(f"{text}|{media_id or ''}".encode("utf-8")).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────── API ────────
def get_oauth_header(method, url, params, consumer_key, consumer_secret, token, token_secret):
    """OAuth 1.0a HEADER — подпись тела JSON в signature не входят (так и требует X)."""
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": hashlib.sha256(os.urandom(32)).hexdigest()[:32],
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    all_params = dict(oauth_params)
    all_params.update(params or {})
    param_str = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(all_params.items())
    )
    base_str = "&".join([method.upper(), urllib.parse.quote(url, safe=""), urllib.parse.quote(param_str, safe="")])
    signing_key = (f"{urllib.parse.quote(consumer_secret, safe='')}&"
                   f"{urllib.parse.quote(token_secret, safe='')}")
    signature = hmac.new(signing_key.encode(), base_str.encode(), hashlib.sha1).digest()
    oauth_params["oauth_signature"] = base64.b64encode(signature).decode()
    return "OAuth " + ", ".join(
        f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"'
        for k, v in sorted(oauth_params.items())
    )


def _request(url, auth, data=None, content_type="application/json", method="POST"):
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": auth,
        "Content-Type": content_type,
    })
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())


class Transient(Exception):
    """Надо повторить: 429 или 5xx."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def call_with_retries(fn, max_retries=3, base_sleep=2.0):
    """Повторяем только преходящее. Постоянное (4xx кроме 429) поднимаем сразу."""
    attempt = 0
    while True:
        try:
            return fn()
        except Transient as err:
            attempt += 1
            if attempt > max_retries:
                raise
            sleep_for = err.retry_after or min(60.0, base_sleep * (2 ** (attempt - 1)))
            print(f"  ретрай {attempt}/{max_retries} через {sleep_for:.0f}с: {err}", file=sys.stderr)
            time.sleep(sleep_for)


def upload_media(image_path, creds):
    """Возвращает media_id_string. creds: consumer_key/consumer_secret/token/token_secret (+ _retries)."""
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    body = urllib.parse.urlencode({"media_data": b64}).encode()
    auth = get_oauth_header("POST", API_MEDIA, {}, creds["consumer_key"], creds["consumer_secret"],
                          creds["token"], creds["token_secret"])

    def once():
        try:
            return _request(API_MEDIA, auth, data=body, content_type="application/x-www-form-urlencoded")
        except urllib.error.HTTPError as e:
            wrapped = _wrap_http(e)
            if not isinstance(wrapped, Transient):  # 4xx: картинку не переслать — пусть решает вызывающий
                raise RuntimeError(f"X API отклонил загрузку медиа: HTTP {e.code}")
            raise wrapped

    res = call_with_retries(once, max_retries=creds.get("_retries", 3))
    return str(res["media_id_string"])


def _wrap_http(err):
    if err.code == 429 or 500 <= err.code < 600:
        retry_after = None
        header = err.headers.get("Retry-After") or err.headers.get("x-rate-limit-reset")
        try:
            value = float(header)
            retry_after = max(1.0, value - time.time()) if value > 1e9 else max(1.0, value)
        except (TypeError, ValueError):
            retry_after = None
        raise Transient(f"HTTP {err.code}: {err.read().decode('utf-8', 'ignore')[:200]}", retry_after)
    return err


def post_tweet(text, media_id, creds):
    payload = {"text": text}
    if media_id:
        payload["media"] = {"media_ids": [media_id]}
    body = json.dumps(payload).encode()
    auth = get_oauth_header("POST", API_TWEETS, {}, creds["consumer_key"], creds["consumer_secret"],
                          creds["token"], creds["token_secret"])

    def once():
        try:
            return _request(API_TWEETS, auth, data=body)
        except urllib.error.HTTPError as e:
            wrapped = _wrap_http(e)
            if isinstance(wrapped, Transient):
                raise wrapped
            raise RuntimeError(f"X API отклонил публикацию: HTTP {e.code} "
                               f"{e.read().decode('utf-8', 'ignore')[:200]}")

    return call_with_retries(once, max_retries=creds.get("_retries", 3))


# ───────────────────────────────────────────────────────────────── main ──────
def latest_digest(day=None):
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(config.DIGESTS_DIR, f"{day}.txt")
    return (path, day) if os.path.exists(path) else (None, day)


def latest_card_image():
    manifest = os.path.join(config.CARDS_DIR, "latest", "manifest.json")
    if not os.path.exists(manifest):
        return None
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            cards = json.load(f).get("cards", [])
    except (OSError, json.JSONDecodeError):
        return None
    if not cards:
        return None
    rel = cards[0].get("latest_file") or cards[0].get("file")
    path = os.path.join(config.SITE_DIR, rel) if rel else None
    return path if path and os.path.exists(path) else None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv or os.environ.get("X_POST_DRY_RUN") == "1"
    force = "--force" in argv
    status_only = "--status" in argv
    max_retries = int(next((a.split("=", 1)[1] for a in argv if a.startswith("--max-retries=")), 3))

    ledger = load_ledger()
    if status_only:
        print(json.dumps({"path": LEDGER_PATH, "entries": len(ledger.get("entries", [])),
                          "last": (ledger.get("entries") or [None])[-1]}, ensure_ascii=False, indent=1))
        return 0

    creds_src = {
        "consumer_key": os.environ.get("TWITTER_API_KEY"),
        "consumer_secret": os.environ.get("TWITTER_API_SECRET"),
        "token": os.environ.get("TWITTER_ACCESS_TOKEN"),
        "token_secret": os.environ.get("TWITTER_ACCESS_SECRET"),
    }
    txt_path, day = latest_digest()
    if txt_path is None:
        print(f"SKIP: дайджест за {day} не найден ({config.DIGESTS_DIR}/{day}.txt)")
        return 0
    with open(txt_path, "r", encoding="utf-8") as f:
        raw_text = f.read().strip()
    text, truncated = clamp_digest(raw_text)
    media_path = latest_card_image()

    if not all(creds_src.values()) and not dry:
        print("SKIP: TWITTER_* ключи не настроены. Дайджест и карточка готовы для "
              "1-клик публикации через site/queue.html")
        return 0

    key = post_key(text, "media" if media_path else "text")
    if not force and any(e.get("key") == key for e in ledger.get("entries", [])):
        print(f"SKIP: этот текст уже опубликован (ключ {key}) — повторный прогон фабрики твит не дублирует. "
              f"Нужно отправить заново: --force")
        return 0

    print(f"Дайджест {day}: {weighted_len(text)}/{MAX_WEIGHTED} взвешенных символов"
          f"{' (сокращено до лимита)' if truncated else ''}, медиа: "
          f"{os.path.relpath(media_path, config.SITE_DIR) if media_path else 'нет'}")

    if dry:
        print("DRY-RUN: в сеть не ходили. Тело запроса:")
        print(json.dumps({"text": text, "media": [os.path.basename(media_path)] if media_path else []},
                         ensure_ascii=False, indent=1))
        return 0

    creds = {**creds_src, "_retries": max_retries}
    media_id = None
    if media_path:
        try:
            print(f"Загрузка медиа {os.path.basename(media_path)}…")
            media_id = upload_media(media_path, creds)
        except Exception as err:  # текст важнее картинки: продолжаем без медиа
            print(f"ПРЕДУПРЕЖДЕНИЕ: медиа не загружено ({err}), публикуем текст", file=sys.stderr)
            media_id = None

    try:
        res = post_tweet(text, media_id, creds)
    except Transient as err:
        print(f"ОШИБКА: X API недоступен после {max_retries} ретраев: {err}", file=sys.stderr)
        ledger.setdefault("lastError", {})
        ledger["lastError"] = {"at": datetime.now(timezone.utc).isoformat(), "message": str(err)}
        save_ledger(ledger)
        return 2
    except Exception as err:
        print(f"ОШИБКА: {err}", file=sys.stderr)
        ledger["lastError"] = {"at": datetime.now(timezone.utc).isoformat(), "message": str(err)}
        save_ledger(ledger)
        return 2

    tweet_id = (res.get("data") or {}).get("id")
    ledger.setdefault("entries", []).append({
        "key": key, "day": day, "tweetId": tweet_id,
        "url": f"https://x.com/i/web/status/{tweet_id}" if tweet_id else None,
        "weightedChars": weighted_len(text), "media": bool(media_id),
        "postedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    save_ledger(ledger)
    print(f"OK: твит {tweet_id} опубликован (записано в {os.path.relpath(LEDGER_PATH, config.SITE_DIR)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
