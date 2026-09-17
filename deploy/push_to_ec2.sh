#!/usr/bin/env bash
# =============================================================================
# 로컬 -> EC2 배포 스크립트
#
#   bash deploy/push_to_ec2.sh <host> <pem경로> [사용자명]
#   예) bash deploy/push_to_ec2.sh 15.165.144.204 ~/.ssh/highclassupbitbot.pem
#
# 하는 일:
#   1) 코드 업로드 (.env / 키파일 / 캐시 제외)
#   2) python venv + 의존성 설치 (torch 는 CPU 휠)
#   3) 시간 동기화(chrony) 확인
#   4) systemd 유닛 설치
#   5) 업비트 API 도달성 + 잔고 확인 (허용 IP 등록 여부 검증)
#
# 실주문 전환은 이 스크립트가 하지 않는다. 검증 후 별도로 수행한다.
# =============================================================================
set -euo pipefail

HOST="${1:?사용법: push_to_ec2.sh <host> <pem> [user]}"
PEM="${2:?pem 경로를 지정하세요}"
USER_NAME="${3:-}"

SSH_OPTS=(-i "$PEM" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)
APP_DIR=/opt/coinbot
STATE_DIR=/var/lib/coinbot

# ---- 사용자명 자동 판별 ----
if [ -z "$USER_NAME" ]; then
  for u in ec2-user ubuntu admin fedora; do
    if ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$u@$HOST" true 2>/dev/null; then
      USER_NAME="$u"; break
    fi
  done
fi
[ -z "$USER_NAME" ] && { echo "!! SSH 접속 실패. 보안 그룹 22번 포트를 확인하세요." >&2; exit 1; }

SSH=(ssh "${SSH_OPTS[@]}" "$USER_NAME@$HOST")
echo "==> 접속 성공: $USER_NAME@$HOST"
"${SSH[@]}" '. /etc/os-release && echo "    OS: $PRETTY_NAME" && echo "    CPU: $(nproc)코어 / MEM: $(free -m | awk "/Mem:/{print \$2}")MB"'

# ---- 1. 코드 업로드 ----
# STATE_DIR 는 여기서 chown 하지 않는다. setup_ec2.sh 가 최초 1회 coinbot:coinbot
# 로 잡아둔 소유권을 재배포마다 SSH 접속 계정으로 덮어써서, coinbot 서비스가
# 새 하위 디렉터리를 못 만드는 사고가 있었다(2026-09-18, depth/ 생성 실패로
# 라이브 크래시루프). APP_DIR 는 업로드를 위해 잠시 SSH 계정 소유로 바꾸고,
# 런타임 설치가 끝나면 아래 3번에서 coinbot 소유로 되돌린다.
echo "==> 코드 업로드"
"${SSH[@]}" "sudo mkdir -p $APP_DIR $STATE_DIR && sudo chown -R $USER_NAME:$USER_NAME $APP_DIR"
tar --exclude='.git' --exclude='.env' --exclude='__pycache__' --exclude='*.pem' \
    --exclude='.tmp.driveupload' --exclude='state' --exclude='*.zip' \
    -czf - . | "${SSH[@]}" "tar -xzf - -C $APP_DIR"
echo "    업로드 완료"

# ---- 2. 런타임 ----
echo "==> 파이썬 런타임 및 의존성 (수 분 소요)"
"${SSH[@]}" bash -s <<REMOTE
set -euo pipefail
cd $APP_DIR

if command -v dnf >/dev/null; then
  sudo dnf -y install -q python3.12 python3.12-pip chrony 2>/dev/null || \
  sudo dnf -y install -q python3 python3-pip chrony
  PY=\$(command -v python3.12 || command -v python3)
else
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3-venv python3-pip chrony
  PY=python3
fi
echo "    python: \$(\$PY --version)"

[ -d .venv ] || \$PY -m venv .venv
./.venv/bin/pip install -q --upgrade pip
# CPU 전용 torch 휠 (GPU 인스턴스가 아니면 설치 용량이 크게 줄어든다)
./.venv/bin/pip install -q --index-url https://download.pytorch.org/whl/cpu torch==2.14.0
./.venv/bin/pip install -q -r requirements.txt
echo "    의존성 설치 완료"

sudo systemctl enable --now chronyd 2>/dev/null || sudo systemctl enable --now chrony 2>/dev/null || true
echo "    시간동기화: \$(chronyc tracking 2>/dev/null | grep -i 'System time' || echo '확인 불가')"
REMOTE

# ---- 3. 소유권 원복 ----
# 서비스는 coinbot 계정으로 돈다(coinbot.service User=coinbot). coinbot 계정이
# 아직 없으면(최초 배포, setup_ec2.sh 를 아직 안 돌린 상태) 건너뛴다 - 그
# 스크립트가 나중에 한 번에 정리한다.
echo "==> 실행계정 소유권 원복"
"${SSH[@]}" "if id -u coinbot &>/dev/null; then sudo chown -R coinbot:coinbot $APP_DIR && echo '    coinbot:coinbot 로 원복'; else echo '    coinbot 계정 없음 - setup_ec2.sh 를 먼저 실행하세요'; fi"

# ---- 4. 오프라인 점검 ----
echo "==> 오프라인 통합 점검"
"${SSH[@]}" "cd $APP_DIR && DRY_RUN=true UPBIT_OPEN_API_ACCESS_KEY=x UPBIT_OPEN_API_SECRET_KEY=x ./.venv/bin/python smoke_test.py 2>&1 | tail -4"

# ---- 5. systemd ----
echo "==> systemd 유닛 설치"
"${SSH[@]}" "sudo install -m 0644 $APP_DIR/deploy/coinbot.service /etc/systemd/system/coinbot.service && sudo systemctl daemon-reload && sudo systemctl enable coinbot -q && echo '    등록 완료'"

echo
echo "============================================================"
echo "업로드 완료. 다음 단계:"
echo "  1) 시크릿 등록 (SSM 또는 서버 .env)"
echo "  2) bash deploy/verify_ec2.sh $HOST $PEM   <- 업비트 허용 IP 검증"
echo "  3) DRY-RUN 관찰 후 실주문 전환"
echo "============================================================"
