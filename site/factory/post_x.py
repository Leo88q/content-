#!/usr/bin/env python3
"""Шаг 7 фабрики: автопостинг в X (Twitter) через официальный API v2.

Использует официальный API v2 (POST /2/tweets) и медиа-аплоад v1.1.
Правило безопасности: только официальный API разработчика, никаких серых
сессий или паролей.

Требует секретов в GitHub Secrets / окружении:
  TWITTER_API_KEY
  TWITTER_API_SECRET
  TWITTER_ACCESS_TOKEN
  TWITTER_ACCESS_SECRET

Если секреты не заданы — вежливо пишет SKIP и завершается с 0
(контент остаётся в очереди site/queue.html для ручной публикации в 1 клик).
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config


def get_oauth_header(method, url, params, consumer_key, consumer_secret, token, token_secret):
    """Генерация заголовка OAuth 1.0a без внешних зависимостей."""
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": hashlib.sha256(str(time.time()).encode()).hexdigest()[:32],
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    all_params = dict(oauth_params)
    all_params.update(params)

    # Нормализация параметров
    sorted_params = sorted(all_params.items())
    param_str = "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted_params)

    # Base string
    base_elems = [method.upper(), urllib.parse.quote(url, safe=""), urllib.parse.quote(param_str, safe="")]
    base_str = "&".join(base_elems)

    # Signing key
    signing_key = f"{urllib.parse.quote(consumer_secret, safe='')}&{urllib.parse.quote(token_secret, safe='')}"
    signature = hmac.new(signing_key.encode(), base_str.encode(), hashlib.sha1).digest()
    oauth_params["oauth_signature"] = base64.b64encode(signature).decode()

    header_parts = [f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"' for k, v in sorted(oauth_params.items())]
    return "OAuth " + ", ".join(header_parts)


def upload_media(image_path, ck, cs, at, ats):
    """Загрузка PNG-карточки через upload.twitter.com/1.1/media/upload.json."""
    url = "https://upload.twitter.com/1.1/media/upload.json"
    with open(image_path, "rb") as f:
        img_bytes = f.read()

    b64_data = base64.b64encode(img_bytes).decode("utf-8")
    post_data = urllib.parse.urlencode({"media_data": b64_data}).encode("utf-8")

    auth_header = get_oauth_header("POST", url, {}, ck, cs, at, ats)
    req = urllib.request.Request(url, data=post_data, headers={
        "Authorization": auth_header,
        "Content-Type": "application/x-www-form-urlencoded"
    })

    with urllib.request.urlopen(req, timeout=30) as resp:
        res = json.loads(resp.read().decode())
        return str(res["media_id_string"])


def post_tweet(text, media_id, ck, cs, at, ats):
    """Создание твита через POST https://api.twitter.com/2/tweets."""
    url = "https://api.twitter.com/2/tweets"
    payload = {"text": text}
    if media_id:
        payload["media"] = {"media_ids": [media_id]}

    data_bytes = json.dumps(payload).encode("utf-8")
    auth_header = get_oauth_header("POST", url, {}, ck, cs, at, ats)
    req = urllib.request.Request(url, data=data_bytes, headers={
        "Authorization": auth_header,
        "Content-Type": "application/json"
    })

    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    ck = os.environ.get("TWITTER_API_KEY")
    cs = os.environ.get("TWITTER_API_SECRET")
    at = os.environ.get("TWITTER_ACCESS_TOKEN")
    ats = os.environ.get("TWITTER_ACCESS_SECRET")

    if not all([ck, cs, at, ats]):
        print(
            "SKIP: TWITTER_* ключи не настроены в Secrets. "
            "Дайджест и карточка готовы для 1-клик публикации через site/queue.html"
        )
        return

    # 1. Читаем свежий дайджест
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    txt_path = os.path.join(config.DIGESTS_DIR, f"{day}.txt")
    if not os.path.exists(txt_path):
        print(f"SKIP: файл дайджеста {txt_path} не найден", file=sys.stderr)
        return

    with open(txt_path, "r", encoding="utf-8") as f:
        text = f.read().strip()

    # Ограничение твита до 280 символов (если дайджест длиннее, обрезаем с сохранением ссылки)
    if len(text) > 275:
        lines = text.split("\n")
        short_lines = [lines[0]]
        for line in lines[1:]:
            if line.startswith("⚡") or line.startswith("https://"):
                short_lines.append(line)
            elif len("\n".join(short_lines + [line])) < 220:
                short_lines.append(line)
        text = "\n".join(short_lines)

    # 2. Ищем карточку
    media_id = None
    manifest_path = os.path.join(config.CARDS_DIR, "latest", "manifest.json")
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            cards = m.get("cards", [])
            if cards:
                top_card = cards[0]
                img_rel = top_card.get("latest_file") or top_card.get("file")
                img_path = os.path.join(config.SITE_DIR, img_rel)
                if os.path.exists(img_path):
                    print(f"Загрузка медиа {img_path} в Twitter...")
                    media_id = upload_media(img_path, ck, cs, at, ats)
                    print(f"Медиа загружено: id={media_id}")
        except Exception as e:
            print(f"Предупреждение: ошибка загрузки медиа ({e}), публикуем текстовый твит")

    # 3. Публикуем
    try:
        res = post_tweet(text, media_id, ck, cs, at, ats)
        tweet_id = res.get("data", {}).get("id")
        print(f"OK: Твит опубликован! ID: {tweet_id} (https://twitter.com/i/web/status/{tweet_id})")
    except Exception as e:
        print(f"Ошибка публикации твита: {e}", file=sys.stderr)
        # Не ломаем CI рана, если Twitter API отклонил дубль или рейтлимит
        return


if __name__ == "__main__":
    main()
