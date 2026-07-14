import base64

import requests

import config
from config import (
    CAFE24_CLIENT_ID,
    CAFE24_CLIENT_SECRET,
    CAFE24_MALL_ID,
    CAFE24_REDIRECT_URI,
)

API_BASE = f"https://{CAFE24_MALL_ID}.cafe24api.com/api/v2"

_token_cache = {"access_token": None}


def _basic_auth_header():
    raw = f"{CAFE24_CLIENT_ID}:{CAFE24_CLIENT_SECRET}".encode("utf-8")
    return base64.b64encode(raw).decode("utf-8")


def _refresh_access_token():
    """Uses the current refresh token to get a new access token, and persists
    the rotated refresh token back to .env (Cafe24 issues a new one each time).
    Reads the refresh token fresh from .env on disk rather than the cached
    config module attribute -- the daily pipeline (cron) and the long-running
    realtime watcher both refresh independently, and whichever ran most recently
    has already rotated the token on disk, invalidating any stale in-memory copy."""
    current_refresh_token = config.get_env_value("CAFE24_REFRESH_TOKEN", config.CAFE24_REFRESH_TOKEN)
    resp = requests.post(
        f"{API_BASE}/oauth/token",
        headers={
            "Authorization": f"Basic {_basic_auth_header()}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": current_refresh_token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    config.CAFE24_REFRESH_TOKEN = data["refresh_token"]
    config.update_env_value("CAFE24_REFRESH_TOKEN", data["refresh_token"])
    # access token + 만료시각도 저장 → 클라우드 워처가 재사용 가능 (2시간마다만 rotate)
    config.update_env_value("CAFE24_ACCESS_TOKEN", data["access_token"])
    config.update_env_value("CAFE24_ACCESS_TOKEN_EXPIRES_AT", data["expires_at"])

    _token_cache["access_token"] = data["access_token"]
    return data["access_token"]


def _get_access_token():
    if _token_cache["access_token"]:
        return _token_cache["access_token"]
    return _refresh_access_token()


def _get(path: str, params: dict):
    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=30)
    if resp.status_code == 401:
        token = _refresh_access_token()
        headers["Authorization"] = f"Bearer {token}"
        resp = requests.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_orders(date: str):
    """date: YYYY-MM-DD (KST). Returns list of order entries paid on that day,
    each including an "items" list (embed=items) so orders can be filtered down
    to '알파셀 올나잇 세이프' line items only."""
    orders = []
    offset = 0
    limit = 100
    while True:
        data = _get(
            "/admin/orders",
            {
                "start_date": date,
                "end_date": date,
                "date_type": "pay_date",
                "embed": "items",
                "limit": limit,
                "offset": offset,
            },
        )
        page_orders = data.get("orders", [])
        if not page_orders:
            break
        orders.extend(page_orders)
        if len(page_orders) < limit:
            break
        offset += limit
    return orders


TARGET_PRODUCT_KEYWORD = "올나잇"


def _is_target_item(item: dict) -> bool:
    """This Cafe24 store has so far only sold '알파셀 올나잇 세이프' (under various
    promo-label product names), but filter explicitly in case other products get
    added later -- same pattern as the Coupang/Naver collectors."""
    return TARGET_PRODUCT_KEYWORD in item.get("product_name", "")


def _payment_total(amount_info: dict) -> float:
    order_price = float(amount_info.get("order_price_amount", 0))
    shipping_fee = float(amount_info.get("shipping_fee", 0))
    coupon = float(amount_info.get("coupon_discount_price", 0)) + float(
        amount_info.get("coupon_shipping_fee_amount", 0)
    )
    other_discount = (
        float(amount_info.get("membership_discount_amount", 0))
        + float(amount_info.get("set_product_discount_amount", 0))
        + float(amount_info.get("shipping_fee_discount_amount", 0))
        + float(amount_info.get("app_discount_amount", 0))
    )
    return order_price + shipping_fee - coupon - other_discount


def summarize_daily_sales(date: str):
    """Returns (order_count, item_quantity, sales_amount) for the given date,
    counting ONLY orders that are 100% '알파셀 올나잇 세이프' line items.
    sales_amount matches the "결제합계" column in Cafe24's own 일별 매출내역 admin
    screen -- the pre-refund payment total, computed from initial_order_amount
    (the original order before any cancellation).
    Orders that mix target-product items with other products (e.g. 혈당 세이프)
    are excluded entirely rather than prorated, since a bundled "세트" SKU can
    contain both products under one line item with no reliable per-item split.
    Use fetch_refunds_by_order_date separately to get 환불합계 and compute
    순매출 = 결제합계 - 환불합계."""
    orders = fetch_orders(date)
    order_count = 0
    item_quantity = 0
    sales_amount = 0.0
    for o in orders:
        items = o.get("items", [])
        target_items = [i for i in items if _is_target_item(i)]
        if not target_items or len(target_items) != len(items):
            continue
        order_count += 1
        item_quantity += sum(int(i.get("quantity", 1)) for i in target_items)
        sales_amount += _payment_total(o.get("initial_order_amount", {}))
    return order_count, item_quantity, sales_amount


def fetch_refunds(refund_date_since: str, refund_date_until: str):
    """Returns raw refund entries whose refund_date (when the refund was actually
    processed) falls in [refund_date_since, refund_date_until]. Each entry carries
    its original order_date, which is what Cafe24's admin 환불합계 column buckets by
    (a refund processed days after the order still counts against the order's date)."""
    refunds = []
    offset = 0
    limit = 100
    while True:
        data = _get(
            "/admin/refunds",
            {
                "start_date": refund_date_since,
                "end_date": refund_date_until,
                "date_type": "refund_date",
                "limit": limit,
                "offset": offset,
            },
        )
        page = data.get("refunds", [])
        if not page:
            break
        refunds.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return refunds


def refund_totals_by_order_date(refund_date_since: str, refund_date_until: str) -> dict:
    """Scans refunds processed in [refund_date_since, refund_date_until] and buckets
    the refund amounts by each refund's original order_date (YYYY-MM-DD)."""
    refunds = fetch_refunds(refund_date_since, refund_date_until)
    totals = {}
    for r in refunds:
        order_date = r["order_date"][:10]
        totals[order_date] = totals.get(order_date, 0.0) + float(r.get("actual_refund_amount", 0))
    return totals
