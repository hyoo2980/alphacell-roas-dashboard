from datetime import date

import requests

from collectors.cafe24 import _is_target_item as _is_target_cafe24_item
from collectors.cafe24 import fetch_orders as fetch_cafe24_orders
from collectors.coupang import _is_target_item, fetch_ordersheets
from collectors.naver import _is_target_product_order, fetch_product_orders
from config import DISCORD_WEBHOOK_URL_ORDERS
from storage.db import filter_unnotified_order_ids, init_db, mark_orders_notified

COLOR_BY_PLATFORM = {
    "cafe24": 0x1E88E5,
    "coupang": 0xE53935,
    "naver": 0x43A047,
}
EMOJI_BY_PLATFORM = {
    "cafe24": "🛍️",
    "coupang": "📦",
    "naver": "🟢",
}
LABEL_BY_PLATFORM = {
    "cafe24": "자사몰(카페24)",
    "coupang": "쿠팡",
    "naver": "스마트스토어",
}


def _send_order_alert(platform: str, order_id: str, amount: float, detail: str = ""):
    embed = {
        "title": f"{EMOJI_BY_PLATFORM[platform]} 새 주문 — {LABEL_BY_PLATFORM[platform]}",
        "color": COLOR_BY_PLATFORM[platform],
        "fields": [
            {"name": "주문번호", "value": str(order_id), "inline": True},
            {"name": "금액", "value": f"{amount:,.0f}원", "inline": True},
        ],
    }
    if detail:
        embed["fields"].append({"name": "상세", "value": detail, "inline": False})
    resp = requests.post(DISCORD_WEBHOOK_URL_ORDERS, json={"embeds": [embed]}, timeout=30)
    resp.raise_for_status()


def _is_pure_target_order_cafe24(o: dict) -> bool:
    items = o.get("items", [])
    target_items = [i for i in items if _is_target_cafe24_item(i)]
    return bool(target_items) and len(target_items) == len(items)


def _is_pure_target_order_coupang(o: dict) -> bool:
    items = o.get("orderItems", [])
    target_items = [i for i in items if _is_target_item(i)]
    return bool(target_items) and len(target_items) == len(items)


def check_cafe24_new_orders(today: str):
    orders = fetch_cafe24_orders(today)
    target_orders = [o for o in orders if _is_pure_target_order_cafe24(o)]
    order_ids = [o["order_id"] for o in target_orders]
    new_ids = set(filter_unnotified_order_ids("cafe24", order_ids))
    if not new_ids:
        return 0
    for o in target_orders:
        if o["order_id"] not in new_ids:
            continue
        amount = float(o["actual_order_amount"].get("payment_amount", 0))
        target_items = [i for i in o.get("items", []) if _is_target_cafe24_item(i)]
        names = ", ".join(i.get("product_name", "") for i in target_items)
        _send_order_alert("cafe24", o["order_id"], amount, detail=names)
    mark_orders_notified("cafe24", list(new_ids))
    return len(new_ids)


def check_coupang_new_orders(today: str):
    orders = fetch_ordersheets(today)
    target_orders = [o for o in orders if _is_pure_target_order_coupang(o)]
    order_ids = [str(o["orderId"]) for o in target_orders]
    new_ids = set(filter_unnotified_order_ids("coupang", order_ids))
    if not new_ids:
        return 0
    for o in target_orders:
        oid = str(o["orderId"])
        if oid not in new_ids:
            continue
        target_items = [i for i in o.get("orderItems", []) if _is_target_item(i)]
        amount = sum(float(i.get("salesPrice", 0)) * int(i.get("shippingCount", 1)) for i in target_items)
        amount += float(o.get("shippingPrice", 0))
        names = ", ".join(i.get("sellerProductName", "") for i in target_items)
        _send_order_alert("coupang", oid, amount, detail=names)
    mark_orders_notified("coupang", list(new_ids))
    return len(new_ids)


def check_naver_new_orders(today: str):
    orders = fetch_product_orders(today)
    target_orders = [
        o for o in orders if _is_target_product_order(o.get("content", {}).get("productOrder", {}))
    ]
    order_ids = [o["content"]["productOrder"]["productOrderId"] for o in target_orders]
    new_ids = set(filter_unnotified_order_ids("naver", order_ids))
    if not new_ids:
        return 0
    for o in target_orders:
        po = o["content"]["productOrder"]
        if po["productOrderId"] not in new_ids:
            continue
        amount = float(po.get("totalPaymentAmount", 0)) + float(po.get("deliveryFeeAmount", 0))
        _send_order_alert("naver", po["productOrderId"], amount, detail=po.get("productName", ""))
    mark_orders_notified("naver", list(new_ids))
    return len(new_ids)


def bootstrap_seen_orders():
    """Marks all of today's existing orders as already-notified WITHOUT sending
    alerts. Run this once before starting the watch loop so the first poll
    doesn't dump every order placed earlier today."""
    init_db()
    today = date.today().isoformat()

    cafe24_orders = fetch_cafe24_orders(today)
    cafe24_ids = [o["order_id"] for o in cafe24_orders if _is_pure_target_order_cafe24(o)]
    mark_orders_notified("cafe24", cafe24_ids)

    coupang_orders = fetch_ordersheets(today)
    coupang_ids = [str(o["orderId"]) for o in coupang_orders if _is_pure_target_order_coupang(o)]
    mark_orders_notified("coupang", coupang_ids)

    naver_orders = fetch_product_orders(today)
    naver_ids = [
        o["content"]["productOrder"]["productOrderId"]
        for o in naver_orders
        if _is_target_product_order(o.get("content", {}).get("productOrder", {}))
    ]
    mark_orders_notified("naver", naver_ids)

    print(f"부트스트랩 완료: cafe24={len(cafe24_ids)} coupang={len(coupang_ids)} naver={len(naver_ids)}건을 기존 주문으로 기록")


def check_all_new_orders():
    """Single poll pass across all 3 channels for today's orders. Safe to call
    repeatedly -- already-notified order IDs are skipped via the notified_orders table."""
    init_db()
    today = date.today().isoformat()
    counts = {}
    for platform, fn in (
        ("cafe24", check_cafe24_new_orders),
        ("coupang", check_coupang_new_orders),
        ("naver", check_naver_new_orders),
    ):
        try:
            counts[platform] = fn(today)
        except Exception as e:
            counts[platform] = f"ERROR: {e}"
    return counts
