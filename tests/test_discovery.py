import unittest
from pathlib import Path
from unittest.mock import patch

from llm_circuit_breaker.discovery import (
    discover_models,
    is_model_free,
    supports_tool_calling,
)


class TestModelDiscovery(unittest.TestCase):

    def setUp(self):
        self.test_path = Path("/tmp/test_pkg_catalog.json")
        if self.test_path.exists():
            self.test_path.unlink()

    def tearDown(self):
        if self.test_path.exists():
            self.test_path.unlink()

    def test_filters(self):
        self.assertTrue(is_model_free({"prompt": "0", "completion": "0"}))
        self.assertFalse(is_model_free({"prompt": "0.001", "completion": "0"}))
        self.assertTrue(supports_tool_calling({"supported_parameters": ["tools"]}))
        self.assertFalse(supports_tool_calling({"supported_parameters": ["temp"]}))

    def test_discovery_and_deprecation(self):
        raw = [
            {"id": "good/model-1:free", "context_length": 65536, "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
            {"id": "paid/model-2", "context_length": 65536, "pricing": {"prompt": "0.01", "completion": "0.02"}, "supported_parameters": ["tools"]},
            {"id": "small/model-3:free", "context_length": 4096, "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        ]
        with patch("llm_circuit_breaker.discovery.fetch_openrouter_catalog", return_value=raw):
            catalog = discover_models(force=True, catalog_path=self.test_path)

        self.assertEqual(catalog["total_free_models_discovered"], 1)
        self.assertEqual(catalog["free_models"][0]["id"], "good/model-1:free")

if __name__ == "__main__":
    unittest.main()
