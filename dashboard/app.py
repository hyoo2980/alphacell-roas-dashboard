import sys
from datetime import date, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from analysis.metrics import (
    METRIC_DEFS,
    _metric_col,
    adset_label_options,
    daily_metrics_by_adset,
    detect_declining_adsets,
    shorten_adset_name,
    weekly_metrics_by_adset,
)
from analysis.roas import (
    brand_total_roas,
    daily_roas_by_adset,
    external_leakage_inference,
    latest_change_summary,
    load_adset_df,
    spend_by_account,
)
from collectors.meta import get_active_adset_ids

st.set_page_config(page_title="알파셀 ROAS 대시보드", layout="wide")
st.title("📊 알파셀 올나잇세이프 메타 광고 대시보드")

today = date.today()
default_start = today - timedelta(days=70)  # ~10 weeks of weekly history

col1, col2 = st.columns(2)
with col1:
    start_date = st.date_input("시작일", value=default_start)
with col2:
    end_date = st.date_input("종료일", value=today - timedelta(days=1))

start_str, end_str = start_date.isoformat(), end_date.isoformat()

df = load_adset_df(start_str, end_str)

if df.empty:
    st.warning("선택한 기간에 데이터가 없습니다.")
    st.stop()

active_adset_ids = get_active_adset_ids()
df = df[df["adset_id"].isin(active_adset_ids)]

if df.empty:
    st.warning("현재 활성화된 광고 세트의 데이터가 선택한 기간에 없습니다.")
    st.stop()

df = df.copy()
df["adset_name"] = df["adset_name"].apply(shorten_adset_name)

weekly = weekly_metrics_by_adset(df)
daily_metrics = daily_metrics_by_adset(df)

# ---------------------------------------------------------------------------
# 핵심 지표 탐색기 (메타 광고 데이터 기준)
# ---------------------------------------------------------------------------
st.subheader("📈 핵심 지표 탐색기")

view_mode = st.radio("단위 선택", options=["주단위", "일단위"], horizontal=True)
period_df = weekly if view_mode == "주단위" else daily_metrics
period_unit_label = "주차" if view_mode == "주단위" else "날짜"

adset_opts = adset_label_options(period_df).sort_values("adset_name", ascending=True)
adset_opts["label"] = adset_opts["adset_name"].str.slice(0, 14)
id_by_label = dict(zip(adset_opts["label"], adset_opts["adset_id"]))

st.markdown("**광고 세트 선택**")
selected_label = st.radio(
    "adset", options=adset_opts["label"].tolist(), horizontal=True, label_visibility="collapsed"
)
selected_adset_id = id_by_label[selected_label]

st.markdown("**지표 선택**")
selected_metric = st.radio(
    "metric", options=list(METRIC_DEFS.keys()), horizontal=True, label_visibility="collapsed", index=5
)

metric_col = _metric_col(selected_metric)
higher_is_better = METRIC_DEFS[selected_metric]["higher_is_better"]

st.markdown(f"**전체 세트 {period_unit_label}별 비교 — {selected_metric}**")
is_currency_metric_table = selected_metric != "ROAS"
pivot = period_df.pivot_table(index="adset_name", columns="period_start", values=metric_col)
pivot = pivot.reindex(sorted(pivot.index), axis=0)
cmap = "RdYlGn_r" if not higher_is_better else "RdYlGn"
is_roas = selected_metric == "ROAS"

if view_mode == "주단위":
    cell_fmt = "₩{:,.0f}" if is_currency_metric_table else "{:.2f}"
    if is_roas:
        st.caption("전체 세트 공통 기준(0~2.0)으로 나쁜 구간은 빨간색, 좋은 구간은 초록색입니다.")
        styled = pivot.style.format(cell_fmt, na_rep="-").background_gradient(cmap=cmap, axis=None, vmin=0, vmax=2.0)
    else:
        st.caption("세트별(행 기준) 상대 비교로 나쁜 구간은 빨간색, 좋은 구간은 초록색입니다.")
        styled = pivot.style.format(cell_fmt, na_rep="-").background_gradient(cmap=cmap, axis=1)
    st.dataframe(styled, use_container_width=True)
