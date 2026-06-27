import time
from datetime import datetime

from notify.realtime_orders import bootstrap_seen_orders, check_all_new_orders

POLL_INTERVAL_SECONDS = 300


def main():
    print("기존 주문 부트스트랩 중 (알림 발송 없이 기록만)...")
    bootstrap_seen_orders()
    print(f"실시간 주문 감시 시작 (폴링 간격 {POLL_INTERVAL_SECONDS}초)")
    while True:
        try:
            counts = check_all_new_orders()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now}] checked -> {counts}")
        except Exception as e:
            print(f"폴링 중 오류 발생: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
