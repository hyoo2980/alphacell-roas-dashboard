from datetime import date, timedelta

import numpy as np
import pandas as pd

from analysis.fx import get_usd_krw_rate
from config import BEP_ROAS
from storage.db import fetch_channel_daily, fetch_meta_adset_daily, fetch_refund_daily


def _date_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def load_adset_df(start_date: str, end_date: str) -> pd.DataFrame:
    """Loads Meta adset daily data across all configured ad accounts and converts
    USD spend/purchase_value to KRW. Each ad account can have its own reporting
    currency (e.g. one account is USD, another is already KRW) -- conversion is
    applied per-row based on that row's currency, never blindly to every row."""
    rows = fetch_meta_adset_daily(start_date, end_date)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])

    fx_rate = get_usd_krw_rate()
    is_usd = df["currency"] == "USD"
    df.loc[is_usd, "spend"] = df.loc[is_usd, "spend"] * fx_rate
    df.loc[is_usd, "purchase_value"] = df.loc[is_usd, "purchase_value"] * fx_rate

    df["roas"] = df["purchase_value"] / df["spend"].replace(0, np.nan)
    df["ctr"] = df["clicks"] / df["impressions"].replace(0, np.nan)
    df["lpv_rate"] = df["landing_page_views"] / df["link_clicks"].replace(0, np.nan)
    df["cvr"] = df["purchases"] / df["landing_page_views"].replace(0, np.nan)
    return df


