"""Unit tests for Autonomous Canary Probing and Quirks Ledger Maintenance."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from durallm.canary import (
    CanaryProber,
    NightlyCanaryScheduler,
    compute_seconds_until_next_1am_uk,
    get_uk_timezone,
)
from durallm.pools import IsolatedPoolManager


class TestCanaryEngine(unittest.TestCase):
    """Test suite covering the Canary Prober, Quirks Ledger, and Nightly Scheduler."""

    def test_compute_seconds_until_next_1am_uk(self):
        tz = get_uk_timezone()
        # Case 1: Today at 23:00 (11 PM) -> Next 1 AM is in 2 hours (7200s)
        test_dt1 = datetime(2026, 9, 7, 23, 0, 0, tzinfo=tz)
        sec1 = compute_seconds_until_next_1am_uk(test_dt1)
        self.assertAlmostEqual(sec1, 7200.0, delta=2.0)

        # Case 2: Today at 01:30 (1:30 AM) -> Next 1 AM is tomorrow (23.5 hours = 84600s)
        test_dt2 = datetime(2026, 9, 7, 1, 30, 0, tzinfo=tz)
        sec2 = compute_seconds_until_next_1am_uk(test_dt2)
        self.assertAlmostEqual(sec2, 84600.0, delta=2.0)

        # Case 3: Live now -> Must be > 0 and <= 86400
        sec_live = compute_seconds_until_next_1am_uk()
        self.assertGreaterEqual(sec_live, 0.0)
        self.assertLessEqual(sec_live, 86400.0)

    def test_ledger_load_and_save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "test_quirks.json"
            prober = CanaryProber(ledger_path=ledger_path)

            # Initially empty/default
            data = prober.load_ledger()
            self.assertIn("version", data)
            self.assertIn("providers", data)

            # Modify and save
            data["test_key"] = "test_value"
            prober.save_ledger(data)

            # Reload and verify
            reloaded = prober.load_ledger()
            self.assertEqual(reloaded["test_key"], "test_value")

    @patch("urllib.request.urlopen")
    def test_fetch_openrouter_free_models(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "data": [
                {
                    "id": "test/model-free:free",
                    "name": "Test Free Model",
                    "context_length": 128000,
                    "pricing": {"prompt": "0", "completion": "0"},
                    "supported_parameters": ["tools"],
                },
                {
                    "id": "test/paid-model",
                    "name": "Paid Model",
                    "context_length": 32000,
                    "pricing": {"prompt": "0.001", "completion": "0.002"},
                },
            ]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        prober = CanaryProber()
        models = prober.fetch_openrouter_free_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["id"], "test/model-free:free")
        self.assertTrue(models[0]["supports_tools"])

    def test_ping_endpoint_no_key(self):
        prober = CanaryProber()
        res = prober.ping_endpoint("groq", "qwen", "https://api.groq.com", api_key="")
        self.assertEqual(res["status"], "skipped")

    @patch("urllib.request.urlopen")
    def test_ping_endpoint_active_and_headers(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {
            "x-ratelimit-limit-tokens": "6000",
            "x-ratelimit-limit-requests": "30",
        }
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        prober = CanaryProber()
        res = prober.ping_endpoint("groq", "qwen", "https://api.groq.com", api_key="valid-key")
        self.assertEqual(res["status"], "active")
        self.assertEqual(res["rate_limit_tokens"], "6000")
        self.assertGreater(res["latency_ms"], 0.0)

    def test_scheduler_status(self):
        prober = CanaryProber()
        scheduler = NightlyCanaryScheduler(prober=prober, target_hour=1, target_minute=0)
        st = scheduler.status()
        self.assertEqual(st["target_schedule"], "01:00 Europe/London")
        self.assertFalse(st["running"])
        self.assertGreater(st["seconds_until_next_run"], 0)
        self.assertTrue(
            any(tz_name in st["next_scheduled_run_uk"] for tz_name in ["BST", "GMT", "UTC", "+01", "+00"])
        )

    @patch.object(CanaryProber, "run_probe")
    def test_scheduler_trigger_now(self, mock_run_probe):
        mock_run_probe.return_value = {
            "status": "success",
            "timestamp": "2026-09-07T00:00:00Z",
            "discovered_free_models": 2,
        }
        scheduler = NightlyCanaryScheduler()
        res = scheduler.trigger_now()
        self.assertEqual(res["status"], "success")
        self.assertEqual(scheduler.last_run_utc, "2026-09-07T00:00:00Z")

    def test_pool_manager_sync_from_quirks_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_file = Path(tmpdir) / "quirks.json"
            sample_ledger = {
                "version": "1.0.0",
                "providers": {
                    "openrouter": {
                        "base_url": "https://openrouter.ai/api/v1",
                        "auth_env": "OPENROUTER_API_KEY",
                        "models": {
                            "custom/test-coding:free": {
                                "pool": "coding",
                                "context_window": 128000,
                                "max_output_tokens": 4096,
                                "status": "active",
                            },
                            "retired/old-model:free": {
                                "pool": "coding",
                                "status": "deprecated",
                            },
                        },
                    }
                },
            }
            with open(ledger_file, "w", encoding="utf-8") as f:
                json.dump(sample_ledger, f)

            pool_mgr = IsolatedPoolManager()
            pool_mgr.load_from_quirks_ledger(ledger_file)

            # Verify active route added
            coding_models = [r.model for r in pool_mgr.coding_routes]
            self.assertIn("custom/test-coding:free", coding_models)

            # Verify deprecated model marked deprecated
            self.assertIn(("coding", "retired/old-model:free"), pool_mgr.deprecated)


if __name__ == "__main__":
    unittest.main()
