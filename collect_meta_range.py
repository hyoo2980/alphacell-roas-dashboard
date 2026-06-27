import sys

from collectors.meta import fetch_adset_daily
from storage.db import init_db, upsert_meta_adset_daily


def main(since: str, until: str, account_ids=None):
    init_db()
    rows = fetch_adset_daily(since, until, account_ids=account_ids)
    upsert_meta_adset_daily(rows)
    print(f"Upserted {len(rows)} adset-day rows from {since} to {until}")


if __name__ == "__main__":
    since, until = sys.argv[1], sys.argv[2]
    main(since, until)
