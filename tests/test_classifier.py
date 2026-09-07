import unittest
from unittest.mock import MagicMock

from durallm.classifier import FailoverReason, classify_api_error


class MockAPIError(Exception):
    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {"error": {"message": message}}
        self.response = MagicMock(status_code=status_code)

class TestClassifierTaxonomy(unittest.TestCase):

    def test_all_status_codes_and_patterns(self):
        cases = [
            (MockAPIError("Rate limit reached", status_code=429), FailoverReason.rate_limit, True),
            (MockAPIError("poolside/model:free is temporarily rate-limited upstream", status_code=429), FailoverReason.upstream_rate_limit, True),
            (MockAPIError("Insufficient credits. Add more", status_code=402), FailoverReason.billing, True),
            (MockAPIError("Access denied", status_code=403), FailoverReason.auth, True),
            (MockAPIError("Model does not exist", status_code=404), FailoverReason.model_not_found, True),
            (MockAPIError("Model has been deprecated", status_code=400), FailoverReason.model_not_found, True),
            (MockAPIError("Service Unavailable: overloaded", status_code=503), FailoverReason.overloaded, True),
            (MockAPIError("Bad Gateway", status_code=502), FailoverReason.server_error, True),
            (MockAPIError("Gateway Timeout", status_code=504), FailoverReason.server_error, True),
            (MockAPIError("certificate verify failed", status_code=None), FailoverReason.ssl_cert_verification, True),
            (MockAPIError("ConnectTimeout to host", status_code=None), FailoverReason.timeout, True),
        ]
        for exc, expected_reason, expected_fallback in cases:
            res = classify_api_error(exc)
            self.assertEqual(res.reason, expected_reason, f"Failed on {exc}")
            self.assertEqual(res.should_fallback, expected_fallback)

if __name__ == "__main__":
    unittest.main()
