import sqlite3
from contextlib import contextmanager

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_adset_daily (
    date TEXT NOT NULL,
    adset_id TEXT NOT NULL,
    ad_account_id TEXT,
    ad_account_name TEXT,
    currency TEXT,
    adset_name TEXT,
    campaign_id TEXT,
    campaign_name TEXT,
    spend REAL DEFAULT 0,
    impressions INTEGER DEFAULT 0,
    clicks INTEGER DEFAULT 0,
    link_clicks INTEGER DEFAULT 0,
    landing_page_views INTEGER DEFAULT 0,
    add_to_cart INTEGER DEFAULT 0,
    initiate_checkout INTEGER DEFAULT 0,
    video_views INTEGER DEFAULT 0,
    purchases INTEGER DEFAULT 0,
    purchase_value REAL DEFAULT 0,
    PRIMARY KEY (date, adset_id)
);

CREATE TABLE IF NOT EXISTS coupang_daily (
    date TEXT NOT NULL PRIMARY KEY,
    order_count INTEGER DEFAULT 0,
    item_quantity INTEGER DEFAULT 0,
    sales_amount REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS naver_daily (
    date TEXT NOT NULL PRIMARY KEY,
    order_count INTEGER DEFAULT 0,
    item_quantity INTEGER DEFAULT 0,
    sales_amount REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cafe24_daily (
    date TEXT NOT NULL PRIMARY KEY,
    order_count INTEGER DEFAULT 0,
    item_quantity INTEGER DEFAULT 0,
    sales_amount REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cafe24_refund_daily (
    date TEXT NOT NULL PRIMARY KEY,
    refund_amount REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS naver_refund_daily (
    date TEXT NOT NULL PRIMARY KEY,
    refund_amount REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notified_orders (
    platform TEXT NOT NULL,
    order_id TEXT NOT NULL,
    notified_at TEXT NOT NULL,
    PRIMARY KEY (platform, order_id)
);
"""


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    for table in ("coupang_daily", "naver_daily", "cafe24_daily"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if "item_quantity" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN item_quantity INTEGER DEFAULT 0")

    cols = [r[1] for r in conn.execute("PRAGMA table_info(meta_adset_daily)")]
    if "ad_account_id" not in cols:
        conn.execute("ALTER TABLE meta_adset_daily ADD COLUMN ad_account_id TEXT")
    if "currency" not in cols:
        conn.execute("ALTER TABLE meta_adset_daily ADD COLUMN currency TEXT")
    if "ad_account_name" not in cols:
        conn.execute("ALTER TABLE meta_adset_daily ADD COLUMN ad_account_name TEXT")
    if "add_to_cart" not in cols:
        conn.execute("ALTER TABLE meta_adset_daily ADD COLUMN add_to_cart INTEGER DEFAULT 0")
    if "initiate_checkout" not in cols:
        conn.execute("ALTER TABLE meta_adset_daily ADD COLUMN initiate_checkout INTEGER DEFAULT 0")
    if "video_views" not in cols:
        conn.execute("ALTER TABLE meta_adset_daily ADD COLUMN video_views INTEGER DEFAULT 0")


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def upsert_meta_adset_daily(rows):
    """rows: list of dicts with keys matching meta_adset_daily columns"""
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO meta_adset_daily
                (date, adset_id, ad_account_id, ad_account_name, currency, adset_name, campaign_id, campaign_name,
                 spend, impressions, clicks, link_clicks, landing_page_views,
                 add_to_cart, initiate_checkout, video_views, purchases, purchase_value)
            VALUES (:date, :adset_id, :ad_account_id, :ad_account_name, :currency, :adset_name, :campaign_id, :campaign_name,
                    :spend, :impressions, :clicks, :link_clicks, :landing_page_views,
                    :add_to_cart, :initiate_checkout, :video_views, :purchases, :purchase_value)
            ON CONFLICT(date, adset_id) DO UPDATE SET
                ad_account_id=excluded.ad_account_id,
                ad_account_name=excluded.ad_account_name,
                currency=excluded.currency,
                adset_name=excluded.adset_name,
                campaign_id=excluded.campaign_id,
                campaign_name=excluded.campaign_name,
                spend=excluded.spend,
                impressions=excluded.impressions,
                clicks=excluded.clicks,
                link_clicks=excluded.link_clicks,
                landing_page_views=excluded.landing_page_views,
                add_to_cart=excluded.add_to_cart,
                initiate_checkout=excluded.initiate_checkout,
                video_views=excluded.video_views,
                purchases=excluded.purchases,
                purchase_value=excluded.purchase_value
            """,
            rows,
        )


def upsert_channel_daily(table: str, date: str, order_count: int, sales_amount: float, item_quantity: int = 0):
    assert table in ("coupang_daily", "naver_daily", "cafe24_daily")
    with get_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO {table} (date, order_count, item_quantity, sales_amount)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                order_count=excluded.order_count,
                item_quantity=excluded.item_quantity,
                sales_amount=excluded.sales_amount
            """,
            (date, order_count, item_quantity, sales_amount),
        )


def upsert_refund_daily(table: str, date: str, refund_amount: float):
    assert table in ("cafe24_refund_daily", "naver_refund_daily")
    with get_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO {table} (date, refund_amount)
            VALUES (?, ?)
            ON CONFLICT(date) DO UPDATE SET refund_amount=excluded.refund_amount
            """,
            (date, refund_amount),
        )


def filter_unnotified_order_ids(platform: str, order_ids: list) -> list:
    """Returns the subset of order_ids not already recorded as notified for this platform."""
    if not order_ids:
        return []
    with get_conn() as conn:
        placeholders = ",".join("?" * len(order_ids))
        cur = conn.execute(
            f"SELECT order_id FROM notified_orders WHERE platform=? AND order_id IN ({placeholders})",
            (platform, *order_ids),
        )
        already = {r[0] for r in cur.fetchall()}
    return [oid for oid in order_ids if oid not in already]


def mark_orders_notified(platform: str, order_ids: list):
    if not order_ids:
        return
    now = datetime_now_iso()
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO notified_orders (platform, order_id, notified_at) VALUES (?, ?, ?)",
            [(platform, oid, now) for oid in order_ids],
        )


def datetime_now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat()


def fetch_refund_daily(table: str, start_date: str, end_date: str):
    assert table in ("cafe24_refund_daily", "naver_refund_daily")
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            f"SELECT * FROM {table} WHERE date BETWEEN ? AND ? ORDER BY date",
            (start_date, end_date),
        )
        return [dict(r) for r in cur.fetchall()]


def fetch_meta_earliest_date() -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT MIN(date) FROM meta_adset_daily").fetchone()
        return row[0] if row else None


def fetch_meta_adset_daily(start_date: str, end_date: str):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT * FROM meta_adset_daily
            WHERE date BETWEEN ? AND ?
            ORDER BY date, adset_id
            """,
            (start_date, end_date),
        )
        return [dict(r) for r in cur.fetchall()]


def fetch_channel_daily(table: str, start_date: str, end_date: str):
    assert table in ("coupang_daily", "naver_daily", "cafe24_daily")
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            f"SELECT * FROM {table} WHERE date BETWEEN ? AND ? ORDER BY date",
            (start_date, end_date),
        )
        return [dict(r) for r in cur.fetchall()]
