import base64
import time

import bcrypt
import requests

from config import NAVER_CLIENT_ID, NAVER_CLIENT_SECRET

BASE_URL = "https://api.commerce.naver.com/external"

_token_cache = {"token": None, "expires_at": 0}


def _get_access_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    timestamp = str(int(now * 1000))
    password = f"{NAVER_CLIENT_ID}_{timestamp}"
    hashed = bcrypt.hashpw(password.encode("utf-8"), NAVER_CLIENT_SECRET.encode("utf-8"))
    client_secret_sign = base64.b64encode(hashed).decode("utf-8")

    resp = requests.post(
        f"{BASE_URL}/v1/oauth2/token",
        data={
            "client_id": NAVER_CLIENT_ID,
            "timestamp": timestamp,
            "client_secret_sign": client_secret_sign,
            "grant_type": "client_credentials",
            "type": "SELF",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]
    expires_in = int(data.get("expires_in", 1800))
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expires_in - 60
    return token


def _get_with_retry(url, headers, params, max_retries=4):
    delay = 1.0
    for attempt in range(max_retries):
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 429:
            time.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def fetch_product_orders(date: str):
    """date: YYYY-MM-DD (KST). Returns list of order entries for that day."""
    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    from_dt = f"{date}T00:00:00.000+09:00"
    to_dt = f"{date}T23:59:59.999+09:00"

    params = {
        "rangeType": "PAYED_DATETIME",
        "from": from_dt,
        "to": to_dt,
    }

    orders = []
    page = 1
    while True:
        params["page"] = page
        resp = _get_with_retry(
            f"{BASE_URL}/v1/pay-order/seller/product-orders", headers, params
        )
        contents = resp.json().get("data", {}).get("contents", [])
        if not contents:
            break
        orders.extend(contents)
        if len(contents) < 100:
            break
        page += 1
        time.sleep(0.3)
    return orders


TARGET_PRODUCT_KEYWORD = "올나잇"


def _is_target_product_order(product_order: dict) -> bool:
    """This smartstore seller account also sells '알파셀 혈당 세이프', so every
    product-order must be filtered down to '알파셀 올나잇 세이프' line items only."""
    return TARGET_PRODUCT_KEYWORD in product_order.get("productName", "")


DONE_CLAIM_STATUSES = {"CANCEL_DONE", "RETURN_DONE"}


def fetch_claim_changes(date: str):
    """date: YYYY-MM-DD (KST). Returns last-changed-status entries on that day
    (claim completion date, not the order's original payment date -- Naver's own
    환불금액 admin column buckets by this claim-completion date)."""
    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "lastChangedFrom": f"{date}T00:00:00.000+09:00",
        "lastChangedTo": f"{date}T23:59:59.999+09:00",
    }
    resp = _get_with_retry(
        f"{BASE_URL}/v1/pay-order/seller/product-orders/last-changed-statuses", headers, params
    )
    return resp.json().get("data", {}).get("lastChangeStatuses", [])


def _fetch_product_orders_by_ids(product_order_ids: list):
    if not product_order_ids:
        return []
    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    results = []
    for i in range(0, len(product_order_ids), 100):
        chunk = product_order_ids[i : i + 100]
        resp = requests.post(
            f"{BASE_URL}/v1/pay-order/seller/product-orders/query",
            headers=headers,
            json={"productOrderIds": chunk},
            timeout=30,
        )
        resp.raise_for_status()
        results.extend(resp.json().get("data", []))
    return results


def refund_total_for_date(date: str) -> float:
    """Returns the total refund amount (CANCEL/RETURN completed) for the given
    claim-completion date, counting only '알파셀 올나잇 세이프' product-orders.
    Matches the 환불금액 column in Naver's own 판매분석 admin screen."""
    changes = fetch_claim_changes(date)
    done_ids = {
        c["productOrderId"] for c in changes if c.get("claimStatus") in DONE_CLAIM_STATUSES
    }
    if not done_ids:
        return 0.0

    orders = _fetch_product_orders_by_ids(list(done_ids))
    total = 0.0
    for o in orders:
        product_order = o.get("productOrder", {})
        if not _is_target_product_order(product_order):
            continue
        total += float(product_order.get("totalPaymentAmount", 0))
    return total


def summarize_daily_sales(date: str):
    """Returns (order_count, item_quantity, sales_amount) for the given date,
    counting ONLY '알파셀 올나잇 세이프' product-orders. sales_amount = totalPaymentAmount
    (product price after discounts) + deliveryFeeAmount (shipping fee), since each
    product-order here is a single-product line item (no mixed-product orders to
    prorate shipping across, unlike Coupang's order-level shipping)."""
    orders = fetch_product_orders(date)
    order_count = 0
    item_quantity = 0
    sales_amount = 0.0
    for o in orders:
        product_order = o.get("content", {}).get("productOrder", {})
        if not _is_target_product_order(product_order):
            continue
        order_count += 1
        item_quantity += int(product_order.get("quantity", 1))
        sales_amount += float(product_order.get("totalPaymentAmount", 0))
        sales_amount += float(product_order.get("deliveryFeeAmount", 0))
    return order_count, item_quantity, sales_amount
