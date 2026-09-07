"""Automated lint test ensuring zero ruff errors before committing."""

from __future__ import annotations

import subprocess
import sys
import unittest


class TestLint(unittest.TestCase):
    """Enforce ruff check passes during pytest run."""

    def test_ruff_check_clean(self):
        cmd = [sys.executable, "-m", "ruff", "check", "."]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(
            res.returncode,
            0,
            f"Ruff lint check failed. Run `ruff check --fix .` to fix automatically.\n{res.stdout}\n{res.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
