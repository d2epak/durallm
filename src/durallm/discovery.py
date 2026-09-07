"""Automated Model Discovery & Catalog Maintenance Engine.

Discovers $0 free models from live aggregator catalogs, validates tool-calling
support, context length, tracks deprecations, and persists catalog state.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("durallm.discovery")

_OPENROUTER_CATALOG_URL = "https://openrouter.ai/api/v1/models"
_DEFAULT_CATALOG_PATH = Path(os.path.expanduser("~/.hermes/model_catalog.json"))
_MIN_CONTEXT_LENGTH = 16384


def is_model_free(pricing: Any) -> bool:
    """Return True if prompt, completion, request, and image pricing are $0."""
    if not isinstance(pricing, dict):
        return False
    try:
        p = float(pricing.get("prompt", "0") or 0)
        c = float(pricing.get("completion", "0") or 0)
        r = float(pricing.get("request", "0") or 0)
        i = float(pricing.get("image", "0") or 0)
        return p == 0 and c == 0 and r == 0 and i == 0
    except (TypeError, ValueError):
        return False


def supports_tool_calling(item: dict[str, Any]) -> bool:
    """Return True if the model advertises tool calling capabilities."""
    if not isinstance(item, dict):
        return True
    params = item.get("supported_parameters")
    if not isinstance(params, list):
        return True
    return "tools" in params


def fetch_openrouter_catalog(timeout: float = 10.0) -> list[dict[str, Any]]:
    """Fetch live catalog from OpenRouter."""
    req = urllib.request.Request(
        _OPENROUTER_CATALOG_URL,
        headers={"User-Agent": "llm-circuit-breaker/0.2.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("data", [])
    except Exception as e:
        logger.warning("Failed to fetch model catalog: %s", e)
        return []


def is_coding_model(model_id: str, name: str = "") -> bool:
    """Determine if a discovered model is tuned specifically for coding."""
    combined = f"{model_id} {name}".lower()
    coding_keywords = ["coder", "code", "devstral", "codestral", "deepseek-coder", "starcoder", "wizardcoder"]
    return any(k in combined for k in coding_keywords)


def load_model_catalog(catalog_path: Optional[Path] = None) -> dict[str, Any]:
    """Load persisted model catalog from disk."""
    path = catalog_path or _DEFAULT_CATALOG_PATH
    if not path.exists():
        return {"free_models": [], "deprecated_models": [], "last_updated": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Could not read catalog at %s: %s", path, e)
        return {"free_models": [], "deprecated_models": [], "last_updated": None}


def save_model_catalog(catalog: dict[str, Any], catalog_path: Optional[Path] = None) -> None:
    """Persist catalog to disk."""
    path = catalog_path or _DEFAULT_CATALOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2)


def discover_models(
    force: bool = False,
    min_context: int = _MIN_CONTEXT_LENGTH,
    catalog_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Discover free models, track deprecations, and save catalog snapshot."""
    path = catalog_path or _DEFAULT_CATALOG_PATH
    existing_catalog = load_model_catalog(path)

    # 1. Fetch live models
    raw_models = fetch_openrouter_catalog()
    if not raw_models:
        return existing_catalog

    # 2. Filter for free, tool-supporting, sufficient context models
    current_free: List[Dict[str, Any]] = []
    for item in raw_models:
        mid = item.get("id", "")
        pricing = item.get("pricing", {})
        context_len = int(item.get("context_length", 0) or 0)

        if not is_model_free(pricing):
            continue
        if not supports_tool_calling(item):
            continue
        if context_len < min_context:
            continue

        current_free.append({
            "id": mid,
            "name": item.get("name", mid),
            "context_length": context_len,
            "pricing": pricing,
            "supported_parameters": item.get("supported_parameters", []),
        })

    # Sort by context length
    current_free.sort(key=lambda x: x["context_length"], reverse=True)

    # 3. Detect deprecated models
    old_free_ids = {m["id"] for m in existing_catalog.get("free_models", [])}
    new_free_ids = {m["id"] for m in current_free}
    missing_ids = old_free_ids - new_free_ids

    all_deprecated = set(existing_catalog.get("deprecated_models", [])) | missing_ids

    new_catalog = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_free_models_discovered": len(current_free),
        "free_models": current_free,
        "deprecated_models": sorted(list(all_deprecated)),
    }

    save_model_catalog(new_catalog, path)
    return new_catalog


