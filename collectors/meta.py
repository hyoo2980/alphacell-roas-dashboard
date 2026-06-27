import requests

from config import META_ACCESS_TOKEN, META_AD_ACCOUNT_IDS

API_VERSION = "v21.0"
BASE_URL = f"https://graph.facebook.com/{API_VERSION}"

FIELDS = [
    "date_start",
    "adset_id",
    "adset_name",
    "campaign_id",
    "campaign_name",
    "spend",
    "impressions",
    "clicks",
    "inline_link_clicks",
    "actions",
    "action_values",
    "video_play_actions",
]


def _extract_action_value(actions, action_type, value_key="value"):
    if not actions:
        return 0
    for a in actions:
        if a.get("action_type") == action_type:
            return float(a.get(value_key, 0))
    return 0


_account_info_cache = {}


def get_account_info(account_id: str) -> dict:
    """Ad accounts can each have their own reporting currency (e.g. USD or KRW) --
    this must be checked per account before any USD->KRW conversion downstream."""
    if account_id not in _account_info_cache:
        resp = requests.get(
            f"{BASE_URL}/{account_id}",
            params={"fields": "name,currency", "access_token": META_ACCESS_TOKEN},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        _account_info_cache[account_id] = {"name": data["name"], "currency": data["currency"]}
    return _account_info_cache[account_id]


def _fetch_adset_daily_for_account(account_id: str, since: str, until: str):
    account_info = get_account_info(account_id)
    currency = account_info["currency"]
    account_name = account_info["name"]
    url = f"{BASE_URL}/{account_id}/insights"
    params = {
        "level": "adset",
        "time_increment": 1,
        "time_range": f'{{"since":"{since}","until":"{until}"}}',
        "fields": ",".join(FIELDS),
        "access_token": META_ACCESS_TOKEN,
        "limit": 500,
    }

    rows = []
    while url:
        resp = requests.get(url, params=params if "?" not in url else None, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Meta API error ({account_id}): {data['error']}")

        for item in data.get("data", []):
            actions = item.get("actions", [])
            purchases = _extract_action_value(actions, "omni_purchase")
            link_clicks = _extract_action_value(actions, "link_click")
            landing_page_views = _extract_action_value(actions, "landing_page_view")
            add_to_cart = _extract_action_value(actions, "omni_add_to_cart")
            initiate_checkout = _extract_action_value(actions, "omni_initiated_checkout")
            video_views = _extract_action_value(item.get("video_play_actions"), "video_view")

            rows.append(
                {
                    "date": item["date_start"],
                    "ad_account_id": account_id,
                    "ad_account_name": account_name,
                    "currency": currency,
                    "adset_id": item["adset_id"],
                    "adset_name": item.get("adset_name", ""),
                    "campaign_id": item.get("campaign_id", ""),
                    "campaign_name": item.get("campaign_name", ""),
                    "spend": float(item.get("spend", 0)),
                    "impressions": int(item.get("impressions", 0)),
                    "clicks": int(item.get("clicks", 0)),
                    "link_clicks": int(link_clicks or item.get("inline_link_clicks", 0)),
                    "landing_page_views": int(landing_page_views),
                    "add_to_cart": int(add_to_cart),
                    "initiate_checkout": int(initiate_checkout),
                    "video_views": int(video_views),
                    "purchases": int(purchases),
                    "purchase_value": _get_purchase_value(item),
                }
            )

        paging = data.get("paging", {})
        url = paging.get("next")
        params = None  # next URL already includes query params

    return rows


def fetch_adset_daily(since: str, until: str, account_ids=None):
    """Fetch adset-level daily insights between since and until (YYYY-MM-DD, inclusive),
    across all configured Meta ad accounts (same product, multiple business portfolios)."""
    account_ids = account_ids or META_AD_ACCOUNT_IDS
    rows = []
    for account_id in account_ids:
        rows.extend(_fetch_adset_daily_for_account(account_id, since, until))
    return rows


def _get_purchase_value(item):
    action_values = item.get("action_values")
    return _extract_action_value(action_values, "omni_purchase") if action_values else 0


def get_active_adset_ids(account_ids=None) -> set:
    """Returns the set of adset_ids currently effective_status == ACTIVE,
    across all configured Meta ad accounts."""
    account_ids = account_ids or META_AD_ACCOUNT_IDS
    active_ids = set()
    for account_id in account_ids:
        url = f"{BASE_URL}/{account_id}/adsets"
        params = {
            "fields": "id,effective_status",
            "filtering": '[{"field":"effective_status","operator":"IN","value":["ACTIVE"]}]',
            "access_token": META_ACCESS_TOKEN,
            "limit": 500,
        }
        while url:
            resp = requests.get(url, params=params if "?" not in url else None, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"Meta API error ({account_id}): {data['error']}")
            for item in data.get("data", []):
                active_ids.add(item["id"])
            paging = data.get("paging", {})
            url = paging.get("next")
            params = None
    return active_ids
