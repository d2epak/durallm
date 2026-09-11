"""Autonomous Nightly Canary Probing & Quirks Ledger Maintenance.

Provides self-scheduled, zero-cost canary probing:
- Probes live OpenRouter model catalog to detect new free models and retirements.
- Pings configured provider routes (Groq, OpenRouter, NVIDIA NIM) to observe availability and rate limits.
- Persists timestamped observations into docs/providers/provider_quirks_ledger.json.
- Schedules automated execution every night at 1:00 AM UK time (Europe/London) with zero external dependencies.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

logger = logging.getLogger("durallm.canary")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LEDGER_PATH = _REPO_ROOT / "docs" / "providers" / "provider_quirks_ledger.json"
_OPENROUTER_CATALOG_URL = "https://openrouter.ai/api/v1/models"


def get_uk_timezone():
    """Return Europe/London timezone or UTC+1 fallback."""
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Europe/London")
        except Exception:
            pass
    # Fallback to UTC+1 (BST default)
    return timezone(timedelta(hours=1))


def compute_seconds_until_next_1am_uk(now_dt: Optional[datetime] = None) -> float:
    """Calculate the number of seconds until the next 1:00 AM Europe/London time."""
    tz = get_uk_timezone()
    now = now_dt or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    target_today = datetime.combine(now.date(), dtime(hour=1, minute=0, second=0), tzinfo=tz)
    if now >= target_today:
        # 1:00 AM today already passed; schedule for tomorrow
        target = target_today + timedelta(days=1)
    else:
        target = target_today

    diff = (target - now).total_seconds()
    return max(0.0, diff)


class CanaryProber:
    """Live probe engine testing free provider endpoints and maintaining the quirks ledger."""

    def __init__(self, ledger_path: Optional[Path] = None):
        self.ledger_path = ledger_path or DEFAULT_LEDGER_PATH

    def load_ledger(self) -> Dict[str, Any]:
        if not self.ledger_path.exists():
            return {
                "version": "1.0.0",
                "last_updated_utc": None,
                "scheduler": {"target_time_local": "01:00", "timezone": "Europe/London"},
                "providers": {},
            }
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Could not read quirks ledger at %s: %s", self.ledger_path, e)
            return {"version": "1.0.0", "last_updated_utc": None, "providers": {}}

    def save_ledger(self, data: Dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def fetch_openrouter_free_models(self, timeout: float = 10.0) -> List[Dict[str, Any]]:
        """Fetch active models from OpenRouter and extract free tool-supporting models."""
        req = urllib.request.Request(
            _OPENROUTER_CATALOG_URL,
            headers={"User-Agent": "durallm-canary/0.2.1", "Accept": "application/json"},
        )
        free_models = []
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                catalog = json.loads(resp.read().decode("utf-8"))
                for item in catalog.get("data", []):
                    mid = item.get("id", "")
                    pricing = item.get("pricing", {})
                    # Free models have prompt & completion price == 0 or slug ending in :free
                    is_free = (
                        mid.endswith(":free")
                        or (
                            float(pricing.get("prompt", 1) or 1) == 0
                            and float(pricing.get("completion", 1) or 1) == 0
                        )
                    )
                    if is_free:
                        supported_params = item.get("supported_parameters", [])
                        free_models.append({
                            "id": mid,
                            "name": item.get("name", mid),
                            "context_length": int(item.get("context_length", 0) or 0),
                            "supports_tools": "tools" in supported_params if supported_params else True,
                        })
        except Exception as e:
            logger.warning("OpenRouter catalog fetch encountered an issue: %s", e)
        return free_models

    def ping_endpoint(
        self,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        timeout: float = 12.0,
    ) -> Dict[str, Any]:
        """Dispatch a minimal probe to test provider responsiveness and rate limit headers."""
        if not api_key:
            return {"status": "skipped", "reason": "no_api_key_configured"}

        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }
        url = f"{base_url.rstrip('/')}/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "durallm-canary/0.2.1",
            },
        )
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                latency_ms = (time.monotonic() - start) * 1000.0
                rate_limit_tokens = resp.headers.get("x-ratelimit-limit-tokens")
                rate_limit_reqs = resp.headers.get("x-ratelimit-limit-requests")
                return {
                    "status": "active",
                    "status_code": resp.status,
                    "latency_ms": round(latency_ms, 1),
                    "rate_limit_tokens": rate_limit_tokens,
                    "rate_limit_requests": rate_limit_reqs,
                }
        except urllib.error.HTTPError as e:
            latency_ms = (time.monotonic() - start) * 1000.0
            raw_body = e.read().decode("utf-8", errors="ignore")
            if e.code in (404, 410) or "unavailable for free" in raw_body:
                return {"status": "deprecated", "status_code": e.code, "message": raw_body[:160]}
            if e.code == 429:
                if "free-models-per-day" in raw_body:
                    return {"status": "quota_exhausted", "status_code": 429, "message": "free-models-per-day"}
                return {"status": "rate_limited", "status_code": 429, "latency_ms": round(latency_ms, 1)}
            return {"status": "error", "status_code": e.code, "message": raw_body[:160]}
        except Exception as exc:
            return {"status": "unreachable", "error": str(exc)[:160]}

    def run_probe(self, update_pools: bool = True) -> Dict[str, Any]:
        """Execute a full canary run: query catalogs, test routes, and update the ledger."""
        logger.info("[🔍 CANARY] Starting live provider and model discovery probe...")
        now_iso = datetime.now(timezone.utc).isoformat()
        ledger = self.load_ledger()
        ledger["last_updated_utc"] = now_iso

        from durallm.pools import POOL_MANAGER, RouteDefinition, load_all_env_keys
        keys = load_all_env_keys()

        # 1. Discover OpenRouter free models
        discovered_or_models = self.fetch_openrouter_free_models()
        active_discovered_ids = {m["id"] for m in discovered_or_models}

        openrouter_prov = ledger.setdefault("providers", {}).setdefault("openrouter", {
            "name": "OpenRouter",
            "base_url": "https://openrouter.ai/api/v1",
            "auth_env": "OPENROUTER_API_KEY",
            "models": {},
        })
        or_models = openrouter_prov.setdefault("models", {})

        # Check existing OpenRouter models for deprecation against live catalog
        if active_discovered_ids:
            for mid, mdata in list(or_models.items()):
                if mid.endswith(":free") and mid not in active_discovered_ids and mdata.get("status") == "active":
                    mdata["status"] = "deprecated"
                    mdata["last_verified_utc"] = now_iso
                    mdata.setdefault("quirks", []).append("Removed from live OpenRouter free catalog")
                    logger.warning("[⚠️ CANARY] OpenRouter retired free model slug: %s", mid)
                    if update_pools:
                        POOL_MANAGER.mark_deprecated("coding", mid)
                        POOL_MANAGER.mark_deprecated("general_agent", mid)

        # Append newly discovered models to ledger and pools
        for dm in discovered_or_models:
            mid = dm["id"]
            if mid not in or_models:
                is_coding = any(k in mid.lower() for k in ["code", "coder", "devstral", "codestral"])
                pool_name = "coding" if is_coding else "general_agent"
                or_models[mid] = {
                    "pool": pool_name,
                    "context_window": dm["context_length"] or 65536,
                    "safe_input_tokens": min(32000, dm["context_length"] or 32000),
                    "max_output_tokens": 8192,
                    "supports_tools": dm["supports_tools"],
                    "supports_streaming": True,
                    "status": "active",
                    "last_verified_utc": now_iso,
                    "quirks": ["Discovered via live OpenRouter aggregator catalog"],
                }
                logger.info("[✨ CANARY] Discovered new free model on OpenRouter: %s (%s)", mid, pool_name)
                if update_pools:
                    route = RouteDefinition(
                        id=f"openrouter-canary-{mid.replace('/', '-')}",
                        provider="openrouter",
                        model=mid,
                        pool=pool_name,
                        base_url="https://openrouter.ai/api/v1",
                        api_format="openai",
                        env_key="OPENROUTER_API_KEY",
                        context_length=dm["context_length"] or 65536,
                        max_output_tokens=8192,
                        is_discovered=True,
                    )
                    POOL_MANAGER.add_discovered_route(pool_name, route)

        # 2. Ping sample routes across providers to verify live health
        probed_routes = [
            ("groq", "qwen/qwen3.6-27b", "https://api.groq.com/openai/v1", keys.get("GROQ_API_KEY", "")),
            ("openrouter", "openrouter/free", "https://openrouter.ai/api/v1", keys.get("OPENROUTER_API_KEY", "")),
            ("nvidia", "meta/llama-3.2-11b-vision-instruct", "https://integrate.api.nvidia.com/v1", keys.get("NVIDIA_API_KEY", "")),
        ]
        probe_results = {}
        for prov, model, base_url, key_val in probed_routes:
            res = self.ping_endpoint(prov, model, base_url, key_val)
            probe_results[f"{prov}:{model}"] = res
            prov_data = ledger.get("providers", {}).get(prov, {})
            model_data = prov_data.get("models", {}).get(model, {})
            if model_data:
                model_data["last_verified_utc"] = now_iso
                if res.get("status") in ("active", "rate_limited"):
                    model_data["status"] = "active"
                    if "latency_ms" in res:
                        model_data["last_probe_latency_ms"] = res["latency_ms"]
                elif res.get("status") == "deprecated":
                    model_data["status"] = "deprecated"
                    if update_pools:
                        POOL_MANAGER.mark_deprecated("coding", model)

        self.save_ledger(ledger)
        logger.info("[✅ CANARY] Probe completed. Updated ledger written to %s", self.ledger_path)
        return {
            "status": "success",
            "timestamp": now_iso,
            "discovered_free_models": len(discovered_or_models),
            "probed_endpoints": probe_results,
        }


class NightlyCanaryScheduler:
    """Thread-safe background daemon executing CanaryProber every night at 1:00 AM UK time."""

    def __init__(
        self,
        prober: Optional[CanaryProber] = None,
        target_hour: int = 1,
        target_minute: int = 0,
    ):
        self.prober = prober or CanaryProber()
        self.target_hour = target_hour
        self.target_minute = target_minute
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self.last_run_utc: Optional[str] = None
        self.last_run_result: Optional[Dict[str, Any]] = None

    def start(self, run_immediately: bool = False) -> None:
        """Start the background scheduler thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._scheduler_loop,
                args=(run_immediately,),
                name="durallm-nightly-canary",
                daemon=True,
            )
            self._thread.start()
            logger.info("🕒 Nightly Canary Scheduler started (targets %02d:%02d Europe/London)", self.target_hour, self.target_minute)

    def stop(self) -> None:
        """Stop the background scheduler thread."""
        with self._lock:
            self._stop_event.set()
            if self._thread is not None and self._thread.is_alive():
                self._thread.join(timeout=2.0)
            self._thread = None

    def _scheduler_loop(self, run_immediately: bool) -> None:
        if run_immediately:
            try:
                self.trigger_now()
            except Exception as e:
                logger.warning("Immediate canary startup run failed: %s", e)

        while not self._stop_event.is_set():
            sleep_sec = compute_seconds_until_next_1am_uk()
            logger.info("🕒 Next Nightly Canary Probe in %.1f hours (%.0fs)", sleep_sec / 3600.0, sleep_sec)

            # Wait with frequent checks for stop event
            chunk_seconds = 10.0
            elapsed = 0.0
            while elapsed < sleep_sec and not self._stop_event.is_set():
                time.sleep(min(chunk_seconds, sleep_sec - elapsed))
                elapsed += chunk_seconds

            if self._stop_event.is_set():
                break

            try:
                self.trigger_now()
            except Exception as e:
                logger.error("Nightly Canary Probe run failed: %s", e)

    def trigger_now(self) -> Dict[str, Any]:
        """Trigger an immediate canary run safely."""
        with self._lock:
            res = self.prober.run_probe()
            self.last_run_utc = res.get("timestamp")
            self.last_run_result = res
            return res

    def status(self) -> Dict[str, Any]:
        """Return diagnostic status of the canary scheduler and upcoming run."""
        with self._lock:
            sec_until = compute_seconds_until_next_1am_uk()
            tz = get_uk_timezone()
            next_run_dt = datetime.now(tz) + timedelta(seconds=sec_until)
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "target_schedule": f"{self.target_hour:02d}:{self.target_minute:02d} Europe/London",
                "seconds_until_next_run": round(sec_until, 1),
                "next_scheduled_run_uk": next_run_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "last_run_utc": self.last_run_utc,
                "last_run_summary": self.last_run_result,
            }


DEFAULT_CANARY_SCHEDULER = NightlyCanaryScheduler()
