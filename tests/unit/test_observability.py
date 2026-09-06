"""Structured event logging: redaction on the wire and executor/proxy wiring."""

import io
import json
import logging
import unittest

from llm_circuit_breaker.errors import NoHealthyRouteError
from llm_circuit_breaker.execution.policy import RetryPolicy
from llm_circuit_breaker.observability.logger import StructuredJsonLogger
from tests.faults.mock_provider import MockFaultAction
from tests.faults.test_executor_backoff import build_executor, make_request


def _events(buf: io.StringIO):
    return [json.loads(line) for line in buf.getvalue().splitlines()]


class TestStructuredJsonLogger(unittest.TestCase):

    def test_stream_output_is_one_redacted_json_object_per_line(self):
        buf = io.StringIO()
        log = StructuredJsonLogger(stream=buf)
        log.info("attempt", request_id="req_1", api_key="sk-" + "a" * 30, input_tokens=12)
        log.error("boom", detail="Authorization: Bearer abc123")
        events = _events(buf)
        self.assertEqual([e["event"] for e in events], ["attempt", "boom"])
        self.assertEqual(events[0]["data"], {"api_key": "[REDACTED]", "input_tokens": 12})
        self.assertEqual(events[1]["level"], "ERROR")
        self.assertNotIn("abc123", events[1]["data"]["detail"])

    def test_default_sink_is_the_stdlib_logger_so_hosts_control_handlers(self):
        log = StructuredJsonLogger()
        with self.assertLogs("llm_circuit_breaker.events", level="INFO") as cm:
            log.warning("slow", latency_ms=900)
        self.assertEqual(cm.records[0].levelno, logging.WARNING)
        self.assertEqual(json.loads(cm.records[0].getMessage())["data"], {"latency_ms": 900})


class TestExecutorEvents(unittest.TestCase):

    def test_each_attempt_and_the_outcome_are_reported(self):
        ex, mock_a, mock_b, _ = build_executor(RetryPolicy(max_attempts_same_endpoint=1, jitter=False))
        buf = io.StringIO()
        ex.events = StructuredJsonLogger(stream=buf)
        mock_a.set_sequence([MockFaultAction.server_error(503)])
        mock_b.set_sequence([MockFaultAction.success("via b")])

        ex.execute(make_request(), pool="coding", strategy="priority", api_keys={"K": "sk-" + "z" * 30})

        events = _events(buf)
        self.assertEqual([e["event"] for e in events], ["upstream_attempt_failed", "upstream_attempt_succeeded"])
        failed, ok = events
        self.assertEqual((failed["level"], failed["data"]["endpoint_id"], failed["data"]["status_code"]), ("WARNING", "ep-a", 503))
        self.assertEqual(failed["data"]["reason"], "overloaded")  # 503 classifies as overloaded
        self.assertEqual((ok["data"]["endpoint_id"], ok["data"]["fallback_index"]), ("ep-b", 1))
        self.assertTrue(all(e["request_id"] for e in events))
        self.assertNotIn("z" * 30, buf.getvalue())

    def test_exhaustion_is_reported_as_an_error_event(self):
        ex, mock_a, mock_b, _ = build_executor(RetryPolicy(max_attempts_same_endpoint=1, jitter=False))
        buf = io.StringIO()
        ex.events = StructuredJsonLogger(stream=buf)
        mock_a.set_sequence([MockFaultAction.server_error(503)])
        mock_b.set_sequence([MockFaultAction.server_error(503)])

        with self.assertRaises(NoHealthyRouteError):
            ex.execute(make_request(), pool="coding", strategy="priority")

        last = _events(buf)[-1]
        self.assertEqual((last["event"], last["level"], last["data"]["attempts"], last["data"]["last_reason"]),
                         ("request_exhausted", "ERROR", 2, "overloaded"))


if __name__ == "__main__":
    unittest.main()
