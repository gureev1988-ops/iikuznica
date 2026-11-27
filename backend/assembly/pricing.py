"""Observed OpenRouter endpoint prices for calls whose usage omits the charge."""
from decimal import Decimal, InvalidOperation
from time import monotonic

import httpx

# Refresh outside the turn worker. Each charge is persisted in the model journal
# at execution time, so later tariff changes do not alter historical totals.
_rates = {}
_next_check = 0.0
_VALID_SECONDS = 24 * 3600


def refresh():
    global _rates, _next_check
    now = monotonic()
    if now < _next_check:
        return False
    _next_check = now + 900
    from .models import PROVIDERS

    updates = {}
    try:
        with httpx.Client(timeout=8) as client:
            for model in PROVIDERS:
                response = client.get(f"https://openrouter.ai/api/v1/models/{model}/endpoints")
                response.raise_for_status()
                for endpoint in response.json()["data"]["endpoints"]:
                    fields = endpoint["pricing"]
                    if any(Decimal(str(v or 0)) for k, v in fields.items() if k not in
                           {"prompt", "completion", "input_cache_read", "input_cache_write", "request"}):
                        continue
                    prices = {k: Decimal(str(v)) for k, v in fields.items() if v is not None}
                    if not {"prompt", "completion"} <= prices.keys() or any(
                        not p.is_finite() or p < 0 for p in prices.values()
                    ):
                        continue
                    tag = endpoint["tag"].casefold()
                    updates[model, tag] = now + _VALID_SECONDS, endpoint["provider_name"].casefold(), prices
        _rates = updates
        _next_check = now + 3600
    except (httpx.HTTPError, KeyError, TypeError, ValueError, InvalidOperation):
        pass
    return False


def charge(model, tag, actual_provider, usage):
    item = _rates.get((model, tag.casefold()))
    if not item or item[0] < monotonic() or not actual_provider or item[1] != actual_provider.casefold():
        return None
    prices = item[2]
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if type(prompt) is not int or type(completion) is not int or min(prompt, completion) < 0:
        return None
    details = usage.get("prompt_tokens_details") or {}
    cached, written = details.get("cached_tokens", 0), details.get("cache_write_tokens", 0)
    if type(cached) is not int or type(written) is not int or min(cached, written, prompt - cached - written) < 0:
        return None
    if cached and "input_cache_read" not in prices or written and "input_cache_write" not in prices:
        return None
    return (prices["prompt"] * (prompt - cached - written) + prices["completion"] * completion
            + prices.get("input_cache_read", 0) * cached + prices.get("input_cache_write", 0) * written
            + prices.get("request", 0))
