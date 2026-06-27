import sys
import time
from datetime import date, timedelta

from collectors.coupang import summarize_daily_sales
from storage.db import init_db, upsert_channel_daily


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
        upsert_channel_daily("coupang_daily", d, order_count, sales_amount, item_quantity)
        print(f"{d}: orders={order_count} qty={item_quantity} sales={sales_amount}")
        time.sleep(0.5)


if __name__ == "__main__":
    since, until = sys.argv[1], sys.argv[2]
    main(since, until)
