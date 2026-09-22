#!/usr/bin/env python3
"""
Contract tests for the Games Watchtower Integration Adapter (trafficgen).

16 групп приёмки из PROMPT_TRAFFIC_GENERATOR_INTEGRATION.md §11.1:
 1  envelope               9  read-only (сигнатура хендлеров; HTTP-часть в smoke)
 2  identity              10  auth tokens parsing (HTTP-часть в smoke)
 3  idempotency           11  metrics/daily
 4  rejected               12  funnel honesty
 5  cursor/replay          13  forecast (HTTP, в smoke)
 6  filters                14  events catalog
 7  gap detect + heal      15  prometheus + restart persistence
 8  PII scrub              16  bot marking + synthetic exclusion + retention
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "site", "factory"))

import watchtower_exporter as we
from watchtower_exporter import (
    WatchtowerStore, strip_pii, canonicalize_event, build_prometheus_metrics,
    wrap_envelope, compute_metrics, compute_funnel, reconcile_catalog,
    EVENTS_CATALOG, IMPLEMENTED_EVENTS, UNAVAILABLE_EVENTS, TARGET_PAGE_IDS,
    InvalidCursor,
)

APP_JS = os.path.join(REPO_ROOT, "site", "app.js")


def make_event(**kw):
    base = {
        "eventType": "PageView",
        "campaignId": "talkchart_interactive_radar",
        "pageId": "target_terminal",
        "sessionId": "sess_unit",
        "seq": 1,
        "payload": {},
    }
    base.update(kw)
    return base


class TestWatchtowerAdapter(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_watchtower.db")
        self.store = WatchtowerStore(db_path=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -- 1. Единый конверт ---------------------------------------------------
    def test_01_envelope_shape(self):
        env = wrap_envelope({"a": 1})
        for k in ("data", "generatedAt", "period", "source", "dataQuality",
                  "confidence", "parserVersion"):
            self.assertIn(k, env, f"missing envelope key: {k}")
        self.assertEqual(env["source"], "trafficgen-exporter")
        self.assertEqual(env["parserVersion"], "trafficgen-v1")
        err = we.error_envelope("boom", "invalid_cursor")
        self.assertEqual(err["dataQuality"], "unavailable")
        self.assertEqual(err["confidence"], 0.0)
        self.assertEqual(err["data"]["code"], "invalid_cursor")

    # -- 2. Каноническая идентичность ----------------------------------------
    def test_02_identity_deterministic_and_normalized(self):
        raw = make_event(seq=1, sessionId="sess_x", campaignId="c1", pageId="terminal")
        ev1 = canonicalize_event(raw)
        ev2 = canonicalize_event(dict(raw))
        self.assertEqual(
            ev1["identity"],
            "offchain:trafficgen:c1:target_terminal:sess_x:1",
            "legacy pageId 'terminal' must normalize to 'target_terminal'")
        self.assertEqual(ev1["identity"], ev2["identity"], "identity must be deterministic")
        self.assertEqual(ev1["chain"], "offchain")
        self.assertEqual(ev1["source"], "trafficgen")
        self.assertEqual(ev1["app"], "trafficgen")
        self.assertEqual(ev1["parserVersion"], "trafficgen-v1")
        self.assertTrue(ev1["sessionId"].startswith("sess_"))

    def test_02b_session_id_pseudonymization(self):
        """Несоответствующий sessionId детерминированно псевдонимизируется."""
        raw = make_event(sessionId="user+tracker@example.com")
        ev1 = canonicalize_event(raw)
        ev2 = canonicalize_event(dict(raw))
        self.assertTrue(ev1["sessionId"].startswith("sess_"))
        self.assertNotIn("user+tracker@example.com", json.dumps(ev1, ensure_ascii=False))
        self.assertEqual(ev1["sessionId"], ev2["sessionId"],
                         "псевдонимизация обязана быть детерминированной (иначе ломается dedup)")
        self.assertIn(ev1["sessionId"], ev1["identity"],
                      "identity пересобирается с псевдонимом, а не с исходным id")
        # соответствующий формат не трогаем; явная identity системных событий сохраняется
        sys_ev = canonicalize_event(make_event(sessionId="sess_system_fake",
                                               identity="offchain:trafficgen:c:target_terminal:sess_system_fake:created"))
        self.assertTrue(sys_ev["identity"].endswith(":created"))

    # -- 3. Идемпотентность ---------------------------------------------------
    def test_03_idempotency_eventId_and_identity(self):
        ev = canonicalize_event(make_event(seq=1, sessionId="sess_dedup_01"))
        r1 = self.store.record_event(dict(ev))
        self.assertEqual(r1["status"], "accepted")
        r2 = self.store.record_event(dict(ev))  # полный повтор
        self.assertEqual(r2["status"], "duplicate")
        # то же identity, другой eventId
        ev3 = dict(ev)
        ev3["eventId"] = "ev_" + "f" * 12
        r3 = self.store.record_event(ev3)
        self.assertEqual(r3["status"], "duplicate")
        with self.store.get_connection() as conn:
            cnt = conn.execute("SELECT COUNT(*) c FROM events WHERE session_id='sess_dedup_01'").fetchone()["c"]
        self.assertEqual(cnt, 1)
        self.assertEqual(self.store.metrics["events_duplicate_total"], 2)
        self.assertEqual(self.store.metrics["events_total"]["real"], 1, "duplicates must not double-count")

    # -- 4. Rejected ------------------------------------------------------------
    def test_04_schema_rejections(self):
        before = sum(self.store.metrics["events_rejected_total"].values())
        cases = [
            {"seq": 1},                                               # нет eventType
            make_event(seq=0),                                        # seq < 1
            make_event(seq=2, timestamp="2026-09-22T12:00:00+03:00"), # не-UTC
            make_event(seq=2, timestamp="22.09.2026 12:00"),          # невалидный формат
            make_event(seq=2, sourceType="unknown_kind"),             # sourceType вне whitelist
        ]
        for raw in cases:
            res = self.store.record_event(raw)
            self.assertEqual(res["status"], "rejected", f"not rejected: {raw}")
        after = sum(self.store.metrics["events_rejected_total"].values())
        self.assertEqual(after - before, len(cases))
        with self.store.get_connection() as conn:
            cnt = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        self.assertEqual(cnt, 0, "rejected events must not reach storage")

    # -- 5. Курсор и replay -------------------------------------------------------
    def test_05_cursor_pagination_and_replay(self):
        total = 1200
        for i in range(total):
            self.store.record_event(make_event(
                seq=1, sessionId=f"sess_cur_{i}", eventType="PageView"))
        seen, cursor, pages = [], None, 0
        while True:
            page, cursor = self.store.get_events(cursor=cursor, limit=500)
            seen.extend(page)
            pages += 1
            if not cursor:
                break
        self.assertEqual(pages, 3)
        self.assertEqual(len(seen), total)
        ids = [e["eventId"] for e in seen]
        self.assertEqual(len(set(ids)), total, "dups across pages")
        # replay: чтение с нуля даёт тот же упорядоченный набор
        again, cursor = [], None
        while True:
            page, cursor = self.store.get_events(cursor=cursor, limit=700)
            again.extend(e["eventId"] for e in page)
            if not cursor:
                break
        self.assertEqual(ids, again, "replay must be byte-identical in order")
        with self.assertRaises(InvalidCursor):
            self.store.get_events(cursor="not-base64!!!")
        with self.assertRaises(InvalidCursor):
            import base64 as b64
            self.store.get_events(cursor=b64.b64encode(b"evil:payload").decode())
        with self.assertRaises(InvalidCursor):
            self.store.get_events(cursor=b64.b64encode(b"cursor:-5").decode())
        # допустимая replay-форма (совместимость с чек-листом Watchtower): base64 голого id
        replay_events, _ = self.store.get_events(cursor=b64.b64encode(b"5").decode(), limit=500)
        self.assertEqual([e["eventId"] for e in replay_events], ids[5:505],
                         "MA== / bare-int cursor must replay from that id forward")
        replay_all, _ = self.store.get_events(cursor=b64.b64encode(b"0").decode(), limit=500)
        self.assertEqual(replay_all[0]["eventId"], ids[0])

    # -- 6. Фильтры -------------------------------------------------------------
    def test_06_filters(self):
        self.store.record_event(make_event(seq=1, sessionId="s1", eventType="PageView",
                                           campaignId="campA", sourceId="x_twitter"))
        self.store.record_event(make_event(seq=1, sessionId="s2", eventType="CTAClicked",
                                           campaignId="campA", sourceId="direct_web"))
        self.store.record_event(make_event(seq=1, sessionId="s3", eventType="PageView",
                                           campaignId="campB", sourceId="factory_pipeline"))
        e, _ = self.store.get_events(filters={"eventType": "PageView"})
        self.assertEqual({x["eventType"] for x in e}, {"PageView"})
        e, _ = self.store.get_events(filters={"campaignId": "campA"})
        self.assertEqual(len(e), 2)
        e, _ = self.store.get_events(filters={"sourceType": "bot"})
        self.assertEqual(len(e), 1, "factory_pipeline must be bot")
        e, _ = self.store.get_events(filters={"eventType": "PageView", "campaignId": "campA"})
        self.assertEqual(len(e), 1)
        e, _ = self.store.get_events(filters={"since": "2999-01-01T00:00:00.000Z"})
        self.assertEqual(len(e), 0)

    # -- 7. Gap detection и heal --------------------------------------------------
    def test_07_gap_detect_partial_heal_resolve(self):
        sess = "sess_gap_01"
        self.store.record_event(make_event(seq=1, sessionId=sess))
        self.store.record_event(make_event(seq=2, sessionId=sess))
        self.store.record_event(make_event(seq=5, sessionId=sess, eventType="CTAClicked"))
        alerts = [a for a in self.store.get_alerts() if a["sessionId"] == sess]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["alertType"], "DataGapDetected")
        self.assertEqual(alerts[0]["status"], "active")
        self.assertEqual(alerts[0]["details"],
                         {"expectedSeq": 3, "receivedSeq": 5, "missingCount": 2})
        # частичный backfill (3 из 2 нужных) — разрыв НЕ закрывается
        self.store.record_event(make_event(seq=3, sessionId=sess))
        alerts = [a for a in self.store.get_alerts() if a["sessionId"] == sess]
        self.assertEqual(alerts[0]["status"], "active", "partial fill must not heal")
        # полный backfill
        self.store.record_event(make_event(seq=4, sessionId=sess))
        alerts = [a for a in self.store.get_alerts() if a["sessionId"] == sess]
        self.assertEqual(alerts[0]["status"], "resolved")
        self.assertIsNotNone(alerts[0]["resolvedAt"])
        with self.store.get_connection() as conn:
            heal = conn.execute(
                "SELECT payload FROM events WHERE event_type='DataGapHealed'").fetchone()
        self.assertIsNotNone(heal)
        hp = json.loads(heal["payload"])
        self.assertEqual(hp["healedSeq"], [3, 4])
        self.assertEqual(self.store.metrics["data_gaps_healed_total"], 1)

    # -- 8. PII scrub --------------------------------------------------------------
    def test_08_pii_scrub_deep(self):
        raw = make_event(
            seq=1, sessionId="sess_pii_01",
            ip="192.168.0.5", email="trader@example.com", fingerprint="fp123",
            device_id="dev-1", user_agent="curl/8", cookie="sid=zzz", private_key="deadbeef",
            walletAddress="So11111111111111111111111111111111111111112",
            payload={
                "user_ip": "10.0.0.9",
                "ref": "write me: bob@corp.io",
                "avatar": "http://x.io/a.png",
                "path": "/pools/sol.html?token=sekrit&utm_source=x&fbclid=abc",
                "note": "ipv6 fe80::1 and key " + "ab" * 32,
                "wallet": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
                "auth": {"token": "t"},
                "nested": {"arr": [{"secret": "s"}, {"ok": "v"}]},
                "json_string": '{"phone": "+7999", "keep": 1}',
                "target": "solana_miner",
                "name": "SOL/USDC",  # публичное имя пула — НЕ PII, остаётся
            },
        )
        res = self.store.record_event(raw)
        self.assertEqual(res["status"], "accepted")
        events, _ = self.store.get_events()
        stored = json.dumps(events, ensure_ascii=False)
        for frag in ("192.168.0.5", "trader@example.com", "fp123", "dev-1", "curl/8",
                     "sid=zzz", "deadbeef", "10.0.0.9", "bob@corp.io", "fe80::1",
                     "ab" * 32, "So1111111", "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
                     "sekrit", "fbclid=abc", "+7999", "session=secret"):
            self.assertNotIn(frag, stored, f"PII leaked: {frag}")
        keep = json.dumps(strip_pii(raw["payload"]) | {"payload_keys": "x"}, ensure_ascii=False)
        self.assertIn('[redacted]', keep)
        self.assertIn("utm_source=x", stored, "utm-* must be preserved")
        self.assertIn("solana_miner", stored)
        self.assertIn("SOL/USDC", stored, "public pool name is not PII and must survive")
        self.assertTrue(all(e["sessionId"].startswith("sess_") for e in events))

    def test_08b_strip_pii_unit(self):
        out = strip_pii({"a": 1, "ip": "1.1.1.1", "l": [{"token": "t"}, "x@y.zz"]})
        self.assertEqual(out, {"a": 1, "l": [{}, "[redacted]"]})

    # -- 9. Read-only (сигнатуры; HTTP-проверки в smoke_watchtower.py) -----------
    def test_09_readonly_handlers_exist(self):
        for m in ("do_PUT", "do_DELETE", "do_PATCH", "do_POST"):
            self.assertTrue(hasattr(we.WatchtowerHandler, m), f"missing handler {m}")

    # -- 10. Auth tokens parsing --------------------------------------------------
    def test_10_token_env_parsing(self):
        old1, old2 = os.environ.pop("WATCHTOWER_READ_TOKEN", None), os.environ.pop("WATCHTOWER_READ_TOKEN_PREVIOUS", None)
        try:
            self.assertEqual(we._watchtower_tokens(), [])
            os.environ["WATCHTOWER_READ_TOKEN"] = "t1"
            os.environ["WATCHTOWER_READ_TOKEN_PREVIOUS"] = "t0"
            self.assertEqual(we._watchtower_tokens(), [b"t1", b"t0"])
            cfg = self.store.get_config()
            self.assertEqual(cfg["auth"], "api-key")
        finally:
            os.environ.pop("WATCHTOWER_READ_TOKEN", None)
            os.environ.pop("WATCHTOWER_READ_TOKEN_PREVIOUS", None)
            if old1:
                os.environ["WATCHTOWER_READ_TOKEN"] = old1
            if old2:
                os.environ["WATCHTOWER_READ_TOKEN_PREVIOUS"] = old2
        self.assertEqual(self.store.get_config()["auth"], "none")

    # -- 11. metrics/daily ---------------------------------------------------------
    def test_11_metrics_daily_shape(self):
        # две сессии: одна bounce (1 событие), вторая 3 события за 60с; бот-сессия отдельно
        self.store.record_event(make_event(seq=1, sessionId="sA", eventType="SessionStarted",
                                           timestamp="2026-09-22T10:00:00.000Z"))
        self.store.record_event(make_event(seq=2, sessionId="sA", eventType="PageView",
                                           timestamp="2026-09-22T10:00:30.000Z"))
        self.store.record_event(make_event(seq=3, sessionId="sA", eventType="CTAClicked",
                                           timestamp="2026-09-22T10:01:00.000Z"))
        self.store.record_event(make_event(seq=1, sessionId="sB", eventType="PageView",
                                           timestamp="2026-09-22T10:05:00.000Z"))
        self.store.record_event(make_event(seq=1, sessionId="sB2", eventType="PageView",
                                           sourceId="factory_pipeline",
                                           timestamp="2026-09-22T10:06:00.000Z"))
        m = compute_metrics(7, self.store)
        self.assertEqual(m["periodDays"], 7)
        self.assertEqual(len(m["days"]), 7, "day series must be continuous")
        dates = [d["date"] for d in m["days"]]
        self.assertEqual(len(set(dates)), 7)
        day = [d for d in m["days"] if d["date"] == "2026-09-22"][0]
        self.assertEqual(day["pageViews"], 3)
        self.assertEqual(day["sessions"], 3)
        self.assertEqual(day["bounceRate"], round(2 / 3, 3))
        # длительности [0, 0, 60]: медиана 0, среднее 20, p95 = 60
        self.assertEqual(day["sessionDurationSeconds"]["p50"], 0.0)
        self.assertEqual(day["sessionDurationSeconds"]["p95"], 60.0)
        self.assertEqual(day["sessionDurationSeconds"]["avg"], 20.0)
        self.assertEqual(day["ctaClickRate"], round(1 / 3, 3))
        self.assertEqual(day["trafficType"]["bot"], 1)
        self.assertEqual(day["trafficType"]["real"], 4)
        self.assertIn("byCampaign", day["breakdowns"])
        self.assertIn("bySource", day["breakdowns"])
        self.assertIn("byPage", day["breakdowns"])
        self.assertIn("integrity", day)
        self.assertIn("errors", day)
        self.assertTrue(any(u["metric"] == "forecast" for u in m["unavailableMetrics"]))
        est = [u for u in m["unavailableMetrics"] if u["metric"] == "sessionDurationSeconds"][0]
        self.assertTrue(est["estimate"])
        self.assertEqual(m["uniquePseudoVisitors"], 3)
        self.assertEqual(m["visitorsByType"]["bot"], 1)

    # -- 12. Воронка честности ------------------------------------------------------
    def test_12_funnel_honesty(self):
        # 5/4/3/2/1: CampaignStarted..LandingReached по одному лесенкой
        stages5 = ["CampaignStarted", "SessionStarted", "PageView", "CTAClicked", "LandingReached"]
        # LandingReached не в каталоге implemented — отправляем как известный тип каталога,
        # но он помечен unavailable: ступень обязана нести stageUnavailable и честный count.
        seq = 1
        for n, et in enumerate(stages5):
            for k in range(5 - n):
                self.store.record_event(make_event(seq=seq, sessionId=f"f_sess_{n}_{k}", eventType=et))
                seq += 1
        f = compute_funnel(self.store, period_days=7)
        steps = {s["stage"]: s for s in f["steps"]}
        self.assertEqual(steps["CampaignStarted"]["count"], 5)
        self.assertEqual(steps["SessionStarted"]["count"], 4)
        self.assertNotIn("stageUnavailable", steps["CampaignStarted"])
        self.assertTrue(steps["LandingReached"]["stageUnavailable"],
                        "LandingReached has no emitter -> stageUnavailable")
        self.assertEqual(steps["SessionStarted"]["conversionFromPrev"], round(4 / 5, 3))
        self.assertEqual(steps["PageView"]["dropOffRate"], round(1 - round(3 / 4, 3), 3))
        self.assertIn("byCampaign", f)
        self.assertIn("bySource", f)
        # вторая БД: пустая воронка — конверсии null, а не выдуманные 1.0
        empty = WatchtowerStore(db_path=os.path.join(self.test_dir, "empty.db"))
        f2 = compute_funnel(empty, period_days=7)
        s2 = {s["stage"]: s for s in f2["steps"]}
        self.assertIsNone(s2["CampaignStarted"]["conversionFromPrev"])
        self.assertIsNone(s2["SessionStarted"]["conversionFromPrev"])
        self.assertIsNone(s2["SessionStarted"]["conversionFromFirst"])
        self.assertEqual(s2["CampaignStarted"]["count"], 0,
                         "Р3: никаких len(CAMPAIGNS_DEF) вместо реальных событий")

    # -- 14. Каталог событий ---------------------------------------------------------
    def test_14_events_catalog_consistency(self):
        implementations = set(IMPLEMENTED_EVENTS)
        unavailable = {u["eventType"] for u in UNAVAILABLE_EVENTS}
        catalog = {e for e, _, _ in EVENTS_CATALOG}
        self.assertEqual(implementations | unavailable, catalog)
        self.assertEqual(implementations & unavailable, set())
        # Каждый implemented тип реально эмитируется: клиент или серверный эмиттер
        with open(APP_JS, "r", encoding="utf-8") as fh:
            js = fh.read()
        client_events = {"SessionStarted", "PageView", "Click", "CTAClicked", "SessionEnded"}
        for et in client_events:
            self.assertIn(f'"{et}"', js, f"client emitter for {et} missing in site/app.js")
        # серверные эмиттеры: реконсилёр + gap + rate limit
        store = WatchtowerStore(db_path=os.path.join(self.test_dir, "recon.db"))
        emitted = reconcile_catalog(store)
        self.assertTrue({"CampaignCreated", "CampaignStarted", "SourceConnected",
                         "PageAssigned"} <= set(emitted), emitted)
        second = reconcile_catalog(store)
        self.assertEqual(second, [], "reconcile must be idempotent (no dup lifecycle events)")
        with store.get_connection() as conn:
            types = {r[0] for r in conn.execute("SELECT DISTINCT event_type FROM events").fetchall()}
        for et in ("CampaignCreated", "CampaignStarted", "SourceConnected", "PageAssigned"):
            self.assertIn(et, types)
        recon_after = [a for a in store.get_alerts()]
        self.assertEqual(recon_after, [], "system sessions must bypass gap detection")

    # -- 15. Prometheus и переживание перезапуска --------------------------------------
    def test_15_prometheus_format_and_restart_persistence(self):
        self.store.record_event(make_event(seq=1, sessionId="sess_prom"))
        self.store.record_event(make_event(seq=1, sessionId="sess_prom"))  # duplicate
        self.store.record_event({"seq": 1})                                # rejected
        txt = build_prometheus_metrics(self.store)
        required = [
            "trafficgen_events_total", "trafficgen_events_duplicate_total",
            "trafficgen_events_rejected_total", "trafficgen_delivery_failures_total",
            "trafficgen_exporter_errors_total", "trafficgen_buffer_depth",
            "trafficgen_data_gaps_total", "trafficgen_data_gaps_healed_total",
            "trafficgen_rate_limited_total", "trafficgen_events_unknown_type_total",
        ]
        for name in required:
            self.assertIn(f"# TYPE {name} counter" if name != "trafficgen_buffer_depth"
                          else f"# TYPE {name} gauge", txt, name)
        self.assertIn('reason="schema"', txt)
        # симуляция перезапуска: новый инстанс на той же БД
        store2 = WatchtowerStore(db_path=self.db_path)
        self.assertEqual(store2.metrics["events_duplicate_total"], 1)
        self.assertEqual(sum(store2.metrics["events_rejected_total"].values()), 1)
        self.assertGreaterEqual(sum(store2.metrics["events_total"].values()), 1)
        txt2 = build_prometheus_metrics(store2)
        self.assertIn("trafficgen_events_duplicate_total 1", txt2)

    # -- 16. Bot-маркировка, неизвестные типы, синтетика, retention -------------------
    def test_16_bot_synthetic_unknown_retention(self):
        # factory_pipeline принудительно bot, даже если прислали real
        r = self.store.record_event(make_event(seq=1, sessionId="sess_fp",
                                               sourceId="factory_pipeline", sourceType="real"))
        self.assertEqual(r["status"], "accepted")
        events, _ = self.store.get_events(filters={"sourceType": "bot"})
        self.assertEqual(len(events), 1)
        # isBot=True -> bot
        self.store.record_event({"eventType": "PageView", "sessionId": "sess_ib",
                                 "seq": 1, "isBot": True})
        events, _ = self.store.get_events(filters={"sourceType": "bot"})
        self.assertEqual(len(events), 2)
        # неизвестный тип: сохраняется как partial + unknown счётчик
        r = self.store.record_event({"eventType": "SomethingNew", "sessionId": "sess_un", "seq": 1})
        self.assertEqual(r["status"], "accepted")
        events, _ = self.store.get_events(filters={"eventType": "SomethingNew"})
        self.assertEqual(events[0]["dataQuality"], "partial")
        self.assertEqual(self.store.metrics["events_unknown_type"].get("SomethingNew"), 1)
        # синтетика исключается из агрегатов
        self.store.record_event({"eventType": "PageView", "sessionId": "sess_smoke_x",
                                 "seq": 1, "payload": {"synthetic": True}})
        m = compute_metrics(7, self.store)
        pv_sessions = set()
        for b in m["breakdowns"]["bySource"].values():
            pass
        total_pv = m["pageViews"]
        with self.store.get_connection() as conn:
            all_pv = conn.execute(
                "SELECT COUNT(*) c FROM events WHERE event_type='PageView' AND is_synthetic=0").fetchone()["c"]
        self.assertEqual(total_pv, all_pv, "synthetic must not enter product aggregates")
        # retention: старое событие выпиливается
        self.store.record_event({"eventType": "PageView", "sessionId": "sess_old",
                                 "seq": 1, "timestamp": "2020-01-01T00:00:00.000Z"})
        pruned = self.store.prune_retention()
        self.assertGreaterEqual(pruned["events"], 1)
        events, _ = self.store.get_events(filters={"since": "2019-01-01T00:00:00.000Z"})
        self.assertFalse(any(e["sessionId"] == "sess_old" for e in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
