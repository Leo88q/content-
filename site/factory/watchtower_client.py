#!/usr/bin/env python3
"""Клиент телеметрии фабрики: emits TrafficError (и только его).

Зачем отдельный модуль: конвейер фабрики (fetch_data.py, make_cards.py, …)
работает в GitHub Actions и не имеет доступа к БД экспортёра. Единственный
разрешённый путь записи — тот же самый `POST /api/track`, что и у браузера.
Поэтому ошибка конвейера попадает ровно туда же, куда и пользовательские
события, и попадает в общий каталог.

Правила
-------
1. **Никогда не ломаем конвейер.** Любой сбой отправки проглатывается: падение
   фабрики из-за телеметрии — худший из возможных исходов.
2. **Молчим, если не настроено.** Нет `TRAFFICGEN_TRACK_URL` — нет отправки.
3. **Без PII.** В payload уходят только стадия, тип и обрезанное сообщение.
   URL очищается от query-параметров, потому что в них бывают ключи.
4. **Событие типа TrafficError** — именно то, что раньше числилось
   unavailable «за неимением точки эмиссии». Точка появилась здесь.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_TRACK_URL = os.environ.get("TRAFFICGEN_TRACK_URL", "http://127.0.0.1:8000/api/track")
TIMEOUT_SECONDS = float(os.environ.get("TRAFFICGEN_TRACK_TIMEOUT", "5"))
MAX_MESSAGE = 240


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def scrub_url(url: str) -> str:
    """Обрезает query-строку: в ней могут быть ключи и идентификаторы."""
    if not isinstance(url, str):
        return ""
    return url.split("?", 1)[0][:200]


def _session_id() -> str:
    """Стабильный в пределах прогона идентификатор: GITHUB_RUN_ID или хост."""
    run = os.environ.get("GITHUB_RUN_ID") or os.environ.get("HOSTNAME") or "local"
    return "sess_system_factory_" + hashlib.sha256(run.encode()).hexdigest()[:10]


def emit_traffic_error(stage: str, error, context=None, url: str = "") -> bool:
    """Отправить событие TrafficError. Возвращает True, если принято.

    `stage` — стадия конвейера ('fetch_data', 'make_cards', 'post_x', …).
    """
    if not DEFAULT_TRACK_URL:
        return False
    message = str(error)[:MAX_MESSAGE]
    payload = {
        "stage": stage,
        "errorType": type(error).__name__ if not isinstance(error, str) else "ReportedError",
        "message": message,
        "url": scrub_url(url),
    }
    if context:
        try:
            payload["context"] = json.loads(json.dumps(context))  # только JSON-сериализуемое
        except Exception:
            payload["context"] = {"repr": repr(context)[:120]}
    if os.environ.get("GITHUB_RUN_ID"):
        payload["ciRunId"] = os.environ["GITHUB_RUN_ID"]

    event = {
        "eventType": "TrafficError",
        "campaignId": "system",
        "pageId": "target_terminal",
        "sessionId": _session_id(),
        "sourceId": "factory_pipeline",
        "sourceType": "bot",
        "seq": int(datetime.now(timezone.utc).timestamp() * 1000),
        "timestamp": now_utc_iso(),
        "payload": payload,
    }
    return send(event)


def send(event: dict) -> bool:
    if not DEFAULT_TRACK_URL:
        return False
    try:
        req = urllib.request.Request(
            DEFAULT_TRACK_URL,
            data=json.dumps(event).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


if __name__ == "__main__":
    # Ручная проверка: python3 site/factory/watchtower_client.py "fetch_data" "timeout"
    import sys

    stage = sys.argv[1] if len(sys.argv) > 1 else "manual"
    msg = sys.argv[2] if len(sys.argv) > 2 else "manual smoke"
    ok = emit_traffic_error(stage, RuntimeError(msg), {"manual": True})
    print(f"отправлено: {ok} ({DEFAULT_TRACK_URL})")
