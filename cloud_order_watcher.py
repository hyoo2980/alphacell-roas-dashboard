"""GitHub Actions에서 2분마다 실행되는 주문 알림 스크립트.

로컬 watcher(realtime_order_watcher.py)와 달리 루프 없이 1회 실행 후 종료.
상태(알림 보낸 주문 ID + 오늘 누적금액)는 GitHub Variable(ORDER_STATE)에 저장.
GitHub Actions cache 대신 Variable을 사용해 캐시 미스로 인한 중복 bootstrap을 방지.

Cafe24 리프레시 토큰 갱신은 .env 대신 GitHub Repository Variables API를 통해
저장한다 (시크릿은 프로그래매틱 수정 불가, 변수는 가능).
"""

import json
import os
import time
import traceback
from datetime import datetime, timezone, timedelta

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
    try:
        resp = http.patch(f"{base_url}/{name}", headers=headers, json={"name": name, "value": value}, timeout=10)
        if resp.status_code == 404:
            resp = http.post(base_url, headers=headers, json={"name": name, "value": value}, timeout=10)
        if resp.ok:
            print(f"[INFO] GitHub variable '{name}' 갱신 완료")
        else:
            print(f"[WARN] GitHub variable '{name}' 갱신 실패: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[WARN] GitHub variable '{name}' 갱신 중 네트워크 오류 (무시): {e}")


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
# 상태 관리 — GitHub Variable(ORDER_STATE) 기반
# GitHub Actions cache는 캐시 미스 시 오래된 상태를 복원해 bootstrap이 중복 실행되는
# 문제가 있으므로 완전히 영속적인 Variable로 대체.
# ──────────────────────────────────────────────────────────────────
_GH_VAR_NAME = "ORDER_STATE"


def _get_github_variable(name: str) -> str:
    gh_token = os.environ.get("GH_PAT", "") or os.environ.get("GITHUB_TOKEN", "")
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not gh_token or not gh_repo:
        return ""
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = http.get(
        f"https://api.github.com/repos/{gh_repo}/actions/variables/{name}",
        headers=headers, timeout=10,
    )
    if resp.ok:
        return resp.json().get("value", "")
    return ""


def load_state() -> dict:
    raw = _get_github_variable(_GH_VAR_NAME)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


def save_state(state: dict):
    _update_github_variable(_GH_VAR_NAME, json.dumps(state, ensure_ascii=False))


# ──────────────────────────────────────────────────────────────────
# Discord 알림
# ──────────────────────────────────────────────────────────────────
EMOJI = {"cafe24": "🛍️", "naver": "🟢"}
LABEL = {"cafe24": "자사몰(카페24)", "naver": "스마트스토어"}
COLOR = {"cafe24": 0x1E88E5, "naver": 0x43A047}
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL_ORDERS", "")


def send_alert(platform: str, order_id: str, amount: float, cumulative: float, detail: str = ""):
    """네이버(로컬 실행)용 — Cafe24 상품군 알림은 send_group_alert를 쓴다."""
    _post_embed(
        WEBHOOK_URL,
        "DISCORD_WEBHOOK_URL_ORDERS",
        f"{EMOJI[platform]} 새 주문 — {LABEL[platform]}",
        COLOR[platform],
        order_id,
        amount,
        cumulative,
        detail,
    )