def daily_roas_by_adset(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    agg = (
        df.groupby(["date", "adset_id", "adset_name"])
        .agg(spend=("spend", "sum"), purchase_value=("purchase_value", "sum"))
        .reset_index()
    )
    agg["roas"] = agg["purchase_value"] / agg["spend"].replace(0, np.nan)
    return agg.sort_values(["adset_id", "date"])


def weekly_roas_by_adset(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    weekly = df.copy()
    weekly["week"] = weekly["date"].dt.to_period("W-SUN").apply(lambda p: p.start_time.date())
    agg = (
        weekly.groupby(["week", "adset_id", "adset_name"])
        .agg(spend=("spend", "sum"), purchase_value=("purchase_value", "sum"))
        .reset_index()
    )
    agg["roas"] = agg["purchase_value"] / agg["spend"].replace(0, np.nan)
    return agg.sort_values(["adset_id", "week"])


def change_rates(daily_df: pd.DataFrame, periods=(1, 3, 5)) -> pd.DataFrame:
    """daily_df: output of daily_roas_by_adset. Adds N-day change columns per adset."""
    if daily_df.empty:
        return daily_df
    out = daily_df.copy()
    out = out.sort_values(["adset_id", "date"])
    for p in periods:
        out[f"roas_change_{p}d"] = out.groupby("adset_id")["roas"].pct_change(periods=p)
        out[f"spend_change_{p}d"] = out.groupby("adset_id")["spend"].pct_change(periods=p)
    return out


def latest_change_summary(daily_df: pd.DataFrame, periods=(1, 3, 5)) -> pd.DataFrame:
    """Returns the most recent row per adset with its N-day change rates."""
    changed = change_rates(daily_df, periods)
    if changed.empty:
        return changed
    latest = changed.sort_values("date").groupby("adset_id").tail(1)
    return latest.reset_index(drop=True)


def external_leakage_inference(df: pd.DataFrame) -> pd.DataFrame:
    """
    Infer which adsets are likely driving purchases on external channels
    (Coupang/Naver) that Meta's pixel cannot see.

    Hypothesis: our adsets optimize for different funnel goals (some for
    add-to-cart, some for checkout-init, some for purchase), so raw CTR/CVR
    aren't comparable across them -- an add-to-cart-optimized adset will always
    look "better" on CTR than a purchase-optimized one. A more targeted signal:
    중장년층(middle-aged/older) shoppers tend to drop off mid-funnel on the
    pixel-tracked store and finish the purchase on a more familiar app (Coupang/
    스마트스토어). The adset with the cheapest 장바구니 담기 CPA (spend per
    add-to-cart) is generating the most cart intent per won -- if that intent
    isn't converting into on-site purchases, it's the prime suspect for driving
    external-channel purchases instead.
    """
    if df.empty:
        return df

    agg = (
        df.groupby(["adset_id", "adset_name"])
        .agg(
            spend=("spend", "sum"),
            impressions=("impressions", "sum"),
            clicks=("clicks", "sum"),
            add_to_cart=("add_to_cart", "sum"),
            purchases=("purchases", "sum"),
            purchase_value=("purchase_value", "sum"),
        )
        .reset_index()
    )

    agg["roas"] = agg["purchase_value"] / agg["spend"].replace(0, np.nan)
    agg["add_to_cart_cpa"] = agg["spend"] / agg["add_to_cart"].replace(0, np.nan)
    agg["cart_to_purchase_rate"] = agg["purchases"] / agg["add_to_cart"].replace(0, np.nan)

    ranked = agg[agg["add_to_cart"] > 0].sort_values("add_to_cart_cpa")

    def grade_for(idx_position: int) -> str:
        if idx_position == 0:
            return "상"
        if idx_position <= 2:
            return "중"
        return "하"

    grade_map = {row.adset_id: grade_for(pos) for pos, row in enumerate(ranked.itertuples())}
    agg["leakage_grade"] = agg["adset_id"].map(grade_map).fillna("-")

    return agg.sort_values("add_to_cart_cpa", na_position="last")


def spend_by_account(df: pd.DataFrame) -> pd.DataFrame:
    """Per Meta ad-account spend breakdown (KRW, after currency conversion)."""
    if df.empty:
        return df
    agg = (
        df.groupby(["ad_account_id", "ad_account_name"])
        .agg(spend=("spend", "sum"))
        .reset_index()
        .sort_values("spend", ascending=False)
    )
    return agg


def account_roas_summary(df: pd.DataFrame, active_adset_ids: set) -> list:
    """For each Meta ad account: account-wide ROAS (matches what Ads Manager shows
    at the account level for this date range) plus a per-adset ROAS breakdown
    restricted to currently ACTIVE adsets (paused/inactive sets are excluded from
    the per-adset list, but still included in the account-wide total)."""
    if df.empty:
        return []

    results = []
    for account_id, acc_df in df.groupby("ad_account_id"):
        account_name = acc_df["ad_account_name"].iloc[0]
        acc_spend = acc_df["spend"].sum()
        acc_value = acc_df["purchase_value"].sum()
        acc_roas = (acc_value / acc_spend) if acc_spend else None

        active_df = acc_df[acc_df["adset_id"].isin(active_adset_ids)]
        adset_agg = (
            active_df.groupby(["adset_id", "adset_name"])
            .agg(
                spend=("spend", "sum"),
                purchase_value=("purchase_value", "sum"),
                impressions=("impressions", "sum"),
                clicks=("clicks", "sum"),
                landing_page_views=("landing_page_views", "sum"),
                purchases=("purchases", "sum"),
            )
            .reset_index()
        )
        adset_agg["roas"] = adset_agg["purchase_value"] / adset_agg["spend"].replace(0, np.nan)
        adset_agg["ctr"] = adset_agg["clicks"] / adset_agg["impressions"].replace(0, np.nan)
        adset_agg["cvr"] = adset_agg["purchases"] / adset_agg["landing_page_views"].replace(0, np.nan)

        results.append(
            {
                "ad_account_id": account_id,
                "ad_account_name": account_name,
                "account_spend": acc_spend,
                "account_value": acc_value,
                "account_roas": acc_roas,
                "active_adsets": adset_agg.sort_values("spend", ascending=False).to_dict("records"),
            }
        )
    return sorted(results, key=lambda r: r["account_spend"], reverse=True)


def brand_total_roas(start_date: str, end_date: str) -> dict:
    """
    True brand-level ROAS including external channels Meta's pixel cannot see.
    own-store (Cafe24) revenue comes from Cafe24's own order API (actual sales),
    not from Meta's pixel-attributed purchase value (which only reflects
    ad-driven conversions and undercounts organic/direct traffic).
    """
    adset_df = load_adset_df(start_date, end_date)
    total_spend = adset_df["spend"].sum() if not adset_df.empty else 0

    cafe24_rows = fetch_channel_daily("cafe24_daily", start_date, end_date)
    coupang_rows = fetch_channel_daily("coupang_daily", start_date, end_date)
    naver_rows = fetch_channel_daily("naver_daily", start_date, end_date)
    cafe24_refund_total = sum(
        r["refund_amount"] for r in fetch_refund_daily("cafe24_refund_daily", start_date, end_date)
    )
    naver_refund_total = sum(
        r["refund_amount"] for r in fetch_refund_daily("naver_refund_daily", start_date, end_date)
    )
    own_store_value = sum(r["sales_amount"] for r in cafe24_rows) - cafe24_refund_total
    coupang_value = sum(r["sales_amount"] for r in coupang_rows)
    naver_value = sum(r["sales_amount"] for r in naver_rows) - naver_refund_total

    total_value = own_store_value + coupang_value + naver_value
    own_store_roas = (own_store_value / total_spend) if total_spend else None
    brand_roas = (total_value / total_spend) if total_spend else None

    return {
        "total_spend": total_spend,
        "own_store_value": own_store_value,
        "coupang_value": coupang_value,
        "naver_value": naver_value,
        "total_value": total_value,
        "own_store_roas": own_store_roas,
        "brand_total_roas": brand_roas,
        "own_store_net_profit": estimate_net_profit(own_store_value, total_spend),
        "brand_net_profit": estimate_net_profit(total_value, total_spend),
    }


def estimate_net_profit(revenue: float, spend: float) -> float | None:
    """Estimated net profit using the product's break-even ROAS (BEP_ROAS):
    at BEP_ROAS, revenue exactly covers ad spend + COGS/fees (profit = 0).
    profit = revenue/BEP_ROAS - spend  (equivalently: spend * (ROAS - BEP_ROAS) / BEP_ROAS)
    """
    if not spend:
        return None
    return revenue / BEP_ROAS - spend
