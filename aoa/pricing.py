"""Anthropic list prices in USD per million tokens, for estimating spend from the `usage` table.

Source: https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-15). Update here when prices
change. The table does not record which cache TTL a write used, so writes are priced at the configured
`AOA_CACHE_TTL`. Global routing (the default) is assumed; US-only inference would add 1.1x.
"""
from __future__ import annotations

# (input, 5m cache write, 1h cache write, cache read, output)
Rates = tuple[float, float, float, float, float]

SONNET_5_5: Rates = (2.0, 2.5, 4.0, 0.10, 10.0)
HAIKU_5_5: Rates = (0.10, 0.125, 0.20, 0.01, 0.50)
HAIKU_5_5_LONG: Rates = (0.50, 0.625, 1.0, 0.05, 2.50)  # a request whose whole prompt is over 100k tokens
HAIKU_LONG_PROMPT = 100_000

PRICES: dict[str, Rates] = {
    "claude-sonnet-5-5": SONNET_5_5,
    "claude-haiku-5-5": HAIKU_5_5,
}


def rates_for(model: str, prompt_tokens: int) -> Rates | None:
    """Rates for one request, or None for a model we have no price for."""
    if model == "claude-haiku-5-5" and prompt_tokens > HAIKU_LONG_PROMPT:
        return HAIKU_5_5_LONG
    return PRICES.get(model)


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int,
             cache_ttl_seconds: int = 300) -> float | None:
    """Estimated cost of one request; None when the model has no known price."""
    r = rates_for(model, input_tokens + cache_read + cache_write)
    if r is None:
        return None
    write = r[2] if cache_ttl_seconds > 300 else r[1]
    return (input_tokens * r[0] + cache_write * write + cache_read * r[3] + output_tokens * r[4]) / 1_000_000
