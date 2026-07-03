"""GitHub Actions에서 5분마다 실행되는 주문 알림 스크립트.

로컬 watcher(realtime_order_watcher.py)와 달리 루프 없이 1회 실행 후 종료.
상태(알림 보낸 주문 ID + 오늘 누적금액)는 data/notified_cloud.json에 저장하고,
GitHub Actions cache로 run 간에 유지한다.

Cafe24 리프레시 토큰 갱신은 .env 대신 GitHub Repository Variables API를 통해
저장한다 (시크릿은 프로그래매틱 수정 불가, 변수는 가능).
"""

import json
import os
import time
import traceback
from datetime import date
from pathlib import Path

import requests as http

# ──────────────────────────────────────────────────────────────────
# config.update_env_value / get_env_value 패치
# GitHub Actions 환경에서는 .env 파일이 없으므로:
#   - update_env_value → GitHub Variables REST API로 저장
#   - get_env_value    → os.environ에서 읽기 (secrets/vars로 주입됨)
# ──────────────────────────────────────────────────────────────────
import config


def _update_github_variable(name: str, value: str):
    # GITHUB_TOKEN은 Variables API 수정 권한이 없어 PAT를 별도 시크릿(GH_PAT)으로 사용
    gh_token = os.environ.get("GH_PAT", "") or os.environ.get("GITHUB_TOKEN", "")
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not gh_token or not gh_repo:
        print(f"[WARN] GitHub variable 업데이트 불가 ({name}): GITHUB_TOKEN/GITHUB_REPOSITORY 미설정")
        return
    resp = http.patch(
        f"https://api.github.com/repos/{gh_repo}/actions/variables/{name}",
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"name": name, "value": value},
        timeout=10,
    )
    if resp.ok:
        print(f"[INFO] GitHub variable '{name}' 갱신 완료")
    else:
        print(f"[WARN] GitHub variable '{name}' 갱신 실패: {resp.status_code} {resp.text[:200]}")


def _patched_update_env(key: str, value: str):
    _update_github_variable(key, value)
    # 같은 프로세스 안에서 이후 호출도 새 토큰을 쓰도록 in-memory 갱신
    setattr(config, key, value)
    os.environ[key] = value


def _patched_get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


config.update_env_value = _patched_update_env
config.get_env_value = _patched_get_env

# ──────────────────────────────────────────────────────────────────
# 상태 관리 (data/notified_cloud.json)
# ──────────────────────────────────────────────────────────────────
STATE_PATH = Path("data/notified_cloud.json")


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ──────────────────────────────────────────────────────────────────
# Discord 알림
# ──────────────────────────────────────────────────────────────────
EMOJI = {"cafe24": "🛍️", "naver": "🟢"}
LABEL = {"cafe24": "자사몰(카페24)", "naver": "스마트스토어"}
COLOR = {"cafe24": 0x1E88E5, "naver": 0x43A047}
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL_ORDERS", "")


def send_alert(platform: str, order_id: str, amount: float, cumulative: float, detail: str = ""):
    if not WEBHOOK_URL:
        print(f"[WARN] DISCORD_WEBHOOK_URL_ORDERS 미설정")
        return
    embed = {
        "title": f"{EMOJI[platform]} 새 주문 — {LABEL[platform]}",
        "color": COLOR[platform],
        "fields": [
            {"name": "주문번호", "value": str(order_id), "inline": True},
            {"name": "금액", "value": f"{amount:,.0f}원", "inline": True},
            {"name": "오늘 누적 결제금액", "value": f"{cumulative:,.0f}원", "inline": True},
        ],
    }
    if detail:
        embed["fields"].append({"name": "상세", "value": detail, "inline": False})
    # Discord 웹훅 429 재시도
    delay = 2.0
    for attempt in range(4):
        resp = http.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=30)
        if resp.status_code == 429:
            time.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return
    resp.raise_for_status()


# ──────────────────────────────────────────────────────────────────
# 주문 체크 로직
# ──────────────────────────────────────────────────────────────────
def check_cafe24(today: str, seen: dict, cumulative: float, is_bootstrap: bool) -> tuple[int, float]:
    from collectors.cafe24 import _is_target_item, fetch_orders

    orders = fetch_orders(today)
    new_count = 0
    for o in orders:
        items = o.get("items", [])
        target_items = [i for i in items if _is_target_item(i)]
        if not target_items or len(target_items) != len(items):
            continue  # 비대상 상품 또는 혼합 주문 제외

        oid = o["order_id"]
        key = f"cafe24:{oid}"
        if key in seen:
            continue

        amount = float(o.get("actual_order_amount", {}).get("payment_amount", 0))
        if is_bootstrap:
            # 기존 주문 → 알림 없이 기록만
            seen[key] = amount
            cumulative += amount
        else:
            cumulative += amount
            names = ", ".join(i.get("product_name", "") for i in target_items)
            send_alert("cafe24", oid, amount, cumulative, detail=names)
            seen[key] = amount
            new_count += 1

    return new_count, cumulative


def check_naver(today: str, seen: dict, cumulative: float, is_bootstrap: bool) -> tuple[int, float]:
    from collectors.naver import _is_target_product_order, fetch_product_orders

    orders = fetch_product_orders(today)
    new_count = 0
    for o in orders:
        po = o.get("content", {}).get("productOrder", {})
        if not _is_target_product_order(po):
            continue

        oid = po.get("productOrderId", "")
        key = f"naver:{oid}"
        if key in seen:
            continue

        amount = float(po.get("totalPaymentAmount", 0)) + float(po.get("deliveryFeeAmount", 0))
        if is_bootstrap:
            seen[key] = amount
            cumulative += amount
        else:
            cumulative += amount
            send_alert("naver", oid, amount, cumulative, detail=po.get("productName", ""))
            seen[key] = amount
            new_count += 1

    return new_count, cumulative


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────
def main():
    today = date.today().isoformat()

    state = load_state()
    # state 구조: {"date": "YYYY-MM-DD", "seen": {...}, "cumulative": float}
    # 날짜가 바뀌거나 seen이 비어있으면 bootstrap (기존 주문 무음 처리)
    if state.get("date") != today:
        state = {"date": today, "seen": {}, "cumulative": 0.0}
        is_bootstrap = True
        print(f"[INFO] 새 날짜({today}) 감지 — 상태 초기화 및 부트스트랩 실행")
    elif not state.get("seen"):
        # 이전 run이 실패해서 seen이 빈 채로 저장된 경우 — 재부트스트랩
        is_bootstrap = True
        print(f"[INFO] seen이 비어있음 — 부트스트랩 재실행")
    else:
        is_bootstrap = False

    seen: dict = state.get("seen", {})
    cumulative: float = float(state.get("cumulative", 0.0))

    print(f"[INFO] 날짜={today}, 부트스트랩={is_bootstrap}, 기존 seen={len(seen)}건, 누적={cumulative:,.0f}원")

    # Cafe24
    try:
        new_c24, cumulative = check_cafe24(today, seen, cumulative, is_bootstrap)
        print(f"[INFO] Cafe24: 신규 알림 {new_c24}건")
    except Exception:
        print(f"[ERROR] Cafe24 체크 실패:\n{traceback.format_exc()}")

    # 네이버는 IP 화이트리스트 제한으로 로컬 PC에서 별도 실행

    state["seen"] = seen
    state["cumulative"] = cumulative
    save_state(state)
    print(f"[INFO] 완료 — seen 총 {len(seen)}건, 누적={cumulative:,.0f}원")


if __name__ == "__main__":
    main()
