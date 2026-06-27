import time

import requests

_cache = {"rate": None, "fetched_at": 0}
_CACHE_TTL_SECONDS = 6 * 3600


def get_usd_krw_rate() -> float:
    """Returns current USD->KRW rate, cached for a few hours. Falls back to last known rate on failure."""
    now = time.time()
    if _cache["rate"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL_SECONDS:
        return _cache["rate"]

    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        resp.raise_for_status()
        rate = float(resp.json()["rates"]["KRW"])
        _cache["rate"] = rate
        _cache["fetched_at"] = now
        return rate
    except Exception:
        if _cache["rate"] is not None:
            return _cache["rate"]
        raise
