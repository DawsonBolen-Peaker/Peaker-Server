"""Token usage and what it cost, per request, per file and per run.

A usage dict looks like
    {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
     "requests", "model", "api"}
where api is "batch" (Message Batches, half price) or "regular". The token
counts are what the API reports on each response (message.usage), so they
are what Anthropic bills, not an estimate.

docs.db keeps these totals on each files row (the run that processed it) and
on each batches row (everything that run sent, failed files included). See
finance/batch_report.py to print them.
"""

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

# Prompt caching: reading the cached system prompt costs a tenth of the input
# price, writing it (the first request, or after 5 idle minutes) 1.25 times.
CACHE_READ_RATE = 0.10
CACHE_WRITE_RATE = 1.25
BATCH_DISCOUNT = 0.50


def empty_usage(api: str = "regular") -> dict:
    return {**{k: 0 for k in TOKEN_KEYS}, "requests": 0, "model": "", "api": api}


def usage_of(message, api: str = "regular") -> dict:
    """The usage of one Messages API response."""
    u = getattr(message, "usage", None)
    return {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "requests": 1,
        "model": getattr(message, "model", "") or "",
        "api": api,
    }


def add_usage(total: dict, more) -> dict:
    """Add more (a usage dict or None) into total, in place. Returns total."""
    if more:
        for k in TOKEN_KEYS + ("requests",):
            total[k] = total.get(k, 0) + more.get(k, 0)
        total["model"] = total.get("model") or more.get("model", "")
        total["api"] = total.get("api") or more.get("api", "regular")
    return total


def take_usage(records, api: str = "regular") -> dict:
    """Pop the "_usage" that scan_chunk puts on each chunk record and add them up."""
    total = empty_usage(api)
    for r in records:
        if isinstance(r, dict):
            add_usage(total, r.pop("_usage", None))
    return total


def cost_usd(usage) -> float:
    """What a usage dict cost, from the list prices in api/claudeAgent.PRICES."""
    if not usage:
        return 0.0
    from server.services.Claude.claudeAgent import PRICES, MODEL
    model = usage.get("model") or MODEL
    # the API can report a longer name (e.g. with a date); match on the start
    key = next((k for k in sorted(PRICES, key=len, reverse=True) if model.startswith(k)), MODEL)
    price_in, price_out = PRICES.get(key, PRICES.get(MODEL, (0, 0)))
    usd = (usage.get("input_tokens", 0) * price_in
           + usage.get("cache_read_tokens", 0) * price_in * CACHE_READ_RATE
           + usage.get("cache_write_tokens", 0) * price_in * CACHE_WRITE_RATE
           + usage.get("output_tokens", 0) * price_out) / 1_000_000
    if usage.get("api") == "batch":
        usd *= BATCH_DISCOUNT
    return round(usd, 4)
