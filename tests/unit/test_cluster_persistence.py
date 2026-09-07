"""Unit tests for Frontier 2: Distributed State Synchronization (Clustered Gateways)."""

import unittest

from durallm.breaker.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerState
from durallm.storage.cluster import ClusterPersistenceStore


class TestClusterPersistenceStore(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ClusterPersistenceStore()

    def test_session_lifecycle_with_cas_revisions(self) -> None:
        # Create session
        created = self.store.create_session("sess-1", "acp.v1", {"step": 1})
        self.assertIsNotNone(created)
        assert created is not None
        self.assertEqual(created.revision, 1)

        # Duplicate create rejected
        self.assertIsNone(self.store.create_session("sess-1", "acp.v1", {"step": 1}))

        # Load session
        loaded = self.store.load_session("sess-1")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.payload["step"], 1)

        # Stale revision rejected (CAS)
        self.assertIsNone(self.store.save_session("sess-1", "acp.v1", {"step": 2}, expected_revision=99))

        # Valid revision succeeds
        saved = self.store.save_session("sess-1", "acp.v1", {"step": 2}, expected_revision=1)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.revision, 2)
        self.assertEqual(saved.payload["step"], 2)

    def test_attempt_lifecycle(self) -> None:
        att = self.store.prepare_attempt("att-1", "req-1", {"model": "gpt-4o"})
        self.assertEqual(att.status, "PREPARED")

        loaded = self.store.load_attempt("att-1")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.request_id, "req-1")

        by_req = self.store.attempts_for_request("req-1")
        self.assertEqual(len(by_req), 1)

        finished = self.store.finish_attempt("att-1", "COMPLETED", {"tokens": 120})
        self.assertEqual(finished.status, "COMPLETED")
        self.assertEqual(finished.payload["tokens"], 120)

    def test_distributed_leasing_and_fencing_tokens(self) -> None:
        lease1 = self.store.acquire_lease("tool:bash_deploy", "pod-alpha", ttl_seconds=60.0)
        self.assertIsNotNone(lease1)
        assert lease1 is not None
        self.assertEqual(lease1.owner_id, "pod-alpha")
        self.assertEqual(lease1.fencing_token, 1)

        # Competing pod cannot acquire active lease
        lease2 = self.store.acquire_lease("tool:bash_deploy", "pod-beta", ttl_seconds=60.0)
        self.assertIsNone(lease2)

        # Release lease
        released = self.store.release_lease("tool:bash_deploy", "pod-alpha", fencing_token=1)
        self.assertTrue(released)

        # Now pod-beta can acquire with strictly higher monotonic fencing token
        lease3 = self.store.acquire_lease("tool:bash_deploy", "pod-beta", ttl_seconds=60.0)
        self.assertIsNotNone(lease3)
        assert lease3 is not None
        self.assertEqual(lease3.owner_id, "pod-beta")
        self.assertEqual(lease3.fencing_token, 2)

    def test_distributed_token_bucket_rate_limiting(self) -> None:
        # 10 tokens capacity, 1 token/sec refill
        allowed = self.store.consume_tokens("rpm:groq", tokens=5, capacity=10, refill_rate_per_sec=1.0)
        self.assertTrue(allowed)

        allowed2 = self.store.consume_tokens("rpm:groq", tokens=5, capacity=10, refill_rate_per_sec=1.0)
        self.assertTrue(allowed2)

        # Exhausted
        denied = self.store.consume_tokens("rpm:groq", tokens=1, capacity=10, refill_rate_per_sec=1.0)
        self.assertFalse(denied)

    def test_multi_node_circuit_breaker_synchronization(self) -> None:
        cluster_store = ClusterPersistenceStore()

        # Pod Alpha breaker
        cfg = CircuitBreakerConfig(minimum_number_of_calls=2, wait_duration_open_ms=1000)
        breaker_pod_alpha = CircuitBreaker("groq-primary", config=cfg, cluster_store=cluster_store)

        # Pod Beta breaker (independent pod instance)
        breaker_pod_beta = CircuitBreaker("groq-primary", config=cfg, cluster_store=cluster_store)

        self.assertEqual(breaker_pod_alpha.state, CircuitBreakerState.CLOSED)
        self.assertEqual(breaker_pod_beta.state, CircuitBreakerState.CLOSED)

        # Pod Alpha suffers failures and trips to OPEN
        breaker_pod_alpha.record_failure(0.1)
        breaker_pod_alpha.record_failure(0.1)
        self.assertEqual(breaker_pod_alpha.state, CircuitBreakerState.OPEN)

        # Verify cluster store received transition
        synced = cluster_store.get_cluster_breaker_state("groq-primary")
        self.assertIsNotNone(synced)
        assert synced is not None
        self.assertEqual(synced["state"], "OPEN")

        # Pod Beta checks state and immediately trips to OPEN without making failing calls!
        self.assertEqual(breaker_pod_beta.state, CircuitBreakerState.OPEN)


if __name__ == "__main__":
    unittest.main()
