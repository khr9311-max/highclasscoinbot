#!/usr/bin/env python3
"""
코인봇 워치독.

systemd 타이머로 주기 실행되며 아래를 점검하고 이상 시 텔레그램으로 알린다.

  1) 서비스가 active 인가
  2) 마지막 HEARTBEAT 로그가 너무 오래되지 않았나 (프로세스는 살았는데 멈춘 경우)
  3) 디스크 여유가 충분한가
  4) 재시작이 비정상적으로 잦지 않은가

의존성은 파이썬 표준 라이브러리뿐이다. 봇의 venv 가 깨져도 워치독은 돌아야
하므로 시스템 python3 로 실행한다.

  sudo python3 /opt/coinbot/deploy/watchdog.py
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SERVICE = "coinbot"
ENV_FILE = "/etc/coinbot/coinbot.env"
STATE_FILE = "/var/lib/coinbot/watchdog_state.json"

HEARTBEAT_MAX_AGE = 300        # 하트비트가 이 시간(초) 넘게 없으면 이상
DISK_MIN_FREE_PCT = 10         # 여유 공간이 이 % 미만이면 경고
RESTART_WINDOW = 3600          # 최근 1시간
RESTART_MAX = 5                # 그 사이 재시작이 이 횟수 넘으면 경고
ALERT_COOLDOWN = 1800          # 같은 종류 알림 재발송 최소 간격(초)


# ---------------------------------------------------------------------------
def load_env(path=ENV_FILE):
    """
    systemd EnvironmentFile 포맷을 읽는다 (KEY="value").

    errors="replace" 인 이유: 이 파일이 다른 OS 에서 생성되면 주석에 UTF-8 이
    아닌 바이트가 섞일 수 있다. 값 자체는 ASCII 이므로 깨진 주석 때문에
    워치독 전체가 죽으면 안 된다.
    """
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
        print("텔레그램 미설정 - 알림 생략", file=sys.stderr)
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


def run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"상태 저장 실패: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
def check_service():
    active = run(["systemctl", "is-active", SERVICE]).strip()
    if active != "active":
        detail = run(["systemctl", "status", SERVICE, "--no-pager", "-n", "5"])
        return False, f"서비스 상태가 '{active}' 입니다.\n<pre>{detail[-400:]}</pre>"
    return True, ""


def check_heartbeat():
    """journald 에서 마지막 HEARTBEAT 줄의 나이를 잰다."""
    out = run(["journalctl", "-u", SERVICE, "--since", "30 min ago",
               "-o", "short-iso", "--no-pager"])
    lines = [l for l in out.splitlines() if "HEARTBEAT" in l]
    if not lines:
        return False, "최근 30분간 HEARTBEAT 로그가 없습니다.", None

    # systemd 버전에 따라 오프셋이 '+0000' 또는 '+00:00' 으로 나온다.
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+\-]\d{2}:?\d{2}|Z))", lines[-1])
    if not m:
        # 타임스탬프를 못 읽으면 판정을 포기하되, 그 사실을 드러낸다.
        return False, f"HEARTBEAT 시각 파싱 실패: {lines[-1][:80]}", lines[-1]

    raw = m.group(1).replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        try:
            ts = datetime.strptime(raw.replace(":", ""), "%Y-%m-%dT%H%M%S%z")
        except ValueError:
            return False, f"HEARTBEAT 시각 해석 실패: {raw}", lines[-1]

    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age > HEARTBEAT_MAX_AGE:
        return False, f"HEARTBEAT 가 {age/60:.1f}분째 없습니다 (임계 {HEARTBEAT_MAX_AGE/60:.0f}분).", lines[-1]
    return True, "", lines[-1]


def disk_free_pct() -> int:
    out = run(["df", "--output=pcent", "/"])
    try:
        return 100 - int(out.splitlines()[1].strip().rstrip("%"))
    except Exception:
        return 100


def check_disk():
    free = disk_free_pct()
    if free < DISK_MIN_FREE_PCT:
        return False, f"디스크 여유 {free}% - 임계 {DISK_MIN_FREE_PCT}% 미만."
    return True, ""


def push_cloudwatch(service_up: int, heartbeat_age: float, disk_free_pct: int):
    """
    CloudWatch 커스텀 지표 전송.

    로컬 워치독은 '인스턴스 자체가 죽은 경우'를 알릴 수 없다(알림을 보낼
    주체가 같이 죽으므로). 지표를 밖으로 밀어두면 CloudWatch 쪽에서
    '데이터 없음 = 이상'으로 잡을 수 있다.

    IAM 인스턴스 역할이 없으면 조용히 건너뛴다. 이 기능이 없다고 해서
    워치독 본체가 실패해서는 안 된다.
    """
    try:
        import boto3
    except ImportError:
        return None
    try:
        cw = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "ap-northeast-2"))
        cw.put_metric_data(
            Namespace="CoinBot",
            MetricData=[
                {"MetricName": "ServiceUp", "Value": service_up, "Unit": "None"},
                {"MetricName": "HeartbeatAge", "Value": float(heartbeat_age), "Unit": "Seconds"},
                {"MetricName": "DiskFreePercent", "Value": float(disk_free_pct), "Unit": "Percent"},
            ],
        )
        return True
    except Exception as e:
        print(f"CloudWatch 지표 전송 생략: {type(e).__name__}", file=sys.stderr)
        return False


def check_restarts():
    out = run(["journalctl", "-u", SERVICE, "--since", f"-{RESTART_WINDOW}s",
               "--no-pager", "-g", "Started coinbot.service"])
    n = sum(1 for l in out.splitlines() if "Started" in l)
    if n > RESTART_MAX:
        return False, f"최근 1시간 재시작 {n}회 (임계 {RESTART_MAX}회). 크래시 루프 가능성."
    return True, ""


# ---------------------------------------------------------------------------
def main():
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat = env.get("TELEGRAM_CHAT_ID")
    state = load_state()
    now = time.time()

    svc_ok, svc_msg = check_service()
    disk_ok, disk_msg = check_disk()
    restart_ok, restart_msg = check_restarts()
    hb_ok, hb_msg, hb_line = check_heartbeat()

    checks = [
        ("service", svc_ok, svc_msg),
        ("disk", disk_ok, disk_msg),
        ("restarts", restart_ok, restart_msg),
        ("heartbeat", hb_ok, hb_msg),
    ]
    problems = [(name, msg) for name, ok, msg in checks if not ok]

    # 인스턴스가 통째로 죽으면 이 지표가 끊기고, CloudWatch 가 그걸 잡는다.
    push_cloudwatch(
        service_up=1 if svc_ok else 0,
        heartbeat_age=0.0 if hb_ok else float(HEARTBEAT_MAX_AGE * 2),
        disk_free_pct=disk_free_pct(),
    )

    for name, msg in problems:
        last = state.get(f"alert_{name}", 0)
        if now - last < ALERT_COOLDOWN:
            print(f"[{name}] 이상이지만 쿨다운 중 - 알림 생략")
            continue
        text = f"<b>🚨 코인봇 이상 감지</b>\n<b>[{name}]</b> {msg}"
        if telegram(token, chat, text):
            state[f"alert_{name}"] = now
        print(f"[{name}] 알림 발송: {msg}")

    if not problems:
        # 이상이 해소되면 1회 복구 알림
        had = [k for k in list(state) if k.startswith("alert_")]
        if had:
            telegram(token, chat, "<b>✅ 코인봇 정상 복구</b>\n모든 점검 항목이 정상입니다.")
            for k in had:
                state.pop(k, None)
        print("정상:", hb_line[-120:] if hb_line else "(하트비트 라인 없음)")

    state["last_run"] = now
    save_state(state)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
