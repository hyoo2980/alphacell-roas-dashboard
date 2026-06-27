import subprocess
from datetime import date, timedelta
from pathlib import Path

from collect_cafe24_range import main as collect_cafe24
from collect_coupang_range import main as collect_coupang
from collect_meta_range import main as collect_meta
from collect_naver_range import main as collect_naver
from notify.discord import send_account_report, send_daily_report

ROOT_DIR = Path(__file__).resolve().parent


def push_data_to_github(report_date: str):
    """Commits the updated SQLite DB and pushes it so Streamlit Community Cloud
    (which reads data/roas.db straight out of the repo) auto-redeploys with
    today's data. No-ops cleanly if there's nothing new to commit."""
    git = lambda args: subprocess.run(
        args, cwd=ROOT_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )

    git(["git", "add", "data/roas.db"])
    diff = git(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        print("DB 변경 없음 - GitHub 푸시 스킵")
        return

    commit = git(["git", "commit", "-m", f"데이터 자동 갱신: {report_date}"])
    if commit.returncode != 0:
        print("git commit 실패:", commit.stdout, commit.stderr)
        return

    push = git(["git", "push"])
    if push.returncode != 0:
        print("git push 실패:", push.stdout, push.stderr)
    else:
        print("GitHub 푸시 완료 - Streamlit Cloud 자동 재배포됩니다")


def run(report_date: str):
    print(f"=== 일일 파이프라인 시작: {report_date} ===")

    print("[1/6] 메타 데이터 수집")
    collect_meta(report_date, report_date)

    print("[2/6] 쿠팡 데이터 수집")
    collect_coupang(report_date, report_date)

    print("[3/6] 네이버 데이터 수집")
    collect_naver(report_date, report_date)

    print("[4/6] 카페24 데이터 수집")
    collect_cafe24(report_date, report_date)

    print("[5/6] 디스코드 리포트 발송")
    send_daily_report(report_date)
    send_account_report(report_date)

    print("[6/6] GitHub에 데이터 푸시 (클라우드 대시보드 갱신)")
    push_data_to_github(report_date)

    print(f"=== 일일 파이프라인 완료: {report_date} ===")


if __name__ == "__main__":
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    run(yesterday)
