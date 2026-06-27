from datetime import date, timedelta

from collect_cafe24_range import main as collect_cafe24
from collect_coupang_range import main as collect_coupang
from collect_meta_range import main as collect_meta
from collect_naver_range import main as collect_naver
from notify.discord import send_account_report, send_daily_report


def run(report_date: str):
    print(f"=== 일일 파이프라인 시작: {report_date} ===")

    print("[1/5] 메타 데이터 수집")
    collect_meta(report_date, report_date)

    print("[2/5] 쿠팡 데이터 수집")
    collect_coupang(report_date, report_date)

    print("[3/5] 네이버 데이터 수집")
    collect_naver(report_date, report_date)

    print("[4/5] 카페24 데이터 수집")
    collect_cafe24(report_date, report_date)

    print("[5/5] 디스코드 리포트 발송")
    send_daily_report(report_date)
    send_account_report(report_date)

    print(f"=== 일일 파이프라인 완료: {report_date} ===")


if __name__ == "__main__":
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    run(yesterday)
