"""OpenClaw Integration Example using durallm."""

import os

from durallm import FailoverReason, UniversalFailoverRouter, classify_api_error

# Initialize router with priority providers + auto-discovered free backups
router = UniversalFailoverRouter(configured_fallbacks=[
    {"provider": "nvidia", "model": "nvidia/nemotron-3-ultra-550b-a55b", "base_url": "https://integrate.api.nvidia.com/v1", "key_env": "NVIDIA_API_KEY"},
    {"provider": "cerebras", "model": "llama3.3-70b", "base_url": "https://api.cerebras.ai/v1", "key_env": "CEREBRAS_API_KEY"},
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
])

def run_openclaw_step(messages: list) -> str:
    """Execute OpenClaw agent turn with auto-failover."""
    import openai

    while True:
        route = router.active_provider
        api_key = os.getenv(route.get("key_env", ""), "")
        client = openai.OpenAI(api_key=api_key, base_url=route.get("base_url"))

        try:
            print(f"🤖 Dispatching turn to {route['provider']} ({route['model']})...")
            response = client.chat.completions.create(
                model=route["model"],
                messages=messages,
            )
            return response.choices[0].message.content

        except Exception as err:
            classified = classify_api_error(err)
            print(f"⚠️ Caught error {classified.reason} ({err}). Failing over...")

            if not classified.should_fallback:
                raise err

            if classified.reason in (FailoverReason.rate_limit, FailoverReason.upstream_rate_limit):
                router.mark_cooldown(route["provider"], seconds=60.0)
            elif classified.reason == FailoverReason.model_not_found:
                router.mark_deprecated(route["model"])

            next_route = router.get_next_available_route(reason=classified.reason)
            if not next_route:
                raise RuntimeError("All LLM providers exhausted.")

if __name__ == "__main__":
    print("OpenClaw Failover Runner initialized.")