def get_top_free_models(limit: int = 5, min_context: int = _MIN_CONTEXT_LENGTH) -> List[Dict[str, Any]]:
    """Return top free tool-calling models sorted by context length."""
    catalog = discover_models(min_context=min_context)
    models = catalog.get("free_models", [])
    return models[:limit]


def discover_free_models(
    min_context: int = _MIN_CONTEXT_LENGTH,
    timeout: float = 10.0
) -> Dict[str, List[Dict[str, Any]]]:
    """Categorize live models into 'coding' and 'general_agent' pools."""
    raw_models = fetch_openrouter_catalog(timeout=timeout)
    coding_models: List[Dict[str, Any]] = []
    agent_models: List[Dict[str, Any]] = []

    for item in raw_models:
        mid = item.get("id", "")
        pricing = item.get("pricing", {})
        context_len = int(item.get("context_length", 0) or 0)

        if not is_model_free(pricing):
            continue
        if not supports_tool_calling(item):
            continue
        if context_len < min_context:
            continue

        model_info = {
            "id": mid,
            "name": item.get("name", mid),
            "context_length": context_len,
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "key_env": "OPENROUTER_API_KEY",
        }

        if is_coding_model(mid, item.get("name", "")):
            coding_models.append(model_info)
        else:
            agent_models.append(model_info)

    coding_models.sort(key=lambda x: x["context_length"], reverse=True)
    agent_models.sort(key=lambda x: x["context_length"], reverse=True)

    return {"coding": coding_models, "general_agent": agent_models}


def register_discovered_models_to_pools(limit_per_pool: int = 4) -> None:
    """Discover live models and register them into IsolatedPoolManager."""
    from durallm.pools import POOL_MANAGER, RouteDefinition

    try:
        discovered = discover_free_models()
        for m in discovered.get("coding", [])[:limit_per_pool]:
            route = RouteDefinition(
                id=f"openrouter-discovered-{m['id'].replace('/', '-')}",
                provider="openrouter",
                model=m["id"],
                pool="coding",
                base_url=m["base_url"],
                api_format="openai",
                env_key=m["key_env"],
                context_length=m["context_length"],
                is_discovered=True,
            )
            POOL_MANAGER.add_discovered_route("coding", route)

        for m in discovered.get("general_agent", [])[:limit_per_pool]:
            route = RouteDefinition(
                id=f"openrouter-discovered-{m['id'].replace('/', '-')}",
                provider="openrouter",
                model=m["id"],
                pool="general_agent",
                base_url=m["base_url"],
                api_format="openai",
                env_key=m["key_env"],
                context_length=m["context_length"],
                is_discovered=True,
            )
            POOL_MANAGER.add_discovered_route("general_agent", route)
    except Exception as e:
        logger.warning("Background catalog discovery failed: %s", e)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Discover active $0 free LLMs with tool support")
    parser.add_argument("--limit", type=int, default=10, help="Max models to show per pool")
    parser.add_argument("--min-context", type=int, default=16384, help="Min context length (default: 16k)")
    args = parser.parse_args()

    print("🔍 Querying live aggregator catalog for $0 free models with native tool support...")
    res = discover_free_models(min_context=args.min_context)

    print(f"\n💻 Coding Pool ({len(res['coding'])} discovered):")
    for idx, m in enumerate(res["coding"][:args.limit], 1):
        print(f"  {idx}. {m['id']} ({m['context_length']:,} tokens context)")

    print(f"\n🤖 General Agent Pool ({len(res['general_agent'])} discovered):")
    for idx, m in enumerate(res["general_agent"][:args.limit], 1):
        print(f"  {idx}. {m['id']} ({m['context_length']:,} tokens context)")
    print()


if __name__ == "__main__":
    main()
