"""One small provider-agnostic LLM call.

Deliberately not an abstraction layer: a single function and an if/elif. The
project needs exactly one operation (send a prompt, get text back), and which
provider supplies it depends on whichever free tier or key was available on the
day. All three speak plain REST, so `httpx` is the only dependency.

Set LLM_PROVIDER to force one; otherwise the first provider with a key wins.
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

TIMEOUT = 60

PROVIDERS = {
    # provider: (env var holding the key, default model)
    "anthropic": ("ANTHROPIC_API_KEY", "claude-haiku-4-5-20251001"),
    "groq": ("GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "gemini": ("GOOGLE_API_KEY", "gemini-2.0-flash"),
}


def active_provider():
    """The provider to use, or None if no key is configured anywhere."""
    forced = os.getenv("LLM_PROVIDER")
    if forced:
        return forced if os.getenv(PROVIDERS[forced][0]) else None
    for name, (env_var, _) in PROVIDERS.items():
        if os.getenv(env_var):
            return name
    return None


def available() -> bool:
    return active_provider() is not None


def model_name(provider=None):
    provider = provider or active_provider()
    return os.getenv("LLM_MODEL") or PROVIDERS[provider][1]


def complete(system: str, user: str, max_tokens: int = 400) -> str:
    """Send one prompt, return the text. Raises if no provider is configured."""
    provider = active_provider()
    if provider is None:
        raise RuntimeError(
            "No LLM key found. Set ANTHROPIC_API_KEY, GROQ_API_KEY or "
            "GOOGLE_API_KEY in .env — or run against the committed cache."
        )
    key = os.environ[PROVIDERS[provider][0]]
    model = model_name(provider)

    if provider == "anthropic":
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.json()["content"][0]["text"]

    if provider == "groq":
        response = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    if provider == "gemini":
        response = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": max_tokens},
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.json()["candidates"][0]["content"]["parts"][0]["text"]

    raise RuntimeError(f"Unknown LLM_PROVIDER: {provider!r}")
