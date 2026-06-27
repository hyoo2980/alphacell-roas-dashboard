from datetime import date, timedelta

import requests

from analysis.roas import (
    account_roas_summary,
    brand_total_roas,
    daily_roas_by_adset,
    external_leakage_inference,
    latest_change_summary,
    load_adset_df,
    spend_by_account,
    weekly_roas_by_adset,
)
from collectors.meta import get_active_adset_ids
from config import DISCORD_WEBHOOK_URL, DISCORD_WEBHOOK_URL_ACCOUNTS
from storage.db import fetch_meta_earliest_date


def _fmt_pct(x):
    if x is None or x != x:  # NaN check
        return "-"
    return f"{x * 100:+.1f}%"


def _fmt_roas(x):
    if x is None or x != x:
        return "-"
    return f"{x:.2f}"


def _fmt_roas_breakdown(roas, revenue, spend, net_profit=None):
    """e.g. "2.07 (1,037,400/500,032) | 순이익 약 +135,936원" """
    if roas is None or roas != roas:
        return "-"
    base = f"{roas:.2f} ({revenue:,.0f}/{spend:,.0f})"
    if net_profit is not None and net_profit == net_profit:
        base += f" | 순이익(추정) {net_profit:+,.0f}원"
    return base


def _period_label(start_str: str, end_str: str, days: int) -> str:
    return f"최근 {days}일 누적 브랜드 ROAS ({start_str} ~ {end_str})"


def build_report(report_date: str) -> dict:
    target = date.fromisoformat(report_date)
    week_start = (target - timedelta(days=6)).isoformat()
    twoweek_start = (target - timedelta(days=13)).isoformat()
    month_start = (target - timedelta(days=29)).isoformat()

    df = load_adset_df(week_start, report_date)
    daily = daily_roas_by_adset(df)
    weekly = weekly_roas_by_adset(df)
    changes = latest_change_summary(daily)
    leak = external_leakage_inference(df)
    brand_today = brand_total_roas(report_date, report_date)
    brand_week = brand_total_roas(week_start, report_date)
    brand_2week = brand_total_roas(twoweek_start, report_date)
    brand_month = brand_total_roas(month_start, report_date)

    today_df = load_adset_df(report_date, report_date)
    accounts_today = spend_by_account(today_df)

    fields = []

    account_lines = (
        "\n".join(
            f"- {r['ad_account_name']}: {r['spend']:,.0f}원" for _, r in accounts_today.iterrows()
        )
        if not accounts_today.empty
        else "-"
    )

    fields.append(
        {
            "name": f"📊 브랜드 전체 ROAS ({report_date})",
            "value": (
                f"광고비 총액: {brand_today['total_spend']:,.0f}원\n"
                f"{account_lines}\n"
                f"자사몰(카페24) 매출: {brand_today['own_store_value']:,.0f}원\n"
                f"쿠팡 매출: {brand_today['coupang_value']:,.0f}원\n"
                f"스마트스토어 매출: {brand_today['naver_value']:,.0f}원\n"
                f"외부몰 매출 합계: {brand_today['coupang_value'] + brand_today['naver_value']:,.0f}원\n"
                f"브랜드 종합 ROAS(추정): {_fmt_roas_breakdown(brand_today['brand_total_roas'], brand_today['total_value'], brand_today['total_spend'], brand_today['brand_net_profit'])}"
            ),
            "inline": False,
        }
    )

    for label_days, start_str, brand in (
        (7, week_start, brand_week),
        (14, twoweek_start, brand_2week),
        (30, month_start, brand_month),
    ):
        period_df = load_adset_df(start_str, report_date)
        accounts_period = spend_by_account(period_df)
        period_account_lines = (
            "\n".join(
                f"- {r['ad_account_name']}: {r['spend']:,.0f}원" for _, r in accounts_period.iterrows()
            )
            if not accounts_period.empty
            else "-"
        )
        fields.append(
            {
                "name": f"📅 {_period_label(start_str, report_date, label_days)}",
                "value": (
                    f"광고비 총액: {brand['total_spend']:,.0f}원\n"
                    f"{period_account_lines}\n"
                    f"자사몰 매출: {brand['own_store_value']:,.0f}원\n"
                    f"쿠팡 매출: {brand['coupang_value']:,.0f}원 | 스마트스토어 매출: {brand['naver_value']:,.0f}원\n"
                    f"종합 ROAS(추정): {_fmt_roas_breakdown(brand['brand_total_roas'], brand['total_value'], brand['total_spend'], brand['brand_net_profit'])}"
                ),
                "inline": False,
            }
        )

    if not changes.empty:
        top = changes.sort_values("spend", ascending=False).head(5)
        lines = []
        for _, r in top.iterrows():
            lines.append(
                f"**{_shorten_adset_name(r['adset_name'])}**\n"
                f"ROAS {_fmt_roas(r['roas'])} | 1일 {_fmt_pct(r['roas_change_1d'])} · "
                f"3일 {_fmt_pct(r['roas_change_3d'])} · 5일 {_fmt_pct(r['roas_change_5d'])}"
            )
        fields.append(
            {
                "name": "📈 광고세트별 ROAS 변동 (광고비 상위 5개)",
                "value": "\n\n".join(lines)[:1000],
                "inline": False,
            }
        )

    if not leak.empty:
        suspect = leak[leak["leakage_grade"] == "상"]
        if not suspect.empty:
            lines = []
            for _, r in suspect.iterrows():
                lines.append(
                    f"**{_shorten_adset_name(r['adset_name'])}** — 장바구니 CPA {r['add_to_cart_cpa']:,.0f}원 "
                    f"/ 장바구니→구매전환 {r['cart_to_purchase_rate']*100:.1f}% (자사몰 ROAS {_fmt_roas(r['roas'])})"
                )
            fields.append(
                {
                    "name": "🔍 외부몰(쿠팡/스마트스토어) 구매 유출 추정 — 상위 의심 세트",
                    "value": "\n".join(lines)[:1000],
                    "inline": False,
                }
            )

    embed = {
        "title": f"ROAS 일일 리포트 — {report_date}",
        "color": 0x5865F2,
        "fields": fields,
    }
    return {"embeds": [embed]}


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _shorten_adset_name(name: str) -> str:
    """Adset names like '메인02C_2S57/62/70/81번★(겐)' are long and mostly
    targeting/audience codes after the 'S' marker -- cut right after the first
    'S' to keep the table compact."""
    idx = name.find("S")
    return name[: idx + 1] if idx != -1 else name