else:
    hover_fmt = (lambda v: f"₩{v:,.0f}") if is_currency_metric_table else (lambda v: f"{v:.2f}")
    hover_text = pivot.apply(lambda row: row.map(lambda v: hover_fmt(v) if pd.notna(v) else "데이터 없음"))

    if is_roas:
        st.caption("스크롤 없이 전체 기간을 한눈에 보도록 색상 히트맵으로 표시합니다 (전체 세트 공통 0~2.0 기준). 칸에 마우스를 올리면 실제 값이 보입니다.")
        z_values = pivot.values
        zmin, zmax = 0, 2.0
        colorscale = "RdYlGn"
    else:
        st.caption("스크롤 없이 전체 기간을 한눈에 보도록 색상 히트맵으로 표시합니다 (세트별 상대 비교). 칸에 마우스를 올리면 실제 값이 보입니다.")

        def _row_score(row):
            valid = row.dropna()
            if valid.empty or valid.max() == valid.min():
                return row * 0 + 0.5
            norm = (row - valid.min()) / (valid.max() - valid.min())
            return norm if higher_is_better else (1 - norm)

        z_values = pivot.apply(_row_score, axis=1).values
        zmin, zmax = 0, 1
        colorscale = "RdYlGn"

    fig_heat = go.Figure(
        data=go.Heatmap(
            z=z_values,
            x=[str(c) for c in pivot.columns],
            y=pivot.index,
            customdata=hover_text.values,
            hovertemplate="%{y}<br>%{x}<br>%{customdata}<extra></extra>",
            colorscale=colorscale,
            zmin=zmin,
            zmax=zmax,
            showscale=is_roas,
            xgap=1,
            ygap=2,
        )
    )
    fig_heat.update_layout(
        height=max(220, 40 * len(pivot.index)),
        margin=dict(t=10, b=10, l=10, r=10),
        xaxis=dict(tickfont=dict(size=8), tickangle=0, nticks=len(pivot.columns)),
        yaxis=dict(tickfont=dict(size=11)),
    )
    st.plotly_chart(fig_heat, use_container_width=True)

st.divider()

adset_period = period_df[period_df["adset_id"] == selected_adset_id].sort_values("period_start")

if adset_period.empty or adset_period[metric_col].dropna().empty:
    st.info("이 세트는 선택한 지표에 대한 데이터가 없습니다.")
else:
    values = adset_period[metric_col]
    this_value = values.iloc[-1]
    prev_value = values.iloc[-2] if len(values) >= 2 else None
    best_idx = values.idxmin() if not higher_is_better else values.idxmax()
    best_value = values.loc[best_idx]
    best_period = adset_period.loc[best_idx, "period_start"]
    this_spend = adset_period["spend"].iloc[-1]

    if prev_value is not None and prev_value == prev_value and prev_value != 0:
        change_pct = (this_value - prev_value) / abs(prev_value) * 100
        improved = (change_pct < 0) if not higher_is_better else (change_pct > 0)
        trend_label = "개선" if improved else "악화"
        trend_arrow = "↓" if change_pct < 0 else "↑"
    else:
        trend_label, trend_arrow, change_pct = "-", "", 0

    is_currency_metric = selected_metric != "ROAS"
    fmt = (lambda v: f"₩{v:,.0f}") if is_currency_metric else (lambda v: f"{v:.2f}")
    prev_unit_label = "전주" if view_mode == "주단위" else "전일"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"이번 {period_unit_label} ({adset_period['period_start'].iloc[-1]})", fmt(this_value))
    c2.metric(f"{prev_unit_label} 대비", f"{trend_arrow} {trend_label}", f"{change_pct:+.1f}%" if trend_label != "-" else None)
    c3.metric("기간 최고 효율", fmt(best_value), f"{best_period}")
    c4.metric(f"이번 {period_unit_label} 광고비", f"₩{this_spend:,.0f}")

    metric_def = METRIC_DEFS[selected_metric]
    period_total_spend = adset_period["spend"].sum()
    period_total_value = adset_period["purchase_value"].sum()
    period_total_purchases = adset_period["purchases"].sum()
    period_avg_metric = (
        adset_period[metric_def["numerator"]].sum() / adset_period[metric_def["denominator"]].sum()
        if adset_period[metric_def["denominator"]].sum()
        else None
    )

    st.markdown(f"**선택 기간 전체 요약** ({adset_period['period_start'].iloc[0]} ~ {adset_period['period_start'].iloc[-1]})")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric(f"기간 평균 {selected_metric}", fmt(period_avg_metric) if period_avg_metric is not None else "-")
    s2.metric("기간 총 광고비", f"₩{period_total_spend:,.0f}")
    s3.metric("기간 총 매출", f"₩{period_total_value:,.0f}")
    s4.metric("기간 총 구매수", f"{period_total_purchases:,.0f}건")

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=[str(p) for p in adset_period["period_start"]],
            y=adset_period[metric_col],
            mode="lines+markers",
            line=dict(color="#E03C3C" if is_currency_metric else "#3C8CE0"),
        )
    )
    fig.update_layout(
        yaxis_title=selected_metric,
        xaxis_title=period_unit_label,
        height=400,
        margin=dict(t=20, b=20),
    )
    if is_currency_metric:
        fig.update_yaxes(autorange="reversed")  # lower cost = visually "better" (higher on chart)
    st.plotly_chart(fig, use_container_width=True)

    if len(values) >= 2:
        first_value = values.iloc[0]
        overall_change = (this_value - first_value) / abs(first_value) * 100 if first_value else 0
        direction = "개선" if (overall_change < 0) == (not higher_is_better) else "악화"
        st.info(
            f"{best_period} {fmt(best_value)}로 기간 내 최고 효율을 기록했습니다. "
            f"초기({adset_period['period_start'].iloc[0]}) {fmt(first_value)} 대비 "
            f"{abs(overall_change):.0f}% {direction}."
        )

st.divider()

