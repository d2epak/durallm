"""Table-driven guard for the failure taxonomy flags.

Encodes docs/FAILURE_TAXONOMY.md and ADR 0003: client/request faults (4xx other
than 402/429) must never poison provider health and must not be retried on the
same endpoint; unknown failures must still allow fallback.
"""

import unittest

from durallm.classifier import classify_failure
from durallm.models import FailoverReason, FailureCategory


class SSLError(Exception):
    pass


class TestClassifierTable(unittest.TestCase):

    def test_status_code_table(self):
        # (status, message, headers) -> (category, reason, should_fallback, retryable, poisons_health)
        table = [
            ((400, "unsupported parameter: tools", None),
             (FailureCategory.REQUEST_INCOMPATIBILITY, FailoverReason.client_error, True, False, False)),
            ((422, "unprocessable entity", None),
             (FailureCategory.REQUEST_INCOMPATIBILITY, FailoverReason.client_error, True, False, False)),
            ((401, "invalid api key", None),
             (FailureCategory.CLIENT_FAULT, FailoverReason.auth, True, False, False)),
            ((404, "model not found", None),
             (FailureCategory.REQUEST_INCOMPATIBILITY, FailoverReason.model_not_found, True, False, False)),
            ((413, "request too large", None),
             (FailureCategory.REQUEST_INCOMPATIBILITY, FailoverReason.payload_too_large, True, False, False)),
            ((429, "rate limit reached", {"retry-after": "30"}),
             (FailureCategory.RATE_LIMIT, FailoverReason.rate_limit, True, True, True)),
            ((500, "internal error", None),
             (FailureCategory.INFRASTRUCTURE, FailoverReason.server_error, True, True, True)),
            ((503, "overloaded", None),
             (FailureCategory.INFRASTRUCTURE, FailoverReason.overloaded, True, True, True)),
            ((None, "something nobody has seen before", None),
             (FailureCategory.UNKNOWN, FailoverReason.unknown, True, True, True)),
        ]
        for (status, msg, headers), expected in table:
            c = classify_failure(Exception(msg), status_code=status, headers=headers)
            got = (c.category, c.reason, c.should_fallback, c.retryable, c.poisons_health)
            self.assertEqual(got, expected, f"status={status} msg={msg!r}")

    def test_429_carries_retry_after(self):
        c = classify_failure(Exception("rate limit"), status_code=429, headers={"Retry-After": "30"})
        self.assertEqual(c.retry_after_seconds, 30.0)

    def test_ssl_error_type_name_is_detected(self):
        c = classify_failure(SSLError("handshake failed"), status_code=None)
        self.assertEqual(c.reason, FailoverReason.ssl_cert_verification)


if __name__ == "__main__":
    unittest.main()
