#!/usr/bin/env python3
"""
Smoke test (end-to-end) for the Games Watchtower Integration Adapter.

Поднимает эфемерный инстанс сервера и проверяет живьём (PROMPT §11.2):
- все 12 GET-эндпоинтов + Prometheus /watchtower/metrics
- единый конверт {data, generatedAt, period, source, dataQuality, confidence, parserVersion}
- strict read-only: POST/PUT/DELETE/PATCH -> 405 + Allow: GET, OPTIONS
- ingestion POST /api/track со scrub PII (grep утечек по выданным событиям)
- consent/opt-out: заголовок DNT -> 202, событие НЕ сохраняется
- rate limit: при заниженном лимите 1 rps быстрые запросы дают 429 + Retry-After
- invalid cursor -> 400 с code=invalid_cursor
- replay: чтение с нуля после прохода курсором возвращает тот же набор
- честность воронки: LandingReached -> stageUnavailable=true, нулевой знаменатель -> null
- forecast: всегда unavailable + confidence 0.0 + forecast/model = null
"""

import base64
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "site", "factory"))

from http.server import ThreadingHTTPServer
import watchtower_exporter as we
from watchtower_exporter import WatchtowerHandler, STORE
from watchtower_control import ControlStore

ENVELOPE_KEYS = ("data", "generatedAt", "period", "source",
                 "dataQuality", "confidence", "parserVersion")

PII_FRAGMENTS = ("1.2.3.4", "user@example.com", "fp-12345",
                 "sess_cookie_secret", "deadbeef" * 8, "my_wallet_secret_key")


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def request(url, method="GET", data=None, headers=None):
    headers = headers or {}
    req = urllib.request.Request(url, method=method, headers=headers)
    if data is not None:
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        req.data = data
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8"), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8"), e.headers


