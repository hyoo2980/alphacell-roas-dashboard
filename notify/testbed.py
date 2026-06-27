import pandas as pd
import requests

from analysis.testbed import testbed_judgment
from config import DISCORD_WEBHOOK_URL_TESTBED

VERDICT_EMOJI = {"1차 합격": "✅", "조기 탈락": "❌", "관찰중": "🔍"}


def _fmt_won(v):
    return f"{v:,.0f}원" if pd.notna(v) else "-"


def _fmt_pct(v):
    return f"{v*100:.2f}%" if pd.notna(v) else "-"


def build_testbed_report(report_date: str) -> dict:
    rows = testbed_judgment(report_date)

    if not rows:
        fields = [{"name": "데이터 없음", "value": "-", "inline": False}]
    else:
        fields = []
        for r in rows:
            value = (
                f"광고비: {_fmt_won(r['spend'])}\n"
                f"구매수: {r['purchases']:,.0f}건 | 구매당비용(CPA): {_fmt_won(r['cpa'])}\n"
                f"CPC: {_fmt_won(r['cpc'])} | CTR: {_fmt_pct(r['ctr'])}\n"
                f"판정: {VERDICT_EMOJI.get(r['verdict'], '')} {r['verdict']}"
            )
            fields.append({"name": r["adset_name"], "value": value, "inline": False})

    embed = {
        "title": f"테스트베드 세트 승격 판정 — {report_date} (전체기간 누적)",
        "color": 0xF1C40F,
        "description": (
            "1차 합격: 누적 광고비 150,000원 이상 + 구매당비용 30,000원 미만\n"
            "조기 탈락: 누적 광고비 60,000원 이상 + 구매 0건 + 캠페인 내 CPC/CTR 하위"
        ),
        "fields": fields,
    }
    return {"embeds": [embed]}


def send_testbed_report(report_date: str):
    payload = build_testbed_report(report_date)
    resp = requests.post(DISCORD_WEBHOOK_URL_TESTBED, json=payload, timeout=30)
    resp.raise_for_status()
