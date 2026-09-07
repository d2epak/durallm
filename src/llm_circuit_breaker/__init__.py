"""Backwards-compatibility shim for llm_circuit_breaker -> durallm."""
import importlib
import importlib.util
import sys

import durallm


class _ShimLoader:
    def __init__(self, target_name: str) -> None:
        self.target_name = target_name

    def create_module(self, spec):
        return importlib.import_module(self.target_name)

    def exec_module(self, module) -> None:
        pass

class _ShimFinder:
    def find_spec(self, fullname: str, path, target=None):
        if fullname == "llm_circuit_breaker" or fullname.startswith("llm_circuit_breaker."):
            target_name = fullname.replace("llm_circuit_breaker", "durallm", 1)
            target_spec = importlib.util.find_spec(target_name)
            if target_spec is not None:
                spec = importlib.util.spec_from_loader(
                    fullname,
                    _ShimLoader(target_name),
                    is_package=bool(target_spec.submodule_search_locations),
                )
                spec.submodule_search_locations = target_spec.submodule_search_locations
                return spec
        return None

if not any(isinstance(f, _ShimFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _ShimFinder())

for attr in dir(durallm):
    if not attr.startswith("__"):
        globals()[attr] = getattr(durallm, attr)
