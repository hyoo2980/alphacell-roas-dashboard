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
from datetime import datetime, timezone, timedelta
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
    gh_token = os.environ.get("GH_PAT", "") or os.environ.get("GITHUB_TOKEN", "")
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not gh_token or not gh_repo:
        print(f"[WARN] GitHub variable 업데이트 불가 ({name}): GH_PAT/GITHUB_REPOSITORY 미설정")
        return
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base_url = f"https://api.github.com/repos/{gh_repo}/actions/variables"
    resp = http.patch(f"{base_url}/{name}", headers=headers, json={"name": name, "value": value}, timeout=10)
    if resp.status_code == 404:
        # 변수가 없으면 새로 생성
        resp = http.post(base_url, headers=headers, json={"name": name, "value": value}, timeout=10)
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
# Cafe24 access token 캐시 주입
# GitHub Variable에 저장된 access token이 아직 유효하면 재사용 →
# refresh token rotation을 2분마다가 아닌 2시간마다로 줄임
# ──────────────────────────────────────────────────────────────────
def _inject_cached_access_token():
    cached_token = os.environ.get("CAFE24_ACCESS_TOKEN", "")
    expires_at_str = os.environ.get("CAFE24_ACCESS_TOKEN_EXPIRES_AT", "")
    if not cached_token or not expires_at_str:
        return
    try:
        # Cafe24 expires_at 형식: "2026-07-14T12:34:56.000" (KST)
        expires_at = datetime.fromisoformat(expires_at_str.rstrip("0").rstrip("."))
        expires_at = expires_at.replace(tzinfo=timezone(timedelta(hours=9)))
        if expires_at > datetime.now(timezone(timedelta(hours=9))) + timedelta(minutes=5):
            import collectors.cafe24 as _c24
            _c24._token_cache["access_token"] = cached_token
            print(f"[INFO] 캐시된 access token 재사용 (만료: {expires_at_str})")
    except Exception as e:
        print(f"[INFO] access token 캐시 로드 실패 ({e}) — 재발급 진행")

_inject_cached_access_token()

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
_KST = timezone(timedelta(hours=9))


def main():
    today = datetime.now(_KST).date().isoformat()

    state = load_state()
    # state 구조: {"date": "YYYY-MM-DD", "seen": {...}, "cumulative": float}
    # 날짜가 바뀌거나 seen이 비어있으면 bootstrap (기존 주문 무음 처리)
    if state.get("date") != today:
        state = {"date": today, "seen": {}, "cumulative": 0.0}
        is_bootstrap = True
        print(f"[INFO] 새 날짜({today}) 감지 — 상태 초기화 및 부트스트랩 실행")
    elif "seen" not in state:
        # seen 키 자체가 없는 경우만 재부트스트랩 (빈 딕셔너리는 정상 상태)
        is_bootstrap = True
        print(f"[INFO] seen 키 없음 — 부트스트랩 재실행")
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
