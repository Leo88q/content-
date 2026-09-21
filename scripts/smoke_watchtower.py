#!/usr/bin/env python3
"""
Smoke test suite for Games Watchtower Integration Adapter.
Spins up an ephemeral instance of the server on a free port, executes end-to-end HTTP
contract validations against all 12 endpoints, verifies read-only enforcement,
and checks Prometheus metrics.
"""

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "site", "factory"))

from http.server import ThreadingHTTPServer
from watchtower_exporter import WatchtowerHandler, STORE


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
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, body, resp.headers
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        return e.code, body, e.headers


def run_smoke_tests():
    port = get_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), WatchtowerHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.3)

    base_url = f"http://127.0.0.1:{port}"
    print(f"[SMOKE] Watchtower test server started on {base_url}")

    results = []

    def check(name, condition, details=""):
        if condition:
            print(f"  [PASS] {name}")
            results.append((name, True))
        else:
            print(f"  [FAIL] {name}: {details}")
            results.append((name, False))

    # 1. Health
    status, body, _ = request(f"{base_url}/watchtower/health")
    data = json.loads(body)
    check("GET /watchtower/health status 200", status == 200)
    check("GET /watchtower/health envelope data.status == ok", data.get("data", {}).get("status") == "ok")
    check("GET /watchtower/health envelope parserVersion", data.get("parserVersion") == "trafficgen-v1")

    # 2. Readyz
    status, body, _ = request(f"{base_url}/watchtower/readyz")
    data = json.loads(body)
    check("GET /watchtower/readyz status 200", status == 200)
    check("GET /watchtower/readyz envelope dataQuality complete/partial", data.get("dataQuality") in ("complete", "partial"))

    # 3. Config
    status, body, _ = request(f"{base_url}/watchtower/config")
    data = json.loads(body)
    check("GET /watchtower/config status 200", status == 200)
    cfg = data.get("data", {})
    check("GET /watchtower/config readOnly == True", cfg.get("readOnly") is True)
    check("GET /watchtower/config appId == trafficgen", cfg.get("appId") == "trafficgen")

    # 4. Campaigns
    status, body, _ = request(f"{base_url}/watchtower/campaigns")
    data = json.loads(body)
    check("GET /watchtower/campaigns status 200", status == 200)
    camps = data.get("data", {}).get("campaigns", [])
    check("GET /watchtower/campaigns count >= 5", len(camps) >= 5)

    # 5. Campaign Details
    status, body, _ = request(f"{base_url}/watchtower/campaigns/talkchart_seo")
    data = json.loads(body)
    check("GET /watchtower/campaigns/talkchart_seo status 200", status == 200)
    check("GET /watchtower/campaigns/talkchart_seo id match", data.get("data", {}).get("id") == "talkchart_seo")

    # 6. Sources
    status, body, _ = request(f"{base_url}/watchtower/sources")
    data = json.loads(body)
    check("GET /watchtower/sources status 200", status == 200)
    sources = data.get("data", {}).get("sources", [])
    check("GET /watchtower/sources count >= 6", len(sources) >= 6)

    # 7. Pages (check studio games)
    status, body, _ = request(f"{base_url}/watchtower/pages")
    data = json.loads(body)
    check("GET /watchtower/pages status 200", status == 200)
    pages = data.get("data", {}).get("pages", [])
    check("GET /watchtower/pages contains 4 studio games", len([p for p in pages if p.get("role") == "studio_game"]) >= 4)

    # 8. Client Telemetry Ingestion (POST /api/track)
    track_event = {
        "eventType": "CTAClicked",
        "campaignId": "talkchart_interactive_radar",
        "pageId": "terminal",
        "sessionId": "smoke_sess_001",
        "seq": 1,
        "payload": {"target": "sixsec", "gameId": "game1"}
    }
    status, body, _ = request(f"{base_url}/api/track", method="POST", data=track_event)
    check("POST /api/track status 200", status == 200)
    tdata = json.loads(body)
    check("POST /api/track status ok", tdata.get("status") == "ok")

    # 9. Events Read & Cursor Pagination
    status, body, _ = request(f"{base_url}/watchtower/events?limit=10")
    data = json.loads(body)
    check("GET /watchtower/events status 200", status == 200)
    events = data.get("data", {}).get("events", [])
    check("GET /watchtower/events returns array", isinstance(events, list))
    check("GET /watchtower/events contains ingested event", any(e.get("sessionId") == "smoke_sess_001" for e in events))

    # 10. Metrics Daily
    status, body, _ = request(f"{base_url}/watchtower/metrics/daily?period=7d")
    data = json.loads(body)
    check("GET /watchtower/metrics/daily status 200", status == 200)
    mdata = data.get("data", {})
    check("GET /watchtower/metrics/daily contains totalEvents", "totalEvents" in mdata)

    # 11. Funnels
    status, body, _ = request(f"{base_url}/watchtower/funnels")
    data = json.loads(body)
    check("GET /watchtower/funnels status 200", status == 200)
    fdata = data.get("data", {})
    check("GET /watchtower/funnels contains stages", "stages" in fdata)

    # 12. Forecast (Strictly unavailable)
    status, body, _ = request(f"{base_url}/watchtower/forecast")
    data = json.loads(body)
    check("GET /watchtower/forecast status 200", status == 200)
    check("GET /watchtower/forecast quality unavailable", data.get("dataQuality") == "unavailable")
    check("GET /watchtower/forecast confidence 0.0", data.get("confidence") == 0.0)

    # 13. Prometheus Metrics Endpoint
    status, body, headers = request(f"{base_url}/watchtower/metrics")
    check("GET /watchtower/metrics status 200", status == 200)
    check("GET /watchtower/metrics has trafficgen_events_total", "trafficgen_events_total" in body)
    check("GET /watchtower/metrics has trafficgen_data_gaps_total", "trafficgen_data_gaps_total" in body)

    # 14. Strict Read-Only Enforcement (POST to /watchtower/* MUST return 405)
    status, body, _ = request(f"{base_url}/watchtower/campaigns", method="POST", data={"name": "illegal"})
    check("POST /watchtower/campaigns returns 405 Method Not Allowed", status == 405)

    status, body, _ = request(f"{base_url}/watchtower/events", method="POST", data={"name": "illegal"})
    check("POST /watchtower/events returns 405 Method Not Allowed", status == 405)

    server.shutdown()
    print("[SMOKE] Server shut down.")

    failed = [name for name, ok in results if not ok]
    if failed:
        print(f"\n[SMOKE] FAILED {len(failed)}/{len(results)} checks: {failed}")
        return 1
    print(f"\n[SMOKE] ALL {len(results)}/{len(results)} CHECKS PASSED SUCCESSFULLY.")
    return 0


if __name__ == "__main__":
    sys.exit(run_smoke_tests())
