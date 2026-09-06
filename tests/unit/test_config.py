"""GatewayConfig must produce objects the breaker and executor actually accept."""

import unittest

from llm_circuit_breaker.breaker.circuit_breaker import CircuitBreakerConfig
from llm_circuit_breaker.config import GatewayConfig


class TestGatewayConfig(unittest.TestCase):

    def test_default_config_builds_a_breaker_config(self):
        # Regression: the field used to be `wait_duration_in_open_seconds`, which
        # CircuitBreakerConfig does not have, so every call raised TypeError.
        cfg = GatewayConfig().to_breaker_config()

        self.assertIsInstance(cfg, CircuitBreakerConfig)
        self.assertEqual(cfg.wait_duration_open_ms, 30000.0)
        self.assertEqual(cfg.failure_rate_threshold, 50.0)
        self.assertEqual(cfg.sliding_window_size, 10)
        self.assertEqual(cfg.half_open_max_calls, 3)

    def test_wait_duration_is_converted_from_seconds_to_milliseconds(self):
        cfg = GatewayConfig(breaker_wait_duration_in_open=2.5).to_breaker_config()
        self.assertEqual(cfg.wait_duration_open_ms, 2500.0)

    def test_default_port_matches_the_proxy_default(self):
        # README, docs, GatewayConfig and the proxy CLI used to disagree (8000 / 8080 / 4001).
        import inspect
        from llm_circuit_breaker.proxy import start_proxy_server

        self.assertEqual(GatewayConfig().port, 4001)
        self.assertEqual(inspect.signature(start_proxy_server).parameters["port"].default, GatewayConfig().port)

    def test_from_dict_ignores_unknown_keys(self):
        gw = GatewayConfig.from_dict({"port": 9999, "not_a_field": 1})
        self.assertEqual(gw.port, 9999)


if __name__ == "__main__":
    unittest.main()
