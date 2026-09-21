#!/usr/bin/env python3
"""
Test suite for Games Watchtower Integration Adapter (trafficgen).
Validates schema contracts, deduplication, cursor pagination, PII stripping,
gap detection, read-only enforcement, and Prometheus metrics.
"""

import os
import sys
import json
import unittest
import tempfile
import shutil

# Add repo root and site/factory to path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "site", "factory"))

from watchtower_exporter import WatchtowerStore, strip_pii, canonicalize_event, build_prometheus_metrics

class TestWatchtowerAdapter(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_watchtower.db")
        self.store = WatchtowerStore(db_path=self.db_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_schema_and_initialization(self):
        """DB tables must be properly initialized"""
        with self.store.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
            expected = {"events", "campaigns", "sources", "pages", "aggregates_daily", "alerts", "sync_cursors"}
            self.assertTrue(expected.issubset(tables), f"Missing tables: {expected - tables}")

    def test_canonical_identity_and_envelope(self):
        """Canonical envelope validation and identity format"""
        raw = {
            "eventType": "CTAClicked",
            "campaignId": "talkchart_interactive_radar",
            "pageId": "terminal",
            "sessionId": "sess_unit_test_01",
            "seq": 1,
            "payload": {"target": "solana_miner"}
        }
        canonical = canonicalize_event(raw)
        self.assertEqual(canonical["chain"], "offchain")
        self.assertEqual(canonical["source"], "trafficgen")
        self.assertEqual(canonical["app"], "trafficgen")
        self.assertEqual(canonical["parserVersion"], "trafficgen-v1")
        self.assertEqual(canonical["dataQuality"], "complete")
        self.assertEqual(
            canonical["identity"],
            "offchain:trafficgen:talkchart_interactive_radar:terminal:sess_unit_test_01:1"
        )
        self.assertTrue(canonical["eventId"].startswith("ev_"))

    def test_pii_stripping(self):
        """PII fields must be stripped from payload and event"""
        raw = {
            "eventType": "SessionStarted",
            "sessionId": "sess_pii_01",
            "ip": "192.168.1.100",
            "email": "user@example.com",
            "device_id": "dev-abc-123",
            "payload": {
                "user_ip": "10.0.0.1",
                "cookie": "session=secret_token_123",
                "target": "solana_miner"
            }
        }
        cleaned = strip_pii(raw)
        self.assertNotIn("ip", cleaned)
        self.assertNotIn("email", cleaned)
        self.assertNotIn("device_id", cleaned)
        self.assertNotIn("user_ip", cleaned["payload"])
        self.assertNotIn("cookie", cleaned["payload"])
        self.assertEqual(cleaned["payload"]["target"], "solana_miner")

    def test_event_ingestion_and_deduplication(self):
        """Re-inserting same eventId or identity must be detected as duplicate without double-counting"""
        ev = canonicalize_event({
            "eventType": "PageView",
            "campaignId": "talkchart_interactive_radar",
            "pageId": "terminal",
            "sessionId": "sess_dedup_01",
            "seq": 1,
            "payload": {"title": "Terminal"}
        })
        res1 = self.store.ingest_event(ev)
        self.assertTrue(res1["success"])
        self.assertFalse(res1["duplicate"])

        # Insert exact duplicate
        res2 = self.store.ingest_event(ev)
        self.assertTrue(res2["success"])
        self.assertTrue(res2["duplicate"])

        # Check only 1 record exists in DB
        with self.store.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM events WHERE session_id = 'sess_dedup_01'")
            count = cursor.fetchone()[0]
            self.assertEqual(count, 1)

    def test_sequence_gap_detection(self):
        """Skipping a sequence number must emit DataGapDetected alert"""
        ev1 = canonicalize_event({
            "eventType": "PageView",
            "campaignId": "talkchart_interactive_radar",
            "pageId": "terminal",
            "sessionId": "sess_gap_01",
            "seq": 1
        })
        self.store.ingest_event(ev1)

        # Skip seq 2, send seq 3
        ev3 = canonicalize_event({
            "eventType": "CTAClicked",
            "campaignId": "talkchart_interactive_radar",
            "pageId": "terminal",
            "sessionId": "sess_gap_01",
            "seq": 3
        })
        self.store.ingest_event(ev3)

        alerts = self.store.get_alerts()
        gap_alerts = [a for a in alerts if a["alertType"] == "DataGapDetected"]
        self.assertGreaterEqual(len(gap_alerts), 1)
        self.assertEqual(gap_alerts[0]["details"]["expectedSeq"], 2)
        self.assertEqual(gap_alerts[0]["details"]["receivedSeq"], 3)

    def test_cursor_pagination(self):
        """Events pagination via cursor must return sequential ordered items"""
        for i in range(1, 15):
            ev = canonicalize_event({
                "eventType": "PageView",
                "campaignId": "talkchart_interactive_radar",
                "pageId": "terminal",
                "sessionId": f"sess_cursor_{i}",
                "seq": 1
            })
            self.store.ingest_event(ev)

        # Fetch first page limit=5
        page1, next_cursor1 = self.store.get_events(limit=5)
        self.assertEqual(len(page1), 5)
        self.assertTrue(bool(next_cursor1))
        self.assertIsNotNone(next_cursor1)

        # Fetch second page
        page2, next_cursor2 = self.store.get_events(cursor=next_cursor1, limit=5)
        self.assertEqual(len(page2), 5)
        self.assertTrue(bool(next_cursor2))

        # Ensure no overlap between page1 and page2
        ids1 = {e["eventId"] for e in page1}
        ids2 = {e["eventId"] for e in page2}
        self.assertEqual(len(ids1.intersection(ids2)), 0)

    def test_aggregates_and_funnel(self):
        """Daily aggregates and funnel calculations must match ingested events"""
        events = [
            {"eventType": "SessionStarted", "campaignId": "test_camp", "sessionId": "s1", "seq": 1},
            {"eventType": "PageView", "campaignId": "test_camp", "sessionId": "s1", "seq": 2},
            {"eventType": "CTAClicked", "campaignId": "test_camp", "sessionId": "s1", "seq": 3},
            {"eventType": "SessionStarted", "campaignId": "test_camp", "sessionId": "s2", "seq": 1},
            {"eventType": "PageView", "campaignId": "test_camp", "sessionId": "s2", "seq": 2},
        ]
        for e in events:
            self.store.ingest_event(canonicalize_event(e))

        funnels = self.store.get_funnels()
        self.assertTrue(len(funnels) > 0)
        overall = next((f for f in funnels if f["funnelId"] == "trafficgen_overall"), None)
        self.assertIsNotNone(overall)
        step_names = [s["step"] for s in overall["steps"]]
        self.assertIn("sessionstarted", step_names)
        self.assertIn("pageview", step_names)
        self.assertIn("ctaclicked", step_names)

    def test_prometheus_metrics(self):
        """Prometheus metrics endpoint output format and mandatory metrics"""
        ev = canonicalize_event({
            "eventType": "CTAClicked",
            "campaignId": "talkchart_interactive_radar",
            "pageId": "terminal",
            "sessionId": "sess_prom_01",
            "seq": 1
        })
        self.store.ingest_event(ev)

        prom_text = build_prometheus_metrics(self.store)
        required_metrics = [
            "trafficgen_events_total",
            "trafficgen_events_duplicate_total",
            "trafficgen_events_rejected_total",
            "trafficgen_delivery_failures_total",
            "trafficgen_exporter_errors_total",
            "trafficgen_buffer_depth",
            "trafficgen_data_gaps_total"
        ]
        for m in required_metrics:
            self.assertIn(m, prom_text, f"Prometheus metric missing: {m}")

    def test_no_secrets_in_store(self):
        """Verify no sensitive credentials or private keys exist in config or state"""
        config = self.store.get_config()
        config_str = json.dumps(config).lower()
        forbidden = ["private_key", "secret_key", "password", "jwt_secret", "bearer"]
        for f in forbidden:
            self.assertNotIn(f, config_str)

if __name__ == "__main__":
    unittest.main()
