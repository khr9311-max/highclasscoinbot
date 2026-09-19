#!/usr/bin/env python3
"""
메타 모델 학습 진행률을 주기적으로 텔레그램으로 알린다.

기존 watchdog.py 와 성격이 다르다 - 저건 "봇이 죽었나"를 감시하는 안전장치라
시스템 파이썬만 쓰고, 이건 "학습까지 얼마나 남았나"를 보고하는 정보성
스크립트라 코인봇 venv(pandas/lightgbm 등)를 그대로 쓴다. venv 가 깨지면
이 스크립트도 실패하지만, 그건 watchdog 의 서비스 다운 알림으로 이미 잡힌다.

표본 수는 raw BUY/SELL 신호 개수가 아니라 meta_trainer.build_dataset() 이
반환하는 '라벨 확정 표본' 을 쓴다. 신호가 나온 뒤에도 (1) 30분 수직장벽이
지나야 하고 (2) 그 구간 가격 데이터가 있어야 라벨이 확정되므로, raw 신호
수보다 항상 같거나 적다 (실측: raw 156 / 확정 152).

상태는 /var/lib/coinbot/meta_progress_state.json 에 저장해 직전 실행과
비교, 시간당 증가 속도로 ETA 를 낸다.

    python3 /opt/coinbot/deploy/meta_progress.py
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/opt/coinbot")

STATE_DIR = "/var/lib/coinbot"
ENV_FILE = "/etc/coinbot/coinbot.env"
STATE_FILE = os.path.join(STATE_DIR, "meta_progress_state.json")
META_MODEL_PATH = os.path.join(STATE_DIR, "meta_model.pkl")


def load_env(path=ENV_FILE):
    env = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as e:
        print(f"환경파일 읽기 실패: {e}", file=sys.stderr)
    return env


def telegram(token, chat_id, text):
    if not token or not chat_id:
        print("텔레그램 미설정 - 전송 생략", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            return r.status == 200
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}", file=sys.stderr)
        return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"상태 저장 실패: {e}", file=sys.stderr)


def main():
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat = env.get("TELEGRAM_CHAT_ID")
    state = load_state()
    now = time.time()

    # 이미 학습된 모델이 있으면 진행률 알림은 더 이상 의미가 없다.
    # (main.py 가 학습 완료 시 자체적으로 "🧠 메타 모델 학습 완료" 를 보낸다)
    if os.path.exists(META_MODEL_PATH):
        if not state.get("done_notified"):
            telegram(token, chat,
                     "<b>ℹ️ 메타 모델 이미 학습됨</b>\n진행률 알림을 종료합니다.")
            state["done_notified"] = True
            save_state(state)
        print("메타 모델 파일 존재 - 진행률 알림 불필요")
        return 0

    from meta_trainer import MetaTrainer

    mt = MetaTrainer(STATE_DIR, META_MODEL_PATH)
    ds = mt.build_dataset()
    n = 0 if ds is None else len(ds[0])
    target = mt.min_samples

    # 학습 자체는 4종목을 섞은 단일 풀링 모델이라(meta_trainer 는 피처에
    # ticker 를 넣지 않는다) 목표(300)도 종목별이 아니라 합계 기준이다.
    # 그런데 수집은 종목별로 따로 하고(크립토 에이전트/뉴스 검색 모두 개별),
    # 실제로 종목 간 속도 차이가 크므로(BTC > XRP > ETH > SOL) 합계만 보면
    # "절반 왔다" 가 종목별로는 전혀 고르지 않은 상태를 가릴 수 있다.
    # raw BUY/SELL 신호 수(라벨 미확정 포함)로 종목별 현황도 같이 보여준다.
    ticker_counts: dict = {}
    try:
        import collections
        sigs = mt.load_signals()
        c = collections.Counter(
            s.get("ticker", "?") for s in sigs if s.get("action") in ("BUY", "SELL")
        )
        ticker_counts = dict(sorted(c.items(), key=lambda kv: -kv[1]))
    except Exception as e:
        print(f"종목별 집계 실패(무시): {e}", file=sys.stderr)

    prev_n = state.get("n")
    prev_ts = state.get("ts")
    rate_per_hour = None
    eta_hours = None
    if prev_n is not None and prev_ts and now > prev_ts and n > prev_n:
        rate_per_hour = (n - prev_n) / ((now - prev_ts) / 3600.0)
        if rate_per_hour > 0:
            eta_hours = max(0.0, (target - n) / rate_per_hour)

    pct = min(100.0, n / target * 100.0) if target else 0.0
    now_kst = datetime.now(timezone.utc).astimezone().strftime("%m-%d %H:%M")

    lines = [
        "<b>📊 메타 모델 학습 진행률</b>",
        f"{now_kst}",
        f"표본(합계, 4종목 풀링) {n} / {target}건 ({pct:.0f}%)",
    ]
    if rate_per_hour is not None:
        lines.append(f"최근 속도 {rate_per_hour:.1f}건/시간")
    if eta_hours is not None:
        if eta_hours < 1:
            lines.append(f"예상 도달 약 {eta_hours*60:.0f}분 후")
        else:
            lines.append(f"예상 도달 약 {eta_hours:.1f}시간 후")
    elif prev_n is None:
        lines.append("(첫 측정 - 다음 실행부터 속도 계산)")

    if ticker_counts:
        lines.append("")
        lines.append("<b>종목별 진입신호(BUY+SELL, 참고용)</b>")
        lines.append("※ 학습 목표는 종목 구분 없는 합계 기준. 종목별 편차를")
        lines.append("  보려고 별도 표기 - SOL/ETH 는 BTC 대비 표본이 적어")
        lines.append("  합계가 목표에 닿아도 종목별 신뢰도는 다를 수 있음")
        for tk, cnt in ticker_counts.items():
            lines.append(f"  {tk}: {cnt}건")

    if n >= target:
        lines.append("")
        lines.append("목표 도달 - 다음 1시간 주기 점검에서 자동 학습됩니다.")

    telegram(token, chat, "\n".join(lines))
    print("\n".join(lines))

    state["n"] = n
    state["ts"] = now
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