# ---------------------------------------------------------------------------
# 주의가 필요한 세트
# ---------------------------------------------------------------------------
st.subheader("⚠️ 주의가 필요한 세트")
st.caption("최근 3주 연속으로 핵심 지표가 계속 나빠지고 있는 세트입니다 (광고비 1만원 미만 세트는 노이즈가 커서 제외).")
warnings = sorted(detect_declining_adsets(weekly, lookback_weeks=3), key=lambda w: w["adset_name"])
if not warnings:
    st.success("현재 3주 연속 악화 추세를 보이는 세트가 없습니다.")
else:
    for w in warnings:
        bad_list = ", ".join(f"{m}" for m, _, _ in w["bad_metrics"])
        st.warning(f"**{w['adset_name']}** — 악화 지표: {bad_list} (최근 {len(w['weeks'])}주 연속)")

st.divider()

# ---------------------------------------------------------------------------
# 브랜드 전체 ROAS (외부몰 포함 추정)
# ---------------------------------------------------------------------------
brand = brand_total_roas(start_str, end_str)

st.subheader("브랜드 전체 ROAS (자사몰 + 외부몰 추정)")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("총 광고비", f"{brand['total_spend']:,.0f}원")
m2.metric("자사몰 매출", f"{brand['own_store_value']:,.0f}원")
m3.metric("쿠팡 매출", f"{brand['coupang_value']:,.0f}원")
m4.metric("스마트스토어 매출", f"{brand['naver_value']:,.0f}원")
m5.metric("브랜드 종합 ROAS(추정)", f"{brand['brand_total_roas']:.2f}" if brand['brand_total_roas'] else "-")

accounts = spend_by_account(df)
if not accounts.empty:
    st.caption("계정별 광고비: " + " · ".join(
        f"{r['ad_account_name']} {r['spend']:,.0f}원" for _, r in accounts.iterrows()
    ))

st.divider()

st.subheader("광고세트별 데일리 ROAS 추이")
daily = daily_roas_by_adset(df)
adset_options = daily[["adset_id", "adset_name"]].drop_duplicates().sort_values("adset_name", ascending=True)
adset_options["label"] = adset_options["adset_name"] + " (" + adset_options["adset_id"].str[-6:] + ")"
selected_labels = st.multiselect(
    "광고세트 선택 (선택 안 하면 전체 표시)",
    options=adset_options["label"].tolist(),
)
if selected_labels:
    selected_ids = adset_options[adset_options["label"].isin(selected_labels)]["adset_id"].tolist()
    daily_view = daily[daily["adset_id"].isin(selected_ids)]
else:
    daily_view = daily

import plotly.express as px

fig_daily = px.line(
    daily_view,
    x="date",
    y="roas",
    color="adset_name",
    markers=True,
    labels={"roas": "ROAS", "date": "날짜", "adset_name": "광고세트"},
)
st.plotly_chart(fig_daily, use_container_width=True)

st.divider()

st.subheader("광고세트별 1/3/5일 변동률")
changes = latest_change_summary(daily)
display_changes = changes[
    ["adset_name", "spend", "roas", "roas_change_1d", "roas_change_3d", "roas_change_5d"]
].copy()
display_changes.columns = ["광고세트", "최근 광고비", "최근 ROAS", "1일 변동", "3일 변동", "5일 변동"]
for col in ["1일 변동", "3일 변동", "5일 변동"]:
    display_changes[col] = display_changes[col].apply(lambda x: f"{x*100:+.1f}%" if pd.notna(x) else "-")
display_changes["최근 광고비"] = display_changes["최근 광고비"].apply(lambda x: f"{x:,.0f}원")
display_changes["최근 ROAS"] = display_changes["최근 ROAS"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "-")
st.dataframe(display_changes.sort_values("광고세트", ascending=True), use_container_width=True, hide_index=True)

st.divider()

st.subheader("🔍 외부몰(쿠팡/스마트스토어) 구매 유출 추정")
st.caption(
    "장바구니 담기 CPA가 가장 낮은(저렴하게 장바구니를 채우는) 세트를 1순위 의심 세트로 봅니다 — "
    "장바구니까지는 잘 가는데 자사몰 구매(CVR)로 이어지지 않으면, 중장년층이 구매 퍼널에서 이탈해 "
    "외부몰(쿠팡/스마트스토어)에서 구매를 완료했을 가능성이 있다는 '추론'입니다 (정량적 증명은 아님)."
)
leak = external_leakage_inference(df)
display_leak = leak[
    ["adset_name", "add_to_cart_cpa", "cart_to_purchase_rate", "roas", "leakage_grade"]
].copy()
display_leak.columns = ["광고세트", "장바구니 CPA", "장바구니→구매 전환율", "자사몰 ROAS", "외부몰 유출 추정 등급"]
display_leak["장바구니 CPA"] = display_leak["장바구니 CPA"].apply(lambda x: f"{x:,.0f}원" if pd.notna(x) else "-")
display_leak["장바구니→구매 전환율"] = display_leak["장바구니→구매 전환율"].apply(lambda x: f"{x*100:.1f}%" if pd.notna(x) else "-")
display_leak["자사몰 ROAS"] = display_leak["자사몰 ROAS"].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "-")
st.dataframe(display_leak.sort_values("광고세트", ascending=True), use_container_width=True, hide_index=True)