def _build_adset_table(active_adsets: list) -> str:
    """Monospace code-block table: 세트명 | 광고비 | 매출 | ROAS | 구매CPA"""
    headers = ["세트", "광고비", "매출", "ROAS", "CPA"]
    widths = [16, 10, 10, 5, 8]
    lines = [" ".join(h.ljust(w) for h, w in zip(headers, widths))]
    lines.append("-" * sum(widths) + "-" * (len(widths) - 1))
    for s in sorted(active_adsets, key=lambda s: s["adset_name"]):
        cpa = s["spend"] / s["purchases"] if s.get("purchases") else None
        row = [
            _truncate(_shorten_adset_name(s["adset_name"]), widths[0]).ljust(widths[0]),
            f"{s['spend']:,.0f}".rjust(widths[1]),
            f"{s['purchase_value']:,.0f}".rjust(widths[2]),
            (f"{s['roas']:.2f}" if s['roas'] == s['roas'] else "-").rjust(widths[3]),
            (f"{cpa:,.0f}" if cpa else "-").rjust(widths[4]),
        ]
        lines.append(" ".join(row))
    return "```\n" + "\n".join(lines) + "\n```"


def build_account_report(report_date: str) -> dict:
    """Per-account / per-active-adset ROAS (table format), broken out into its
    own message (separate Discord channel) across 1/7/14/30-day + 전체기간
    windows, each labeled with its explicit date range."""
    target = date.fromisoformat(report_date)
    earliest = fetch_meta_earliest_date() or report_date
    periods = [
        (1, report_date, report_date),
        (7, (target - timedelta(days=6)).isoformat(), report_date),
        (14, (target - timedelta(days=13)).isoformat(), report_date),
        (30, (target - timedelta(days=29)).isoformat(), report_date),
        ("전체기간", earliest, report_date),
    ]

    active_adset_ids = get_active_adset_ids()
    embeds = []

    for label, start_str, end_str in periods:
        df = load_adset_df(start_str, end_str)
        account_summaries = account_roas_summary(df, active_adset_ids)
        label_str = f"최근 {label}일" if isinstance(label, int) else label

        fields = []
        for acc in account_summaries:
            value = f"계정 전체 ROAS: {_fmt_roas(acc['account_roas'])} ({acc['account_value']:,.0f}/{acc['account_spend']:,.0f})\n"
            value += (
                _build_adset_table(acc["active_adsets"]) if acc["active_adsets"] else "활성 세트 없음"
            )
            fields.append(
                {
                    "name": f"🧾 {acc['ad_account_name']}",
                    "value": value[:1024],
                    "inline": False,
                }
            )

        if not fields:
            fields = [{"name": "데이터 없음", "value": "-", "inline": False}]

        embeds.append(
            {
                "title": f"광고계정/세트 ROAS — {label_str} ({start_str} ~ {end_str})",
                "color": 0x57F287,
                "fields": fields,
            }
        )

    return {"embeds": embeds}


def send_daily_report(report_date: str):
    payload = build_report(report_date)
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    resp.raise_for_status()


def send_account_report(report_date: str):
    payload = build_account_report(report_date)
    resp = requests.post(DISCORD_WEBHOOK_URL_ACCOUNTS, json=payload, timeout=30)
    resp.raise_for_status()
