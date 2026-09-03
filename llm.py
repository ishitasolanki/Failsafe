"""One small provider-agnostic LLM call.

Deliberately not an abstraction layer: a single function and an if/elif. The
project needs exactly one operation (send a prompt, get text back), and which
provider supplies it depends on whichever free tier or key was available on the
day. All three speak plain REST, so `httpx` is the only dependency.

Set LLM_PROVIDER to force one; otherwise the first provider with a key wins.
"""

import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

TIMEOUT = 60

# Free tiers rate-limit hard. Without backoff, 25 of 60 diagnoses silently fell
# through to the keyword fallback and the reported "model accuracy" was really
# the baseline wearing the model's name. Retried only where the request was not
# processed (429) or the provider faulted (5xx); an LLM call has no side effect,
# so retrying is always safe here.
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
BACKOFF_SECONDS = 1.5

PROVIDERS = {
    # provider: (env var holding the key, default model)
    "anthropic": ("ANTHROPIC_API_KEY", "claude-haiku-4-5-20251001"),
    "groq": ("GROQ_API_KEY", "openai/gpt-oss-120b"),
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


def _send(request):
    """Issue the request with bounded backoff, honouring Retry-After."""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = request()
        except httpx.TransportError as error:
            last_error = error
        else:
            if response.status_code not in RETRY_STATUSES:
                response.raise_for_status()
                return response.json()
            last_error = httpx.HTTPStatusError(
                f"{response.status_code} from provider",
                request=response.request,
                response=response,
            )
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    time.sleep(min(float(retry_after), 30))
                    continue
                except ValueError:
                    pass
        if attempt < MAX_RETRIES - 1:
            time.sleep(BACKOFF_SECONDS * (2 ** attempt))
    raise last_error


def complete(system: str, user: str, max_tokens: int = 1200) -> str:
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
        payload = dict(
            url="https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0,   # classification, not creativity
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=TIMEOUT,
        )
        return _send(lambda: httpx.post(**payload))["content"][0]["text"]

    if provider == "groq":
        payload = dict(
            url="https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0,   # classification, not creativity
                # Groq's default models are reasoning models: their thinking is
                # billed against max_tokens and, left unbounded, it consumes the
                # entire budget and returns empty content. Classification needs
                # a label, not a deliberation.
                "reasoning_effort": "low",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=TIMEOUT,
        )
        message = _send(lambda: httpx.post(**payload))["choices"][0]["message"]
        # A reasoning model that ran out of budget leaves content empty and puts
        # everything in `reasoning`. Fall back to it rather than silently
        # returning nothing, which would look like a parse failure.
        return message.get("content") or message.get("reasoning") or ""

    if provider == "gemini":
        payload = dict(
            url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": max_tokens,
                                     "temperature": 0},
            },
            timeout=TIMEOUT,
        )
        result = _send(lambda: httpx.post(**payload))
        return result["candidates"][0]["content"]["parts"][0]["text"]

    raise RuntimeError(f"Unknown LLM_PROVIDER: {provider!r}")
