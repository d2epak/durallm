"""Importing the package or the proxy must not touch the network or read dotfiles.

Each case runs in a fresh interpreter so the imports really happen, with the
socket layer replaced by a function that records any attempt.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PROBE = r'''
import json, socket, sys, urllib.request

def _blocked(*a, **k):
    sys.stderr.write("NETWORK_ATTEMPTED\n")
    raise OSError("network blocked by test")

socket.socket = _blocked
socket.create_connection = _blocked
urllib.request.urlopen = _blocked

import durallm
import durallm.proxy
from durallm.pools import POOL_MANAGER, load_all_env_keys

print(json.dumps({
    "keys_at_import": POOL_MANAGER.keys,
    "explicit_opt_in": load_all_env_keys(scan_dotfiles=True),
}))
'''

KEY_NAMES = ("GROQ_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY")


class TestImportSideEffects(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        (Path(self.home.name) / ".zshrc").write_text('export GROQ_API_KEY="leaked-from-dotfile"\n')

    def tearDown(self):
        self.home.cleanup()

    def _run(self, **extra_env):
        env = {k: v for k, v in os.environ.items()
               if k not in KEY_NAMES and not k.startswith("LLM_BREAKER_")}
        env.update({"HOME": self.home.name, "PYTHONPATH": str(REPO / "src")}, **extra_env)
        proc = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1]), proc.stderr

    def test_import_makes_no_network_call_and_reads_no_dotfiles(self):
        data, stderr = self._run()
        self.assertNotIn("NETWORK_ATTEMPTED", stderr)
        self.assertEqual(data["keys_at_import"], {})
        # The scanning code path still works when asked for explicitly.
        self.assertEqual(data["explicit_opt_in"], {"GROQ_API_KEY": "leaked-from-dotfile"})

    def test_env_opt_in_enables_dotfile_scanning(self):
        data, _ = self._run(LLM_BREAKER_SCAN_DOTFILES="1")
        self.assertEqual(data["keys_at_import"], {"GROQ_API_KEY": "leaked-from-dotfile"})

    def test_env_opt_in_enables_discovery_at_import(self):
        # Discovery swallows its own errors, so the blocked socket is the evidence it ran.
        _, stderr = self._run(LLM_BREAKER_AUTO_DISCOVER="1")
        self.assertIn("NETWORK_ATTEMPTED", stderr)


if __name__ == "__main__":
    unittest.main()
