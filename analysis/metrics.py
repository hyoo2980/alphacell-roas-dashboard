import numpy as np
import pandas as pd


def shorten_adset_name(name: str) -> str:
    """Adset names like '메인02C_2S57/62/70/81번★(겐)' are long and mostly
    targeting/audience codes after the 'S' marker -- cut right after the first
    'S' to keep tables/charts compact. Names with no 'S' (e.g. '1C_62번_0426')
    are left untouched."""
    idx = name.find("S")
    return name[: idx + 1] if idx != -1 else name


METRIC_DEFS = {
    "구매당 비용": {"numerator": "spend", "denominator": "purchases", "higher_is_better": False},
    "결제시작당 비용": {"numerator": "spend", "denominator": "initiate_checkout", "higher_is_better": False},
    "장바구니담기당 비용": {"numerator": "spend", "denominator": "add_to_cart", "higher_is_better": False},
    "CPC": {"numerator": "spend", "denominator": "clicks", "higher_is_better": False},
    "동영상 3초 비용": {"numerator": "spend", "denominator": "video_views", "higher_is_better": False},
    "ROAS": {"numerator": "purchase_value", "denominator": "spend", "higher_is_better": True},
}

RAW_COLUMNS = [
    "spend",
    "impressions",
    "clicks",
    "add_to_cart",
    "initiate_checkout",
    "video_views",
    "purchases",
    "purchase_value",
]


def weekly_metrics_by_adset(df: pd.DataFrame) -> pd.DataFrame:
    """Weekly (Mon-Sun) aggregation per adset with every raw metric needed for
    the dashboard's cost/ROAS breakdowns."""
    if df.empty:
        return df
    weekly = df.copy()
    weekly["period_start"] = weekly["date"].dt.to_period("W-SUN").apply(lambda p: p.start_time.date())

    agg = (
        weekly.groupby(["period_start", "adset_id", "adset_name"])
        .agg({col: "sum" for col in RAW_COLUMNS})
        .reset_index()
    )

    for metric_name, d in METRIC_DEFS.items():
        col = _metric_col(metric_name)
        agg[col] = agg[d["numerator"]] / agg[d["denominator"]].replace(0, np.nan)

    return agg.sort_values(["adset_id", "period_start"])


def daily_metrics_by_adset(df: pd.DataFrame) -> pd.DataFrame:
    """Daily aggregation per adset with every raw metric -- same shape as
    weekly_metrics_by_adset but bucketed by calendar date instead of week,
    useful for adsets with too short a history for a meaningful weekly trend."""
    if df.empty:
        return df
    daily = df.copy()
    daily["period_start"] = daily["date"].dt.date

    agg = (
        daily.groupby(["period_start", "adset_id", "adset_name"])
        .agg({col: "sum" for col in RAW_COLUMNS})
        .reset_index()
    )

    for metric_name, d in METRIC_DEFS.items():
        col = _metric_col(metric_name)
        agg[col] = agg[d["numerator"]] / agg[d["denominator"]].replace(0, np.nan)

    return agg.sort_values(["adset_id", "period_start"])


def _metric_col(metric_name: str) -> str:
    return "metric__" + metric_name


def adset_label_options(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """Unique adset_id/adset_name pairs sorted by total spend (desc)."""
    if weekly_df.empty:
        return weekly_df
    return (
        weekly_df.groupby(["adset_id", "adset_name"])["spend"]
        .sum()
        .reset_index()
        .sort_values("spend", ascending=False)
    )


def detect_declining_adsets(weekly_df: pd.DataFrame, lookback_weeks: int = 3) -> list:
    """
    Flags adsets whose performance is trending worse across the most recent
    `lookback_weeks` weeks of data they have. "Worse" means: for cost metrics
    (CPA/CPC/video cost), the value has risen every week in a row; for ROAS,
    it has fallen every week in a row. Requires at least `lookback_weeks` weeks
    of history for that adset (adsets with sparse data are skipped to avoid
    noisy false positives) and a minimum spend floor so a handful of clicks on a
    near-zero-budget set doesn't get flagged.
    """
    if weekly_df.empty:
        return []

    warnings = []
    for adset_id, g in weekly_df.groupby("adset_id"):
        g = g.sort_values("period_start")
        if len(g) < lookback_weeks:
            continue
        recent = g.tail(lookback_weeks)
        if recent["spend"].sum() < 10000:  # skip near-zero-budget sets (noisy)
            continue

        adset_name = recent["adset_name"].iloc[0]
        bad_metrics = []
        for metric_name, d in METRIC_DEFS.items():
            col = _metric_col(metric_name)
            values = recent[col].tolist()
            if any(v != v for v in values):  # NaN present -> skip this metric
                continue
            if d["higher_is_better"]:
                worsening = all(values[i] > values[i + 1] for i in range(len(values) - 1))
            else:
                worsening = all(values[i] < values[i + 1] for i in range(len(values) - 1))
            if worsening:
                bad_metrics.append((metric_name, values[0], values[-1]))

        if bad_metrics:
            warnings.append(
                {
                    "adset_id": adset_id,
                    "adset_name": adset_name,
                    "bad_metrics": bad_metrics,
                    "weeks": [str(w) for w in recent["period_start"].tolist()],
                }
            )

    return warnings
