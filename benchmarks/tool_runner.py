"""A stateful stand-in for the agent's tool executor, shared by every system under test.

It executes each delivered tool call unless the gateway marked it as a replay of an
already-committed execution. Executing the same (logical operation, tool, arguments)
twice is counted as a duplicate side effect.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Tuple

from durallm.protocol.ir import NormalizedToolCall


class ToolRunner:

    def __init__(self) -> None:
        self.executions: int = 0
        self.replays: int = 0
        self.duplicate_executions: int = 0
        self.log: List[str] = []
        self._receipts: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    @staticmethod
    def _key(operation_id: str, tc: NormalizedToolCall) -> Tuple[str, str, str]:
        digest = hashlib.sha256(json.dumps(tc.arguments, sort_keys=True).encode("utf-8")).hexdigest()
        return operation_id, tc.name, digest

    def handle(self, operation_id: str, tc: NormalizedToolCall) -> Tuple[Dict[str, Any], bool]:
        """Return (receipt, executed_now). A replayed call returns its cached receipt untouched."""
        key = self._key(operation_id, tc)
        if tc.metadata.get("replayed"):
            self.replays += 1
            return tc.metadata.get("execution_receipt") or self._receipts.get(key, {}), False
        if key in self._receipts:
            self.duplicate_executions += 1
        self.executions += 1
        receipt = {"tool": tc.name, "arguments": dict(tc.arguments), "output": f"executed {tc.name} #{self.executions}"}
        self._receipts[key] = receipt
        self.log.append(f"{operation_id}:{tc.name}")
        return receipt, True
