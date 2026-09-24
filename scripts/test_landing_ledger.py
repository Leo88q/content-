#!/usr/bin/env python3
"""Тесты click-id round-trip для LandingReached (site/factory/landings.py).

Проверяют ровно то, что делает подтверждение перехода честным:
клик регистрируется один раз, подтверждение возможно только по известному clickId,
повторное подтверждение не создаёт второй переход, невалидный идентификатор отбивается,
а через store неподтверждённый LandingReached не доходит ни до воронки, ни до счётчиков.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "site", "factory"))

import landings
from landings import LandingLedger, valid_click_id, cta_href
from watchtower_exporter import WatchtowerStore


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="landings-")
        self.ledger = LandingLedger(os.path.join(self.dir, "landings.sqlite3"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_register_is_idempotent(self):
        first = self.ledger.register_click("click-0001", session_id="s1", campaign_id="c1",
                                           source_id="x_twitter", page_id="target_sixsec")
        again = self.ledger.register_click("click-0001", session_id="s1")
        self.assertEqual(first["status"], "registered")
        self.assertEqual(again["status"], "already_registered")
        self.assertEqual(self.ledger.stats()["clicks"], 1)

    def test_confirm_requires_registration(self):
        self.assertEqual(self.ledger.confirm("click-nope")["reason"], "landing_unconfirmed")
        self.ledger.register_click("click-0002")
        self.assertEqual(self.ledger.confirm("click-0002")["status"], "confirmed")
        # повторное подтверждение не добавляет переход
        self.assertEqual(self.ledger.confirm("click-0002")["status"], "duplicate")
        stats = self.ledger.stats()
        self.assertEqual((stats["clicks"], stats["confirmed"]), (1, 1))
        self.assertEqual(stats["confirmationRate"], 1.0)

    def test_empty_denominator_is_none_not_zero(self):
        stats = self.ledger.stats()
        self.assertEqual(stats["clicks"], 0)
        self.assertIsNone(stats["confirmationRate"], "пустой знаменатель нельзя превращать в 0.0")

    def test_bad_click_id_rejected(self):
        for bad in ("short", "x" * 65, "has space", "slash/id", None, 12):
            self.assertFalse(valid_click_id(bad), bad)
            self.assertEqual(self.ledger.register_click(bad)["reason"], "bad_click_id")

    def test_prune_drops_expired(self):
        self.ledger.register_click("click-old")
        with self.ledger._conn() as c:  # переносим created_at в прошлое
            c.execute("UPDATE landing_clicks SET created_at='2000-01-01T00:00:00Z'")
        self.assertEqual(self.ledger.prune(keep_days=1), 1)
        self.assertEqual(self.ledger.stats()["clicks"], 0)

    def test_targets_allowlist_and_href(self):
        url = cta_href("https://example.site/", "target_duel", "click-0003")
        self.assertEqual(url, "https://example.site/r/click-0003?to=target_duel")
        with self.assertRaises(ValueError):
            cta_href("https://example.site/", "https://evil.example", "click-0003")

    def test_env_targets_override(self):
        os.environ["TALKCHART_LANDING_TARGETS"] = json.dumps({"target_only": "#/only"})
        try:
            self.assertEqual(landings.targets(), {"target_only": "#/only"})
        finally:
            del os.environ["TALKCHART_LANDING_TARGETS"]


class TestStoreIntegration(unittest.TestCase):
    """События проходят через тот же путь, что и HTTP-приём: record_event."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="landings-store-")
        self.store = WatchtowerStore(db_path=os.path.join(self.dir, "watchtower.db"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _event(self, et, seq, **extra):
        ev = {
            "eventType": et, "campaignId": "talkchart_social_x", "sourceId": "x_twitter",
            "sourceType": "real", "pageId": "target_sixsec", "sessionId": f"s{seq}",
            "seq": seq, "timestamp": "2026-09-24T12:00:00Z", "payload": dict(extra),
        }
        return ev

    def test_unconfirmed_landing_is_rejected(self):
        res = self.store.record_event(self._event("LandingReached", 1))
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["reason"], "landing_unconfirmed")
        self.assertEqual(self.store.ledger.stats()["clicks"], 0)

    def test_cta_registers_and_landing_confirms(self):
        click = "store-click-0001"
        cta = self.store.record_event(self._event("CTAClicked", 2, clickId=click))
        self.assertEqual(cta["status"], "accepted")
        self.assertEqual(self.store.ledger.stats()["clicks"], 1)
        landing = self.store.record_event(self._event("LandingReached", 3, clickId=click))
        self.assertEqual(landing["status"], "accepted")
        self.assertEqual(self.store.ledger.stats()["confirmed"], 1)
        # счётчик событий не вырос на отклонённые: только CTAClicked + LandingReached + PageView-нет
        self.assertEqual(self.store.ledger.stats()["pending"], 0)

    def test_funnel_counts_only_confirmed(self):
        for seq, (et, payload) in enumerate([
            ("CTAClicked", {"clickId": "funnel-click-1"}),
            ("LandingReached", {"clickId": "funnel-click-1"}),
            ("LandingReached", {"clickId": "funnel-click-unknown-9"}),
        ], start=1):
            self.store.record_event(self._event(et, seq, **payload))
        from watchtower_exporter import compute_funnel
        steps = {s["stage"]: s for s in compute_funnel(self.store, period_days=7)["steps"]}
        self.assertEqual(steps["LandingReached"]["count"], 1,
                         "в воронку обязан попасть только подтверждённый переход")


if __name__ == "__main__":
    unittest.main(verbosity=2)
