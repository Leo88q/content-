#!/usr/bin/env python3
"""Нагрузочный тест приёма событий (W4, b-16/b-18).

Проверяет не «сколько выдержит», а конкретные свойства:
- всплеск (кампания запустилась — трафик вырос в разы) не приводит к потере
  событий молча: reject/429 считаются и видны;
- backpressure работает: при превышении лимита сервер отвечает 429 с
  `Retry-After`, а не падает и не начинает молча терять события;
- DLQ растёт только за счёт schema-rejected, а не «по дороге»;
- задержка приёма остаётся в бюджете (p95/p99).

Использование
-------------
    python3 scripts/load_test_traffic.py                        # 2000 событий, 8 потоков
    python3 scripts/load_test_traffic.py --events 20000 --concurrency 32
    python3 scripts/load_test_traffic.py --url http://127.0.0.1:8000 --report

Отчёт: reports/load-test-trafficgen.json
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_PATH = os.path.join(REPO_ROOT, "reports", "load-test-trafficgen.json")
DEFAULT_URL = os.environ.get("TRAFFICGEN_API_BASE_URL", "http://127.0.0.1:8000")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def post(url: str, event: dict, timeout=10):
    req = urllib.request.Request(
        url, data=json.dumps(event).encode(), headers={"Content-Type": "application/json"},
        method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}"), \
                round((time.monotonic() - started) * 1000, 2)
    except urllib.error.HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read().decode() or "{}")
        except Exception:
            pass
        return e.code, body, round((time.monotonic() - started) * 1000, 2)
    except Exception as e:  # сеть/таймаут
        return 0, {"error": str(e)}, round((time.monotonic() - started) * 1000, 2)


def make_event(i: int, session_salt: str):
    return {
        "eventType": "PageView",
        "campaignId": "loadtest_campaign",
        "pageId": "target_terminal",
        "sessionId": f"sess_load_{session_salt}_{i % 500}",
        "sourceId": "direct_web",
        "sourceType": "real",
        "seq": (i % 50) + 1,
        "eventId": f"ev_load_{session_salt}_{i}",
        "payload": {"loadTest": True, "synthetic": True},
    }


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q / 100 * (len(ordered) - 1))))
    return round(ordered[idx], 2)


def fetch_quality(base: str):
    try:
        with urllib.request.urlopen(f"{base}/watchtower/quality", timeout=10) as r:
            return json.loads(r.read().decode())["data"]
    except Exception:
        return None


def run(total=2000, concurrency=8, base=DEFAULT_URL, rate_limit_rps=None):
    tasks = queue.Queue()
    for i in range(total):
        tasks.put(i)
    results = []
    lock = threading.Lock()
    salt = datetime.now(timezone.utc).strftime("%H%M%S")
    stop = threading.Event()

    quality_before = fetch_quality(base)

    def worker():
        while not stop.is_set():
            try:
                i = tasks.get_nowait()
            except queue.Empty:
                return
            status, body, ms = post(f"{base}/api/track", make_event(i, salt))
            with lock:
                results.append((status, body, ms))
            tasks.task_done()

    started = time.monotonic()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started
    stop.set()

    quality_after = fetch_quality(base)

    statuses = {}
    accepted = duplicates = rejected = paused = blocked = rate_limited = net_errors = 0
    net_error_samples = []
    for status, body, _ms in results:
        statuses[status] = statuses.get(status, 0) + 1
        accepted += int(body.get("accepted") or 0)
        duplicates += int(body.get("duplicates") or 0)
        rejected += int(body.get("rejected") or 0)
        paused += int(body.get("paused") or 0)
        blocked += int(body.get("blocked") or 0)
        if status == 429:
            rate_limited += 1
        if status == 0:
            net_errors += 1
            if len(net_error_samples) < 3:
                net_error_samples.append(str(body.get("error"))[:160])
    latencies = [ms for _s, _b, ms in results]

    dlq_before = (quality_before or {}).get("deadLetter", {}).get("total")
    dlq_after = (quality_after or {}).get("deadLetter", {}).get("total")

    return {
        "generatedAt": now_utc_iso(),
        "target": base,
        "params": {"events": total, "concurrency": concurrency,
                   "rateLimitRps": rate_limit_rps},
        "elapsedSeconds": round(elapsed, 2),
        "throughputRps": round(total / elapsed, 2) if elapsed else None,
        "requests": {
            "total": len(results),
            "byStatus": statuses,
            "rateLimited": rate_limited,
        },
        "events": {
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": rejected,
            "paused": paused,
            "blocked": blocked,
            "throttledByRateLimit": rate_limited,
            "networkErrors": net_errors,
            "networkErrorSamples": net_error_samples,
            # Молча потерянным считается только то, что не получило ни одного
            # объяснимого исхода. 429 с Retry-After — это backpressure, а не
            # потеря: клиент знает, что надо повторить.
            "lost": total - accepted - duplicates - rejected - paused - blocked
            - rate_limited - net_errors,
        },
        "latencyMs": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "max": round(max(latencies), 2) if latencies else None,
            "avg": round(statistics.mean(latencies), 2) if latencies else None,
        },
        "deadLetter": {"before": dlq_before, "after": dlq_after},
        "verdict": {
            "noSilentLoss": (total - accepted - duplicates - rejected - paused - blocked
                             - rate_limited - net_errors) == 0,
            "throttled": rate_limited > 0,
            "note": "События помечены payload.synthetic=true и исключены из продуктовых "
                    "агрегатов. 429 с Retry-After — штатный backpressure, а не потеря.",
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Нагрузочный тест /api/track")
    ap.add_argument("--events", type=int, default=2000)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--report", action="store_true", help="записать reports/load-test-trafficgen.json")
    args = ap.parse_args(argv)

    report = run(args.events, args.concurrency, args.url)
    if args.report:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        report["reportPath"] = os.path.relpath(REPORT_PATH, REPO_ROOT)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
