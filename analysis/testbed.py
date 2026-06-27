import numpy as np
import pandas as pd

from analysis.roas import load_adset_df
from config import TESTBED_AD_ACCOUNT_ID
from storage.db import fetch_meta_earliest_date

NAME_FILTER = "(구)"

PROMOTE_MIN_SPEND = 150_000
PROMOTE_MAX_CPA = 30_000

DROP_MIN_SPEND = 60_000


def _lifetime_df(report_date: str) -> pd.DataFrame:
    earliest = fetch_meta_earliest_date() or report_date
    df = load_adset_df(earliest, report_date)
    return df[df["ad_account_id"] == TESTBED_AD_ACCOUNT_ID]


def _adset_metrics(group: pd.DataFrame) -> dict:
    spend = group["spend"].sum()
    clicks = group["clicks"].sum()
    impressions = group["impressions"].sum()
    purchases = group["purchases"].sum()
    return {
        "spend": spend,
        "clicks": clicks,
        "impressions": impressions,
        "purchases": purchases,
        "cpa": (spend / purchases) if purchases else None,
        "cpc": (spend / clicks) if clicks else None,
        "ctr": (clicks / impressions) if impressions else None,
    }


def testbed_judgment(report_date: str) -> list:
    """
    Lifetime (since earliest data) performance + promote/drop verdict for the
    '(구)' marked adsets in the single-campaign testbed account. The promote/drop
    thresholds are absolute, but the "campaign 내 하위" (bottom-tier) check for
    early-drop is relative to every adset in the SAME campaign (not just the
    '(구)' ones), since that's the actual comparison set this account uses to
    judge creative performance.
    """
    df = _lifetime_df(report_date)
    if df.empty:
        return []

    results = []
    for campaign_id, camp_df in df.groupby("campaign_id"):
        per_adset = (
            camp_df.groupby(["adset_id", "adset_name"])
            .apply(lambda g: pd.Series(_adset_metrics(g)))
            .reset_index()
        )
        # campaign-wide bottom-tier thresholds (median): worse CPC = higher value,
        # worse CTR = lower value
        median_cpc = per_adset["cpc"].dropna().median()
        median_ctr = per_adset["ctr"].dropna().median()

        target = per_adset[per_adset["adset_name"].str.contains(NAME_FILTER, regex=False)]
        for _, row in target.iterrows():
            spend, purchases, cpa, cpc, ctr = row["spend"], row["purchases"], row["cpa"], row["cpc"], row["ctr"]

            is_bottom_tier = (cpc is not None and pd.notna(median_cpc) and cpc > median_cpc) or (
                ctr is not None and pd.notna(median_ctr) and ctr < median_ctr
            )

            if spend >= PROMOTE_MIN_SPEND and cpa is not None and cpa < PROMOTE_MAX_CPA:
                verdict = "1차 합격"
            elif spend >= DROP_MIN_SPEND and purchases == 0 and is_bottom_tier:
                verdict = "조기 탈락"
            else:
                verdict = "관찰중"

            results.append(
                {
                    "adset_id": row["adset_id"],
                    "adset_name": row["adset_name"],
                    "spend": spend,
                    "purchases": purchases,
                    "cpa": cpa,
                    "cpc": cpc,
                    "ctr": ctr,
                    "verdict": verdict,
                }
            )

    return sorted(results, key=lambda r: r["adset_name"])
