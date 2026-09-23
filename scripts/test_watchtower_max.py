#!/usr/bin/env python3
"""Контрактные тесты «максимума» trafficgen: W2 (качество данных и события),
W3 (переплетение) и контроль-плейн.

Отдельный файл, а не расширение scripts/test_watchtower.py: там зафиксированы
17 приёмочных групп исходного промпта, их состав не должен размываться.

Правило, общее с остальным репозиторием: событие считается реализованным только
если у него есть эмиттер И тест. Поэтому каждый новый эмиттер проверяется здесь
на реальной записи в БД, а не на «функция существует».

Запуск:
    python3 scripts/test_watchtower_max.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "site", "factory"))

import watchtower_exporter as we          # noqa: E402
from watchtower_exporter import EventStore, compute_metrics, now_utc_iso  # noqa: E402
from watchtower_control import ControlStore, totp_code, control_enabled  # noqa: E402
import watchtower_detectors as det        # noqa: E402

# TOTP-секреты (base32) — тестовые, в репозитории больше нигде не встречаются.
SECRET_A = "JBSWY3DPEHPK3PXP"
SECRET_B = "KRSXG5BAMFRGGZDF"
SECRET_C = "MZXW6YTBOI======"

USERS = {
    "proposer": {"token": "tok-proposer", "role": "proposer", "secret": SECRET_A},
    "approver1": {"token": "tok-a1", "role": "approver", "secret": SECRET_B},
    "approver2": {"token": "tok-a2", "role": "approver", "secret": SECRET_C},
}


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "test.db")
        self.store = EventStore(db_path=self.db_path)
        self.control = ControlStore(db_path=self.db_path)
        # Модульные globals нужны emit_control_event и bind_from_event.
        self._old_store, self._old_control = we.STORE, we.CONTROL
        we.STORE, we.CONTROL = self.store, self.control
        self._old_users = os.environ.get("TRAFFICGEN_CONTROL_USERS")
        os.environ["TRAFFICGEN_CONTROL_USERS"] = json.dumps(USERS)
        self._old_ttl = os.environ.get("TRAFFICGEN_PROPOSAL_TTL_SECONDS")
        os.environ["TRAFFICGEN_PROPOSAL_TTL_SECONDS"] = "1800"

    def tearDown(self):
        we.STORE, we.CONTROL = self._old_store, self._old_control
        if self._old_users is None:
            os.environ.pop("TRAFFICGEN_CONTROL_USERS", None)
        else:
            os.environ["TRAFFICGEN_CONTROL_USERS"] = self._old_users
        if self._old_ttl is None:
            os.environ.pop("TRAFFICGEN_PROPOSAL_TTL_SECONDS", None)
        else:
            os.environ["TRAFFICGEN_PROPOSAL_TTL_SECONDS"] = self._old_ttl
        shutil.rmtree(self.dir, ignore_errors=True)

    # --- helpers -----------------------------------------------------------
    def actor(self, name):
        return name, USERS[name]["role"], USERS[name]["secret"]

    def code(self, name):
        return totp_code(USERS[name]["secret"])

    def events_of(self, event_type):
        with self.store.get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM events WHERE event_type = ? ORDER BY id ASC",
                (event_type,)).fetchall()]

    def make_proposal(self, kind, payload, actor="proposer"):
        name, role, _secret = self.actor(actor)
        proposal, err = self.control.create_proposal(kind, payload, name, role,
                                                     reason="unit test")
        self.assertIsNone(err, f"создание proposal не должно падать: {err}")
        return proposal


# ---------------------------------------------------------------------------
# W3 / i-11: proposal-контур — два подтверждения, 2FA, аудит, откат
# ---------------------------------------------------------------------------
class TestProposalFlow(BaseCase):
    def test_01_two_person_rule_and_2fa_are_enforced(self):
        proposal = self.make_proposal("emergency_pause",
                                      {"campaignId": "talkchart_seo", "reason": "test"})
        pid = proposal["id"]
        name_p, role_p, secret_p = self.actor("proposer")

        # роль proposer вообще не имеет права подтверждать
        _, err = self.control.approve(pid, name_p, role_p, self.code("proposer"), secret_p)
        self.assertEqual(err["code"], "forbidden")

        # неверный TOTP
        name_a, role_a, secret_a = self.actor("approver1")
        _, err = self.control.approve(pid, name_a, role_a, "000000", secret_a)
        self.assertEqual(err["code"], "invalid_2fa")

        # первое подтверждение
        prop, err = self.control.approve(pid, name_a, role_a, self.code("approver1"), secret_a)
        self.assertIsNone(err)
        self.assertEqual(prop["status"], "approved")

        # повторное использование того же кода — запрещено
        name_b, role_b, secret_b = self.actor("approver2")
        # повторное approve из статуса approved невозможно по состоянию,
        # а не по коду — состояние проверяется раньше 2FA
        _, err = self.control.approve(pid, name_b, role_b, self.code("approver2"), secret_b)
        self.assertEqual(err["code"], "invalid_state")

        # второе подтверждение тем же человеком — запрещено (two-person rule)
        _, err = self.control.confirm(pid, name_a, role_a, self.code("approver1"), secret_a)
        self.assertEqual(err["code"], "two_person_rule")

        # второе подтверждение другим человеком — применяет
        prop, err = self.control.confirm(pid, name_b, role_b, self.code("approver2"), secret_b,
                                         emit_fn=None)
        self.assertIsNone(err, f"применение не должно падать: {err}")
        self.assertEqual(prop["status"], "applied")
        self.assertTrue(self.control.is_paused("talkchart_seo"))

        # откат
        prop, err = self.control.rollback(pid, name_a, role_a, self.code("approver1"), secret_a,
                                          reason="тест отката")
        self.assertIsNone(err)
        self.assertEqual(prop["status"], "rolled_back")
        self.assertFalse(self.control.is_paused("talkchart_seo"))

    def test_02_applied_proposal_emits_catalog_event(self):
        proposal = self.make_proposal("emergency_pause",
                                      {"campaignId": "talkchart_seo", "reason": "incident"})
        pid = proposal["id"]
        n_a, r_a, s_a = self.actor("approver1")
        n_b, r_b, s_b = self.actor("approver2")
        self.control.approve(pid, n_a, r_a, self.code("approver1"), s_a)
        self.control.confirm(pid, n_b, r_b, self.code("approver2"), s_b,
                             emit_fn=we.emit_control_event)

        events = self.events_of("EmergencyPause")
        self.assertEqual(len(events), 1, "EmergencyPause обязан быть записан по факту применения")
        payload = json.loads(events[0]["payload"])
        self.assertEqual(payload["campaignId"], "talkchart_seo")
        self.assertEqual(payload["proposalId"], pid)
        self.assertIn("until", payload)

        # откат тоже эмитит событие — иначе в истории появляется дырка
        self.control.rollback(pid, n_a, r_a, self.code("approver1"), s_a,
                              emit_fn=we.emit_control_event)
        updated = self.events_of("CampaignUpdated")
        self.assertTrue(any(json.loads(e["payload"]).get("resumed") for e in updated))

    def test_03_audit_chain_is_intact_and_covers_the_whole_lifecycle(self):
        proposal = self.make_proposal("block_source",
                                      {"sourceId": "x_twitter", "reason": "abuse"})
        pid = proposal["id"]
        n_a, r_a, s_a = self.actor("approver1")
        n_b, r_b, s_b = self.actor("approver2")
        self.control.approve(pid, n_a, r_a, self.code("approver1"), s_a)
        self.control.confirm(pid, n_b, r_b, self.code("approver2"), s_b,
                             emit_fn=we.emit_control_event)

        chain = self.control.verify_audit_chain()
        self.assertTrue(chain["valid"], f"цепочка аудита нарушена: {chain}")
        actions = [e["action"] for e in self.control.recent_audit(limit=50)]
        for expected in ("proposal.create", "proposal.approve", "proposal.apply"):
            self.assertIn(expected, actions, f"в аудите нет {expected}")

        # AbuseBlocked — событие каталога, применённое через proposal
        blocked = self.events_of("AbuseBlocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(json.loads(blocked[0]["payload"])["targetRef"], "x_twitter")
        self.assertTrue(self.control.is_blocked("source", "x_twitter"))

        # rollback снимает блокировку
        self.control.rollback(pid, n_a, r_a, self.code("approver1"), s_a)
        self.assertFalse(self.control.is_blocked("source", "x_twitter"))

    def test_04_control_plane_is_disabled_without_configured_users(self):
        os.environ.pop("TRAFFICGEN_CONTROL_USERS", None)
        self.assertFalse(control_enabled(),
                         "без настроенных пользователей контроль обязан быть недоступен")

    def test_05_proposal_expires_by_ttl(self):
        os.environ["TRAFFICGEN_PROPOSAL_TTL_SECONDS"] = "1"
        import importlib
        import watchtower_control as wc
        importlib.reload(wc)
        try:
            store = wc.ControlStore(db_path=self.db_path)
            prop, err = store.create_proposal(
                "emergency_pause", {"campaignId": "talkchart_seo"}, "proposer", "proposer")
            self.assertIsNone(err)
            time.sleep(1.2)
            n_a, r_a, s_a = self.actor("approver1")
            _, err = store.approve(prop["id"], n_a, r_a, self.code("approver1"), s_a)
            self.assertIsNotNone(err)
            self.assertEqual(err["code"], "invalid_state")
        finally:
            importlib.reload(wc)

    def test_06_unknown_kind_and_invalid_payload_are_rejected(self):
        name, role, _ = self.actor("proposer")
        _, err = self.control.create_proposal("drop_database", {}, name, role)
        self.assertEqual(err["code"], "unknown_kind")
        _, err = self.control.create_proposal("emergency_pause", {}, name, role)
        self.assertEqual(err["code"], "invalid_payload")


# ---------------------------------------------------------------------------
# W2: эмиттеры, которых раньше не было
# ---------------------------------------------------------------------------
class TestDetectors(BaseCase):
    def _seed_session(self, session, events, source_id="direct_web", age_hours=0):
        base = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        for i, et in enumerate(events):
            self.store.record_event({
                "eventType": et,
                "campaignId": "talkchart_interactive_radar",
                "pageId": "target_terminal",
                "sessionId": session,
                "sourceId": source_id,
                "sourceType": "real",
                "seq": i + 1,
                "timestamp": iso(base + timedelta(seconds=i * 30)),
                "payload": {},
            })

    def test_07_health_probe_emits_exporter_health(self):
        probe = det.HealthProbe(self.store, snapshot_path=None)
        res, payload = probe.run()
        events = self.events_of("ExporterHealth")
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]["payload"])["status"], payload["status"])
        self.assertIn("intervalSeconds", payload)
        self.assertIn("checks", payload)

    def test_08_session_sweeper_emits_abandoned_once(self):
        self._seed_session("sess_idle", ["SessionStarted", "PageView"], age_hours=5)
        sweeper = det.SessionSweeper(self.store, self.control)
        first = sweeper.run()
        self.assertEqual(first, ["sess_idle"])
        second = sweeper.run()
        self.assertEqual(second, [], "повторный проход не должен создавать дубли")
        events = self.events_of("SessionAbandoned")
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0]["payload"])
        self.assertGreater(payload["idleSeconds"], det.SESSION_IDLE_TIMEOUT)

    def test_09_sweeper_ignores_already_ended_sessions(self):
        self._seed_session("sess_done", ["SessionStarted", "PageView", "SessionEnded"],
                           age_hours=5)
        sweeper = det.SessionSweeper(self.store, self.control)
        self.assertEqual(sweeper.run(), [])
        self.assertEqual(len(self.events_of("SessionAbandoned")), 0)

    def test_10_bot_classifier_flags_session_and_logs_decision(self):
        # Сессия без SessionStarted, с нечеловеческой скоростью и без кликов
        base = datetime.now(timezone.utc)
        for i in range(6):
            self.store.record_event({
                "eventType": "PageView",
                "campaignId": "talkchart_seo",
                "pageId": "target_terminal",
                "sessionId": "sess_bot",
                "sourceId": "direct_web",
                "sourceType": "real",
                "seq": i + 1,
                "timestamp": iso(base + timedelta(milliseconds=i * 5)),
                "payload": {"automated": True},
            })
        clf = det.BotClassifier(self.store, self.control)
        flagged = clf.run()
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["sessionId"], "sess_bot")
        events = self.events_of("BotFlagged")
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0]["payload"])
        self.assertGreaterEqual(payload["score"], payload["threshold"])
        self.assertIn("method", payload)
        self.assertIn("reasons", payload)

        quality = self.control.detector_quality()["bot_classifier"]
        self.assertGreaterEqual(quality["decisions"], 1)

    def test_11_bot_classifier_silent_on_short_sessions(self):
        self._seed_session("sess_short", ["PageView"])
        clf = det.BotClassifier(self.store, self.control)
        self.assertEqual(clf.run(), [], "короткие сессии не классифицируются")

    def test_12_anomaly_detector_reacts_to_spike_and_stays_silent_without_baseline(self):
        base = datetime.now(timezone.utc)
        # 24 часа ровного фона по 5 событий в час
        for h in range(24, 1, -1):
            ts = base - timedelta(hours=h)
            for i in range(5):
                self.store.record_event({
                    "eventType": "PageView",
                    "campaignId": "talkchart_seo",
                    "pageId": "target_terminal",
                    "sessionId": f"sess_bg_{h}_{i}",
                    "sourceId": "direct_web",
                    "sourceType": "real",
                    "seq": 1,
                    "timestamp": iso(ts + timedelta(minutes=i)),
                    "payload": {},
                })
        detector = det.AnomalyDetector(self.store, self.control)
        # Сплеск в текущем часу
        for i in range(80):
            self.store.record_event({
                "eventType": "PageView",
                "campaignId": "talkchart_seo",
                "pageId": "target_terminal",
                "sessionId": f"sess_spike_{i}",
                "sourceId": "direct_web",
                "sourceType": "real",
                "seq": 1,
                "timestamp": iso(base),
                "payload": {},
            })
        found = detector.run()
        self.assertTrue(found, "сплеск обязан быть обнаружен")
        payload = found[0]
        self.assertEqual(payload["direction"], "spike")
        self.assertGreaterEqual(abs(payload["sigmaScore"]), payload["threshold"])
        self.assertEqual(payload["method"], "robust_zscore_mad_v1")
        events = self.events_of("AnomalyDetected")
        self.assertEqual(len(events), 1)

    def test_13_anomaly_detector_silent_when_baseline_is_too_short(self):
        base = datetime.now(timezone.utc)
        for i in range(3):
            self.store.record_event({
                "eventType": "PageView", "campaignId": "talkchart_seo",
                "pageId": "target_terminal", "sessionId": f"s_few_{i}",
                "sourceId": "direct_web", "sourceType": "real", "seq": 1,
                "timestamp": iso(base - timedelta(hours=3)), "payload": {},
            })
        detector = det.AnomalyDetector(self.store, self.control)
        self.assertEqual(detector.run(), [], "при короткой базе детектор молчит")

    def test_14_traffic_error_event_is_in_catalog_and_recordable(self):
        res = self.store.record_event({
            "eventType": "TrafficError",
            "campaignId": "system",
            "pageId": "target_terminal",
            "sessionId": "sess_system_factory",
            "sourceId": "factory_pipeline",
            "sourceType": "bot",
            "seq": 1,
            "payload": {"stage": "fetch_data", "error": "timeout"},
        })
        self.assertEqual(res["status"], "accepted")
        self.assertEqual(len(self.events_of("TrafficError")), 1)


# ---------------------------------------------------------------------------
# W2: качество данных и честность метрик
# ---------------------------------------------------------------------------
class TestDataQuality(BaseCase):
    def test_15_daily_counters_replace_process_global_ones(self):
        self.store.record_event({"eventType": "PageView", "sessionId": "sess_q",
                                 "seq": 1, "campaignId": "talkchart_seo",
                                 "pageId": "target_terminal", "sourceId": "direct_web"})
        self.store.record_event({"eventType": "Bad", "sessionId": "sess_q", "seq": 0})  # rejected
        self.store.bump_daily_counter("paused")
        m = compute_metrics(1, self.store)
        today = datetime.now(timezone.utc).date().isoformat()
        day = next(d for d in m["days"] if d["date"] == today)
        self.assertGreaterEqual(day["integrity"]["rejected"], 1,
                                "per-day счётчик отклонённых обязан быть заполнен")
        self.assertEqual(day["errors"]["paused"], 1)
        # старая запись о «процесс-глобальных счётчиках» удалена из unavailable
        texts = " ".join(u["reason"] for u in m["unavailableMetrics"])
        self.assertNotIn("процесс-глобальн", texts)

    def test_16_dead_letter_keeps_rejected_with_reason(self):
        self.store.record_event({"eventType": "PageView", "sessionId": "sess_dlq", "seq": 0})
        stats = self.store.rejected_stats()
        self.assertGreaterEqual(stats["total"], 1)
        self.assertIn("schema", stats["byReason"])

    def test_17_session_duration_is_estimate_until_enough_explicit_completions(self):
        base = datetime.now(timezone.utc)
        for i in range(3):
            self.store.record_event({
                "eventType": "SessionStarted", "sessionId": f"sess_d{i}",
                "campaignId": "talkchart_seo", "pageId": "target_terminal",
                "sourceId": "direct_web", "sourceType": "real", "seq": 1,
                "timestamp": iso(base), "payload": {}})
            self.store.record_event({
                "eventType": "SessionEnded", "sessionId": f"sess_d{i}",
                "campaignId": "talkchart_seo", "pageId": "target_terminal",
                "sourceId": "direct_web", "sourceType": "real", "seq": 2,
                "timestamp": iso(base + timedelta(seconds=10 * (i + 1))),
                "payload": {"durationSeconds": 10 * (i + 1)}})
        m = compute_metrics(1, self.store)
        self.assertTrue(m["sessionDurationSeconds"]["estimate"],
                        "при малой выборке длительность остаётся оценкой")
        self.assertEqual(m["sessionDurationSeconds"]["explicitCompletions"], 3)

        # Добираем до порога — estimate обязан сняться
        min_needed = m["sessionDurationSeconds"]["minSessionsForExact"]
        for i in range(3, min_needed + 2):
            self.store.record_event({
                "eventType": "SessionStarted", "sessionId": f"sess_d{i}",
                "campaignId": "talkchart_seo", "pageId": "target_terminal",
                "sourceId": "direct_web", "sourceType": "real", "seq": 1,
                "timestamp": iso(base), "payload": {}})
            self.store.record_event({
                "eventType": "SessionEnded", "sessionId": f"sess_d{i}",
                "campaignId": "talkchart_seo", "pageId": "target_terminal",
                "sourceId": "direct_web", "sourceType": "real", "seq": 2,
                "timestamp": iso(base + timedelta(seconds=20)),
                "payload": {"durationSeconds": 20}})
        m2 = compute_metrics(1, self.store)
        self.assertFalse(m2["sessionDurationSeconds"]["estimate"],
                         "при достаточном числе явных завершений оценка снимается")

    def test_18_identity_binding_hashes_only(self):
        self.store.record_event({
            "eventType": "CTAClicked", "sessionId": "sess_id", "seq": 1,
            "campaignId": "talkchart_seo", "pageId": "target_terminal",
            "sourceId": "direct_web", "sourceType": "real",
            "payload": {"externalId": "player-42"}})
        stats = self.control.identity_stats()
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["boundToExternalId"], 1)
        with self.store.get_conn() as conn:
            raw = conn.execute("SELECT * FROM identity_bindings").fetchone()
        self.assertNotIn("player-42", json.dumps(dict(raw)),
                         "сырой идентификатор не должен попадать в БД")

    def test_19_consent_registry_and_latency_samples(self):
        state = self.control.record_consent("hash123", "opt_out", source="landing")
        self.assertEqual(state["byDecision"]["opt_out"], 1)
        for ms in (10, 20, 30, 400, 900):
            self.store.record_latency("track", ms)
        pct = self.store.latency_percentiles(route="track")
        self.assertEqual(pct["samples"], 5)
        self.assertGreaterEqual(pct["p95"], pct["p50"])

    def test_20_catalog_closed_events_are_marked_implemented(self):
        implemented = set(we.IMPLEMENTED_EVENTS)
        for et in ("ExporterHealth", "SessionAbandoned", "BotFlagged", "AnomalyDetected",
                   "TrafficError", "EmergencyPause", "AbuseBlocked", "NavigationCompleted"):
            self.assertIn(et, implemented, f"{et} заявлен реализованным, но его нет в каталоге")
        # честно недоступное осталось недоступным с причиной
        unavailable = {u["eventType"]: u["reason"] for u in we.UNAVAILABLE_EVENTS}
        for et in ("LandingReached", "DeliveryFailed", "RetryScheduled", "ConfigUpdated"):
            self.assertIn(et, unavailable)
            self.assertTrue(unavailable[et])


if __name__ == "__main__":
    unittest.main(verbosity=2)