def _post_embed(webhook: str, webhook_env: str, title: str, color: int,
                order_id: str, amount: float, cumulative: float, detail: str = ""):
    if not webhook:
        print(f"[WARN] {webhook_env} 미설정 — 알림 건너뜀")
        return
    embed = {
        "title": title,
        "color": color,
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
        resp = http.post(webhook, json={"embeds": [embed]}, timeout=30)
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
# Cafe24 상품군별 알림 채널.
# seen_prefix는 상태(ORDER_STATE)의 키 접두사 — 기존 상태와 호환되어야 하므로
# 올나잇의 "cafe24"는 절대 바꾸지 말 것.
def _cafe24_groups() -> list[dict]:
    from collectors.cafe24 import _is_hyuldang_item, _is_target_item

    return [
        {
            "key": "olnight",
            "seen_prefix": "cafe24",
            "title": "🛍️ 새 주문 — 알파셀 올나잇 세이프",
            "color": 0x1E88E5,
            "webhook_env": "DISCORD_WEBHOOK_URL_ORDERS",
            "matches": _is_target_item,
        },
        {
            "key": "hyuldang",
            "seen_prefix": "cafe24hd",
            "title": "🩸 새 주문 — 알파셀 혈당 세이프",
            "color": 0xFB8C00,
            "webhook_env": "DISCORD_WEBHOOK_URL_ORDERS_HYULDANG",
            "matches": _is_hyuldang_item,
        },
    ]


def check_cafe24(today: str, seen: dict, cumulatives: dict, bootstrapped: list) -> dict:
    """상품군별로 주문을 분류해 각자의 Discord 채널로 알린다.
    주문 목록은 한 번만 조회하고 그룹별로 필터링한다.
    seen / cumulatives / bootstrapped는 제자리에서 갱신된다."""
    from collectors.cafe24 import fetch_orders

    orders = fetch_orders(today)
    new_counts = {}

    for group in _cafe24_groups():
        gkey = group["key"]
        is_bootstrap = gkey not in bootstrapped
        webhook = os.environ.get(group["webhook_env"], "")
        cumulative = float(cumulatives.get(gkey, 0.0))
        new_count = 0

        for o in orders:
            items = o.get("items", [])
            target_items = [i for i in items if group["matches"](i)]
            if not target_items or len(target_items) != len(items):
                continue  # 비대상 상품 또는 혼합 주문 제외

            oid = o["order_id"]
            key = f"{group['seen_prefix']}:{oid}"
            if key in seen:
                continue

            amount = float(o.get("actual_order_amount", {}).get("payment_amount", 0))
            cumulative += amount
            if not is_bootstrap:
                names = ", ".join(i.get("product_name", "") for i in target_items)
                _post_embed(webhook, group["webhook_env"], group["title"], group["color"],
                            oid, amount, cumulative, detail=names)
                new_count += 1
            # 부트스트랩이면 기존 주문 → 알림 없이 기록만
            seen[key] = amount

        cumulatives[gkey] = cumulative
        if is_bootstrap:
            bootstrapped.append(gkey)
            print(f"[INFO] {gkey}: 부트스트랩 완료 — 기존 주문 무음 처리, 누적={cumulative:,.0f}원")
        new_counts[gkey] = new_count

    return new_counts


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
    # state 구조: {"date": "YYYY-MM-DD", "seen": {...},
    #             "cumulatives": {상품군: float}, "bootstrapped": [상품군, ...]}
    # bootstrapped에 없는 상품군은 첫 실행 때 기존 주문을 무음 흡수한다.
    # 상품군을 새로 추가해도 그 군만 부트스트랩되고 나머지는 정상 동작.
    yesterday = (datetime.now(_KST).date() - timedelta(days=1)).isoformat()
    prev_date = state.get("date")

    if prev_date == yesterday and "seen" in state:
        # 정상적인 날짜 롤오버. 어제까지 상태가 멀쩡했으므로 새 날에는 밀린 주문이
        # 존재할 수 없고, 지금 조회되는 주문은 전부 진짜 신규다. 여기서 부트스트랩하면
        # 자정 직후(00:00~00:02)에 들어온 주문이 매일 무음 처리되어 사라진다.
        state = {"date": today}
        seen, cumulatives = {}, {}
        bootstrapped = [g["key"] for g in _cafe24_groups()]
        print(f"[INFO] 날짜 롤오버({yesterday}→{today}) — 상태만 초기화, 알림은 즉시 발송")
    elif prev_date != today or "seen" not in state:
        # 상태 유실 또는 장기 중단(하루 이상 공백). 그날 이미 쌓인 주문이 한꺼번에
        # 쏟아지는 것을 막기 위해 무음 부트스트랩한다.
        state = {"date": today}
        seen, cumulatives, bootstrapped = {}, {}, []
        print(f"[INFO] 상태 유실/장기 중단 감지(이전 날짜={prev_date}) — 부트스트랩 실행")
    else:
        seen = state["seen"]
        # 구 상태 마이그레이션: cumulative(단일) → cumulatives(상품군별),
        # bootstrapped 없으면 올나잇은 이미 운영 중이었으므로 완료로 간주
        cumulatives = state.get("cumulatives") or {"olnight": float(state.get("cumulative", 0.0))}
        bootstrapped = state.get("bootstrapped") or ["olnight"]

    print(f"[INFO] 날짜={today}, 기존 seen={len(seen)}건, "
          f"누적={ {k: f'{v:,.0f}원' for k, v in cumulatives.items()} }, 부트스트랩완료={bootstrapped}")

    # Cafe24
    try:
        new_counts = check_cafe24(today, seen, cumulatives, bootstrapped)
        for gkey, n in new_counts.items():
            print(f"[INFO] Cafe24/{gkey}: 신규 알림 {n}건")
    except Exception:
        print(f"[ERROR] Cafe24 체크 실패:\n{traceback.format_exc()}")

    # 네이버는 IP 화이트리스트 제한으로 로컬 PC에서 별도 실행

    state["seen"] = seen
    state["cumulatives"] = cumulatives
    state["bootstrapped"] = bootstrapped
    state.pop("cumulative", None)  # 구 형식 잔재 정리
    save_state(state)
    print(f"[INFO] 완료 — seen 총 {len(seen)}건, "
          f"누적={ {k: f'{v:,.0f}원' for k, v in cumulatives.items()} }")


if __name__ == "__main__":
    main()