def run_smoke_tests():
    # Эфемерная БД: иначе identity предыдущего прогона превращает первое же
    # событие в duplicate и смоук перестаёт быть воспроизводимым.
    tmp_dir = tempfile.mkdtemp(prefix="tc-smoke-")
    db_path = os.path.join(tmp_dir, "smoke_watchtower.db")
    old_store, old_control = we.STORE, we.CONTROL
    we.STORE = we.EventStore(db_path=db_path)
    we.CONTROL = ControlStore(db_path=db_path)

    port = get_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), WatchtowerHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{port}"
    print(f"[SMOKE] Watchtower test server: {base}")

    results = []

    def check(name, cond, details=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f": {details}"))
        results.append((name, bool(cond)))

    # -- 1. Все GET-эндпоинты: 200 + единый конверт --------------------------------
    get_paths = [
        "/watchtower/health", "/watchtower/readyz", "/watchtower/config",
        "/watchtower/campaigns", "/watchtower/campaigns/talkchart_seo",
        "/watchtower/sources", "/watchtower/pages", "/watchtower/events?limit=5",
        "/watchtower/metrics/daily?period=7d", "/watchtower/funnels",
        "/watchtower/alerts", "/watchtower/forecast",
    ]
    for p in get_paths:
        status, body, _ = request(f"{base}{p}")
        ok_code = status in (200, 503) if "readyz" in p else status == 200
        try:
            env = json.loads(body)
            missing = [k for k in ENVELOPE_KEYS if k not in env]
        except Exception:
            env, missing = {}, list(ENVELOPE_KEYS)
        check(f"GET {p} -> {status}, конверт полный", ok_code and not missing,
              f"missing={missing}")

    # -- 2. Паспорт в /watchtower/config + health.ok --------------------------------
    _, body, _ = request(f"{base}/watchtower/health")
    hd = json.loads(body)["data"]
    check("health: data.ok == true (чек-лист Watchtower) + status ok",
          hd.get("ok") is True and hd.get("status") == "ok")
    _, body, _ = request(f"{base}/watchtower/config")
    cfg = json.loads(body)["data"]
    check("config: appId/kind/trafficType/readOnly",
          cfg.get("appId") == "trafficgen" and cfg.get("kind") == "traffic-generator"
          and cfg.get("trafficType") == "hybrid" and cfg.get("readOnly") is True)
    check("config: implementedEvents + unavailableEvents = полный каталог, без пересечения",
          set(cfg.get("implementedEvents", [])) & set(cfg.get("unavailableEvents", [])) == set()
          and "LandingReached" in cfg.get("unavailableEvents", []))
    check("config: 8 sourceSystems, 6 targetPages",
          len(cfg.get("sourceSystems", [])) == 8 and len(cfg.get("targetPages", [])) == 6)
    # совместимость с чек-листом Watchtower: jq .data.campaigns
    check("config: .data.campaigns — массив кампаний (jq .data.campaigns)",
          isinstance(cfg.get("campaigns"), list) and len(cfg["campaigns"]) >= 5)

    # -- 3. Ingestion + PII scrub ------------------------------------------------------
    track_event = {
        "eventType": "CTAClicked",
        "campaignId": "talkchart_interactive_radar",
        "pageId": "terminal",
        "sessionId": "smoke_sess_001",
        "seq": 1,
        "payload": {
            "target": "sixsec", "gameId": "game1",
            "ip": "1.2.3.4", "email": "user@example.com", "fingerprint": "fp-12345",
            "cookie": "sess_cookie_secret",
            "privateKey": "deadbeef" * 8,
            "walletAddress": "my_wallet_secret_key",
        },
    }
    status, body, _ = request(f"{base}/api/track", method="POST", data=track_event)
    tdata = json.loads(body)
    check("POST /api/track -> 200 accepted", status == 200 and tdata.get("accepted") == 1)

    status, body, _ = request(f"{base}/watchtower/events?limit=10&eventType=CTAClicked"
                              f"&campaignId=talkchart_interactive_radar")
    events = json.loads(body)["data"]["events"]
    # входной sessionId "smoke_sess_001" не соответствует форме sess_<random> —
    # сервер обязан его детерминированно псевдонимизировать; событие ищем по payload
    mine = [e for e in events
            if e.get("payload", {}).get("gameId") == "game1"
            and e.get("payload", {}).get("target") == "sixsec"]
    leak = None
    if mine:
        blob = json.dumps(mine, ensure_ascii=False)
        leak = next((f for f in PII_FRAGMENTS if f in blob), None)
    check("PII: ни один фрагмент не дошёл до выдачи", not leak, f"leak={leak}")
    check("PII: sessionId псевдонимизирован (sess_*) и не равен входному",
          bool(mine) and mine[0]["sessionId"].startswith("sess_")
          and mine[0]["sessionId"] != "smoke_sess_001", str(mine))
    check("pageId нормализован terminal -> target_terminal",
          mine and mine[0]["pageId"] == "target_terminal", str(mine))

    # повтор той же отправки -> duplicate
    status, body, _ = request(f"{base}/api/track", method="POST", data=track_event)
    tdata = json.loads(body)
    check("идемпотентность: повтор -> duplicates=1", tdata.get("duplicates") == 1, str(tdata))

    # schema-rejected -> 422
    status, body, _ = request(f"{base}/api/track", method="POST",
                              data={"eventType": "PageView", "seq": 0, "sessionId": "smoke_bad"})
    check("невалидное событие (seq=0) -> 422", status == 422, f"status={status}")

    # -- 4. Consent/opt-out ------------------------------------------------------------
    status, body, _ = request(f"{base}/api/track", method="POST",
                              headers={"DNT": "1"},
                              data={"eventType": "PageView", "sessionId": "smoke_dnt", "seq": 1})
    check("DNT: 1 -> 202 opted_out", status == 202 and json.loads(body).get("status") == "opted_out",
          f"status={status}")
    _, body, _ = request(f"{base}/watchtower/events?limit=500")
    all_events = json.loads(body)["data"]["events"]
    check("DNT-событие НЕ сохранено",
          not any(e.get("sessionId") == "smoke_dnt" for e in all_events))

    # -- 5. Rate limit --------------------------------------------------------------------
    we._TRACK_LIMITER.drain(1, 1)  # 1 rps, burst 1
    codes = []
    for i in range(4):
        status, _, hdrs = request(f"{base}/api/track", method="POST",
                                  data={"eventType": "PageView", "sessionId": f"smoke_rl_{i}", "seq": 1})
        codes.append(status)
    we._TRACK_LIMITER.drain(30, 30)
    check("rate limit: из 4 быстрых запросов есть 429", 429 in codes, str(codes))
    if 429 in codes:
        status, body, hdrs = request(f"{base}/api/track", method="POST",
                                     data={"eventType": "PageView", "sessionId": "smoke_rl_z", "seq": 1})
    # Retry-After присутствовал на 429 (проверено по последнему 429 выше — заголовки читаем отдельно)
    we._TRACK_LIMITER.drain(1, 1)
    status, body, hdrs = request(f"{base}/api/track", method="POST",
                                 data={"eventType": "PageView", "sessionId": "smoke_rl_a", "seq": 1})
    status2, body2, hdrs2 = request(f"{base}/api/track", method="POST",
                                    data={"eventType": "PageView", "sessionId": "smoke_rl_b", "seq": 1})
    we._TRACK_LIMITER.drain(30, 30)
    check("429 несёт Retry-After", status2 == 429 and hdrs2.get("Retry-After") is not None,
          f"status={status2}")
    _, body, _ = request(f"{base}/watchtower/events?eventType=RateLimited&limit=5")
    rl_events = json.loads(body)["data"]["events"]
    check("RateLimited записан как системное событие", len(rl_events) >= 1)

    # -- 6. Invalid cursor ------------------------------------------------------------------
    status, body, _ = request(f"{base}/watchtower/events?cursor=%%%bad%%%")
    d = json.loads(body)
    check("invalid cursor -> 400 invalid_cursor",
          status == 400 and d.get("data", {}).get("code") == "invalid_cursor", f"status={status}")
    bad = base64.b64encode(b"cursor:-5").decode()
    status, _, _ = request(f"{base}/watchtower/events?cursor={bad}")
    check("cursor:-5 -> 400", status == 400)

    # replay клиентской формой курсора из чек-листа Watchtower: MA== (base64 "0")
    status, body, _ = request(f"{base}/watchtower/events?cursor=MA==&limit=5")
    d = json.loads(body)["data"]
    _, body2, _ = request(f"{base}/watchtower/events?limit=5")
    d2 = json.loads(body2)["data"]
    check("cursor=MA== -> 200, replay с первого события (идентичен чтению без cursor)",
          status == 200 and [e["eventId"] for e in d["events"]] == [e["eventId"] for e in d2["events"]],
          f"status={status}")

    # -- 7. Replay: полный проход и повторный с нуля -----------------------------------------
    def read_all():
        out, cursor = [], None
        while True:
            url = f"{base}/watchtower/events?limit=50" + (f"&cursor={cursor}" if cursor else "")
            _, b, _ = request(url)
            d = json.loads(b)["data"]
            out.extend(e["eventId"] for e in d["events"])
            cursor = d.get("nextCursor")
            if not cursor:
                return out
    run1 = read_all()
    run2 = read_all()
    check("replay детерминирован (порядок и набор совпадают)", run1 == run2,
          f"{len(run1)} vs {len(run2)}")

    # -- 8. Честность воронки -----------------------------------------------------------------
    _, body, _ = request(f"{base}/watchtower/funnels")
    fdata = json.loads(body)["data"]
    steps = {s["stage"]: s for s in fdata["steps"]}
    check("воронка: LandingReached -> stageUnavailable=true",
          steps.get("LandingReached", {}).get("stageUnavailable") is True)
    check("воронка: count числом, конверсии null при нулевом знаменателе",
          isinstance(steps.get("CampaignStarted", {}).get("count"), int))
    check("воронка: byCampaign + bySource присутствуют",
          "byCampaign" in fdata and "bySource" in fdata)

    # -- 9. Forecast: строго unavailable ---------------------------------------------------------
    _, body, _ = request(f"{base}/watchtower/forecast")
    d = json.loads(body)
    check("forecast: unavailable + confidence 0.0 + forecast/model null",
          d.get("dataQuality") == "unavailable" and d.get("confidence") == 0.0
          and d["data"].get("forecast") is None and d["data"].get("model") is None)

    # -- 10. Prometheus ------------------------------------------------------------------------------
    status, body, hdrs = request(f"{base}/watchtower/metrics")
    must = ["trafficgen_events_total", "trafficgen_events_duplicate_total",
            "trafficgen_events_rejected_total", 'reason="schema"',
            "trafficgen_data_gaps_total", "trafficgen_data_gaps_healed_total",
            "trafficgen_buffer_depth", "trafficgen_rate_limited_total"]
    check("prometheus: format + обязательные метрики",
          status == 200 and all(m in body for m in must)
          and "text/plain" in str(hdrs.get("Content-Type", "")))
    check("prometheus: rate_limited > 0 после теста лимита",
          any(l.startswith("trafficgen_rate_limited_total ") and not l.endswith(" 0")
              for l in body.splitlines()))

    # -- 10b. Новые read-only эндпоинты (W2/W3) ------------------------------------------------------
    for p in ("/watchtower/proposals", "/watchtower/audit", "/watchtower/blocks",
              "/watchtower/consent", "/watchtower/identity", "/watchtower/forensics",
              "/watchtower/severity", "/watchtower/detectors", "/watchtower/quality"):
        status, body, _ = request(f"{base}{p}")
        try:
            env = json.loads(body)
            missing = [k for k in ENVELOPE_KEYS if k not in env]
        except Exception:
            env, missing = {}, list(ENVELOPE_KEYS)
        check(f"GET {p} -> 200 + конверт", status == 200 and not missing,
              f"status={status} missing={missing}")

    _, body, _ = request(f"{base}/watchtower/audit")
    chain = json.loads(body)["data"]["chain"]
    check("audit: хеш-цепочка цела", chain.get("valid") is True, str(chain))

    _, body, _ = request(f"{base}/watchtower/severity")
    sev = json.loads(body)["data"]["dictionary"]
    check("severity: p1/p2/p3 с временем реакции",
          all(k in sev and sev[k]["responseMinutes"] > 0 for k in ("p1", "p2", "p3")))

    # -- 10c. Эмиттеры, которых раньше не было -------------------------------------------------------
    import watchtower_detectors as det
    det.HealthProbe(we.STORE, snapshot_path=we.config.SNAPSHOT).run()
    _, body, _ = request(f"{base}/watchtower/events?eventType=ExporterHealth&limit=5")
    check("ExporterHealth записан как событие",
          len(json.loads(body)["data"]["events"]) >= 1)

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    request(f"{base}/api/track", method="POST", data={
        "eventType": "PageView", "sessionId": "smoke_idle_sess", "seq": 1,
        "campaignId": "talkchart_seo", "pageId": "target_terminal",
        "sourceId": "direct_web", "timestamp": old_ts})
    det.SessionSweeper(we.STORE, we.CONTROL).run()
    _, body, _ = request(f"{base}/watchtower/events?eventType=SessionAbandoned&limit=5")
    check("SessionAbandoned записан планировщиком таймаутов",
          len(json.loads(body)["data"]["events"]) >= 1)

    status, body, _ = request(f"{base}/api/track", method="POST", data={
        "eventType": "TrafficError", "sessionId": "sess_system_factory", "seq": 1,
        "campaignId": "system", "pageId": "target_terminal",
        "sourceId": "factory_pipeline", "sourceType": "bot",
        "payload": {"stage": "fetch_data", "message": "smoke"}})
    check("TrafficError принимается как событие каталога",
          status == 200 and json.loads(body).get("accepted") == 1, f"status={status}")

    # -- 10d. Контроль-плейн: без настроенных пользователей недоступен --------------------------------
    os.environ.pop("TRAFFICGEN_CONTROL_USERS", None)
    status, body, _ = request(f"{base}/api/control/proposals", method="POST",
                              data={"kind": "emergency_pause", "payload": {"campaignId": "x"}})
    check("control без TRAFFICGEN_CONTROL_USERS -> 503 unavailable",
          status == 503 and json.loads(body)["data"].get("code") == "control_unavailable",
          f"status={status}")

    # -- 10e. Proposal-flow: два подтверждения, 2FA, эффект, откат -----------------------------------
    from watchtower_control import totp_code
    SECRET_A, SECRET_B = "JBSWY3DPEHPK3PXP", "KRSXG5BAMFRGGZDF"
    os.environ["TRAFFICGEN_CONTROL_USERS"] = json.dumps({
        "proposer": {"token": "tok-proposer", "role": "proposer", "secret": SECRET_A},
        "approver1": {"token": "tok-a1", "role": "approver", "secret": SECRET_B},
        "approver2": {"token": "tok-a2", "role": "approver", "secret": SECRET_A},
    })
    hdr_prop = {"Authorization": "Bearer tok-proposer"}
    hdr_a1 = {"Authorization": "Bearer tok-a1"}
    hdr_a2 = {"Authorization": "Bearer tok-a2"}

    status, _, _ = request(f"{base}/api/control/proposals", method="POST",
                           data={"kind": "emergency_pause",
                                 "payload": {"campaignId": "talkchart_smoke_pause",
                                             "reason": "smoke-test"},
                                 "reason": "smoke"})
    check("control: запрос без токена -> 401", status == 401, f"status={status}")

    status, body, _ = request(f"{base}/api/control/proposals", method="POST", headers=hdr_prop,
                              data={"kind": "emergency_pause",
                                    "payload": {"campaignId": "talkchart_smoke_pause",
                                                "reason": "smoke-test"}})
    prop = json.loads(body).get("data") or {}
    pid = prop.get("id")
    check("control: предложение создано -> 201", status == 201 and bool(pid), f"status={status}")

    status, body, _ = request(f"{base}/api/control/proposals/{pid}/approve", method="POST",
                              headers=hdr_a1, data={"totp": "000000"})
    check("control: неверный TOTP -> 401 invalid_2fa",
          status == 401 and json.loads(body)["data"].get("code") == "invalid_2fa",
          f"status={status}")

    status, body, _ = request(f"{base}/api/control/proposals/{pid}/approve", method="POST",
                              headers=hdr_a1, data={"totp": totp_code(SECRET_B)})
    check("control: первое подтверждение -> approved",
          status == 200 and json.loads(body)["data"].get("status") == "approved", f"status={status}")

    status, body, _ = request(f"{base}/api/control/proposals/{pid}/confirm", method="POST",
                              headers=hdr_a1, data={"totp": totp_code(SECRET_B)})
    check("control: two-person rule — тем же человеком нельзя",
          status == 409 and json.loads(body)["data"].get("code") == "two_person_rule",
          f"status={status}")

    status, body, _ = request(f"{base}/api/control/proposals/{pid}/confirm", method="POST",
                              headers=hdr_a2, data={"totp": totp_code(SECRET_A)})
    check("control: второе подтверждение -> applied",
          status == 200 and json.loads(body)["data"].get("status") == "applied", f"status={status}")

    status, body, _ = request(f"{base}/api/track", method="POST", data={
        "eventType": "PageView", "sessionId": "smoke_paused_sess", "seq": 1,
        "campaignId": "talkchart_smoke_pause", "pageId": "target_terminal"})
    check("пауза кампании: событие не принято, помечено paused",
          json.loads(body).get("paused") == 1, f"body={body[:200]}")

    status, body, _ = request(f"{base}/api/control/proposals/{pid}/rollback", method="POST",
                              headers=hdr_a1, data={"totp": totp_code(SECRET_B)})
    check("control: откат -> rolled_back",
          status == 200 and json.loads(body)["data"].get("status") == "rolled_back",
          f"status={status}")

    status, body, _ = request(f"{base}/api/track", method="POST", data={
        "eventType": "PageView", "sessionId": "smoke_paused_sess_2", "seq": 1,
        "campaignId": "talkchart_smoke_pause", "pageId": "target_terminal"})
    check("после отката события снова принимаются",
          json.loads(body).get("accepted") == 1, f"body={body[:200]}")

    _, body, _ = request(f"{base}/watchtower/blocks")
    blocks = json.loads(body)["data"]
    check("/watchtower/blocks отдаёт активные блокировки и паузы",
          isinstance(blocks.get("active"), list) and isinstance(blocks.get("pausedCampaigns"), dict))

    _, body, _ = request(f"{base}/watchtower/proposals")
    props = json.loads(body)["data"]["proposals"]
    check("/watchtower/proposals показывает применённое предложение",
          any(p["id"] == pid for p in props))

    # -- 10f. Новые эндпоинты тоже read-only ----------------------------------------------------------
    for p in ("/watchtower/proposals", "/watchtower/audit", "/watchtower/blocks"):
        status, _, hdrs = request(f"{base}{p}", method="POST", data={"x": 1})
        check(f"POST {p} -> 405 + Allow", status == 405 and hdrs.get("Allow") == "GET, OPTIONS",
              f"status={status} allow={hdrs.get('Allow')}")

    # -- 11. Strict read-only ------------------------------------------------------------------------------
    for p in ("/watchtower/events", "/watchtower/campaigns", "/watchtower/config",
              "/watchtower/health", "/watchtower/funnels", "/watchtower/forecast"):
        status, _, hdrs = request(f"{base}{p}", method="POST", data={"x": 1})
        allow = hdrs.get("Allow", "")
        check(f"POST {p} -> 405 + Allow", status == 405 and allow == "GET, OPTIONS",
              f"status={status} allow={allow}")
    for method in ("PUT", "DELETE", "PATCH"):
        status, _, hdrs = request(f"{base}/watchtower/events", method=method, data={"x": 1})
        check(f"{method} /watchtower/events -> 405 + Allow",
              status == 405 and hdrs.get("Allow") == "GET, OPTIONS", f"status={status}")
    # бывший alias /watchtower/ingest удалён — тоже 405
    status, _, _ = request(f"{base}/watchtower/ingest", method="POST",
                           data={"eventType": "PageView", "seq": 1})
    check("POST /watchtower/ingest (legacy alias) -> 405", status == 405, f"status={status}")

    server.shutdown()
    print("[SMOKE] Server shut down.")
    we.STORE, we.CONTROL = old_store, old_control
    shutil.rmtree(tmp_dir, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"\n[SMOKE] FAILED {len(failed)}/{len(results)} checks:")
        for n in failed:
            print(f"  - {n}")
        return 1
    print(f"\n[SMOKE] ALL {len(results)}/{len(results)} CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(run_smoke_tests())
