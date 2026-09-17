#!/usr/bin/env bash
# =============================================================================
# EC2 (Amazon Linux 2023, ap-northeast-2) 코인봇 초기 세팅
#
#   sudo bash setup_ec2.sh
#
# 사전 준비 (이 스크립트가 하지 않는 것):
#   1) Elastic IP 할당 후 인스턴스에 연결  <- 업비트 API 허용 IP 등록에 필수
#   2) 그 EIP 를 업비트 Open API 허용 IP 로 등록
#   3) IAM 인스턴스 역할에 ssm:GetParametersByPath + kms:Decrypt 부여
#   4) SSM Parameter Store 에 시크릿 등록 (아래 put_secrets 참고)
# =============================================================================
set -euo pipefail

APP_USER=coinbot
APP_DIR=/opt/coinbot
STATE_DIR=/var/lib/coinbot
REGION=ap-northeast-2

echo "==> 시스템 패키지 설치"
dnf -y update
dnf -y install python3.12 python3.12-pip git chrony amazon-cloudwatch-agent

echo "==> 시간 동기화 (업비트 JWT 는 타임스탬프를 검증한다. 시계가 밀리면 인증 실패)"
systemctl enable --now chronyd
grep -q "169.254.169.123" /etc/chrony.conf || \
  sed -i '1i server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4' /etc/chrony.conf
systemctl restart chronyd
chronyc sources -v | head -5 || true

echo "==> 전용 계정 생성 (root 로 봇을 돌리지 않는다)"
id -u "$APP_USER" &>/dev/null || useradd --system --shell /sbin/nologin --home "$APP_DIR" "$APP_USER"
mkdir -p "$APP_DIR" "$STATE_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$STATE_DIR"
chmod 750 "$STATE_DIR"

echo "==> 애플리케이션 배치"
# 코드는 git clone 또는 scp 로 $APP_DIR 에 미리 올려둔다.
if [ ! -f "$APP_DIR/main.py" ]; then
  echo "!! $APP_DIR/main.py 가 없습니다. 코드를 먼저 배치하세요." >&2
  exit 1
fi

echo "==> 가상환경 + 의존성"
sudo -u "$APP_USER" python3.12 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
# torch 가 CPU 전용 휠이면 설치 용량이 크게 줄어든다 (GPU 인스턴스가 아니라면 권장)
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install \
  --index-url https://download.pytorch.org/whl/cpu torch==2.14.0
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> systemd 유닛 설치"
install -m 0644 "$APP_DIR/deploy/coinbot.service" /etc/systemd/system/coinbot.service
systemctl daemon-reload
systemctl enable coinbot

echo "==> CloudWatch 로그 전송 설정"
cat >/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<'JSON'
{
  "agent": { "run_as_user": "root" },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/messages",
            "log_group_name": "/coinbot/system",
            "log_stream_name": "{instance_id}",
            "retention_in_days": 14
          }
        ]
      }
    }
  },
  "metrics": {
    "namespace": "CoinBot",
    "append_dimensions": { "InstanceId": "${aws:InstanceId}" },
    "metrics_collected": {
      "mem": { "measurement": ["mem_used_percent"] },
      "disk": { "measurement": ["used_percent"], "resources": ["/"] }
    }
  }
}
JSON
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json

cat <<'EOF'

============================================================
설치 완료. 다음 순서로 진행하세요.

1) SSM 에 시크릿 등록 (로컬에서 1회, EC2 아님):
   for k in UPBIT_OPEN_API_ACCESS_KEY UPBIT_OPEN_API_SECRET_KEY \
            TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID GEMINI_API_KEY; do
     aws ssm put-parameter --region ap-northeast-2 \
       --name "/coinbot/$k" --type SecureString --overwrite \
       --value "$(read -rsp "$k: " v; echo "$v")"
   done

2) 기동 (기본 DRY_RUN=true - 실주문 안 나감):
   sudo systemctl start coinbot
   sudo journalctl -u coinbot -f

3) 하트비트 확인:
   sudo journalctl -u coinbot | grep HEARTBEAT

4) 최소 1~2주 DRY-RUN 관찰 후 실주문 전환:
   sudo systemctl edit coinbot     # DRY_RUN=false 로 오버라이드
   sudo systemctl restart coinbot

!! Elastic IP 를 업비트 허용 IP 에 등록했는지 반드시 확인하세요.
   등록하지 않으면 재부팅 때마다 인증이 깨집니다.
============================================================
EOF
