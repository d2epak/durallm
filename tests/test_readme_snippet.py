"""The README "Basic Python Usage" block must run exactly as printed.

It is executed against a mock adapter so no network call happens, but the
code itself is taken verbatim from README.md at test time.
"""

import contextlib
import io
import os
import re
import unittest
from pathlib import Path

from durallm.capability.registry import DEFAULT_CAPABILITY_REGISTRY
from durallm.providers.adapters import DEFAULT_ADAPTER_REGISTRY
from tests.faults.mock_provider import MockFaultAction, ProgrammableMockAdapter

README = Path(__file__).resolve().parents[1] / "README.md"


def readme_python_block() -> str:
    text = README.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    assert blocks, "README has no python block"
    return blocks[0]


class TestReadmeSnippetRuns(unittest.TestCase):

    def setUp(self):
        self.mock = ProgrammableMockAdapter("groq")
        self.mock.set_sequence([MockFaultAction.success("deploying")])
        self._real_adapter = DEFAULT_ADAPTER_REGISTRY._adapters["groq"]
        DEFAULT_ADAPTER_REGISTRY._adapters["groq"] = self.mock
        self._old_key = os.environ.get("GROQ_API_KEY")
        os.environ["GROQ_API_KEY"] = "test-key"

    def tearDown(self):
        DEFAULT_ADAPTER_REGISTRY._adapters["groq"] = self._real_adapter
        DEFAULT_CAPABILITY_REGISTRY._endpoints.pop("groq-llama", None)
        if self._old_key is None:
            os.environ.pop("GROQ_API_KEY", None)
        else:
            os.environ["GROQ_API_KEY"] = self._old_key

    def test_snippet_executes_and_reaches_the_registered_endpoint(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile(readme_python_block(), str(README), "exec"), {"__name__": "readme"})

        self.assertIn("Selected Endpoint: groq-llama", out.getvalue())
        self.assertIn("Response: deploying", out.getvalue())
        self.assertEqual(len(self.mock.call_history), 1)
        prepared = self.mock.call_history[0]
        self.assertEqual(prepared.headers.get("Authorization"), "Bearer test-key")


if __name__ == "__main__":
    unittest.main()
