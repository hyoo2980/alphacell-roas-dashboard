import sys
import time
from datetime import date, timedelta

from collectors.cafe24 import refund_totals_by_order_date, summarize_daily_sales
from storage.db import init_db, upsert_channel_daily, upsert_refund_daily


def daterange(since: str, until: str):
    d0 = date.fromisoformat(since)
    d1 = date.fromisoformat(until)
    cur = d0
    while cur <= d1:
        yield cur.isoformat()
        cur += timedelta(days=1)


def main(since: str, until: str):
    init_db()
    for d in daterange(since, until):
        order_count, item_quantity, sales_amount = summarize_daily_sales(d)
        upsert_channel_daily("cafe24_daily", d, order_count, sales_amount, item_quantity)
        print(f"{d}: orders={order_count} qty={item_quantity} sales={sales_amount}")
        time.sleep(0.3)

    # Refunds can be processed days after the original order, so scan refund_date
    # from `since` through today to catch any refund tied back to an order in range.
    refund_scan_until = max(until, date.today().isoformat())
    refund_totals = refund_totals_by_order_date(since, refund_scan_until)
    for d in daterange(since, until):
        amount = refund_totals.get(d, 0.0)
        upsert_refund_daily("cafe24_refund_daily", d, amount)
        if amount:
            print(f"{d}: refund={amount}")


if __name__ == "__main__":
    since, until = sys.argv[1], sys.argv[2]
    main(since, until)
