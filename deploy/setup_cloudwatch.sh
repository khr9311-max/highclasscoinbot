#!/usr/bin/env bash
# =============================================================================
# CloudWatch 알람 설정 (인스턴스에서 실행)
#
#   sudo bash /opt/coinbot/deploy/setup_cloudwatch.sh <알림받을_이메일>
#
# 사전 조건: EC2 에 IAM 인스턴스 역할이 붙어 있고, 아래 권한이 있어야 한다.
#   cloudwatch:PutMetricData, cloudwatch:PutMetricAlarm, cloudwatch:DescribeAlarms
#   sns:CreateTopic, sns:Subscribe, sns:ListTopics
#
# 걸리는 알람:
#   1) CoinBot-ServiceDown   : 봇 서비스가 내려감
#   2) CoinBot-NoHeartbeat   : 워치독 지표가 끊김 (= 인스턴스 사망/네트워크 단절)
#   3) CoinBot-DiskLow       : 디스크 여유 15% 미만
#   4) CoinBot-StatusCheck   : EC2 상태 검사 실패 (하드웨어/커널)
# =============================================================================
set -euo pipefail

EMAIL="${1:?사용법: setup_cloudwatch.sh <알림받을_이메일>}"
REGION="${AWS_REGION:-ap-northeast-2}"
TOPIC_NAME="coinbot-alerts"
PY=/opt/coinbot/.venv/bin/python

echo "==> IAM 역할 확인"
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)
ROLE=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  "http://169.254.169.254/latest/meta-data/iam/security-credentials/" 2>/dev/null || true)
if [ -z "$ROLE" ] || echo "$ROLE" | grep -qi "404\|not found"; then
  cat >&2 <<'MSG'
!! IAM 인스턴스 역할이 없습니다.

AWS 콘솔에서:
  EC2 -> 인스턴스 선택 -> 작업 -> 보안 -> IAM 역할 수정
  -> 새 역할 생성 (신뢰할 수 있는 개체: EC2) -> 아래 인라인 정책 부착

{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "cloudwatch:PutMetricData",
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:DescribeAlarms",
      "sns:CreateTopic",
      "sns:Subscribe",
      "sns:ListTopics"
    ],
    "Resource": "*"
  }]
}

역할을 붙인 뒤 이 스크립트를 다시 실행하세요.
MSG
  exit 1
fi
echo "    역할: $ROLE"

echo "==> SNS 토픽 및 구독"
"$PY" - "$REGION" "$TOPIC_NAME" "$EMAIL" <<'PYEOF'
import sys, boto3
region, topic_name, email = sys.argv[1], sys.argv[2], sys.argv[3]
sns = boto3.client("sns", region_name=region)
arn = sns.create_topic(Name=topic_name)["TopicArn"]
print(f"    토픽: {arn}")

# SNS subscribe 는 멱등이다. 같은 이메일을 다시 등록해도 구독이 중복되지 않고,
# 이미 확인된 구독이면 기존 ARN 이 돌아온다. 중복 확인을 위해
# ListSubscriptionsByTopic 을 호출하면 권한이 하나 더 필요하므로 그냥 호출한다.
res = sns.subscribe(TopicArn=arn, Protocol="email", Endpoint=email)
sub_arn = res.get("SubscriptionArn", "")
if sub_arn == "pending confirmation":
    print(f"    구독 요청 발송: {email}")
    print("    >>> 메일함에서 'AWS Notification - Subscription Confirmation' 을 열어")
    print("    >>> Confirm subscription 을 눌러야 알람이 실제로 도착합니다.")
else:
    print(f"    구독: {email} (이미 확인됨)")
with open("/tmp/coinbot_topic_arn", "w") as f:
    f.write(arn)
PYEOF

TOPIC_ARN=$(cat /tmp/coinbot_topic_arn)

echo "==> 알람 생성"
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  "http://169.254.169.254/latest/meta-data/instance-id")

"$PY" - "$REGION" "$TOPIC_ARN" "$INSTANCE_ID" <<'PYEOF'
import sys, boto3
region, topic, iid = sys.argv[1], sys.argv[2], sys.argv[3]
cw = boto3.client("cloudwatch", region_name=region)

alarms = [
    dict(
        AlarmName="CoinBot-ServiceDown",
        AlarmDescription="코인봇 서비스가 내려갔습니다.",
        Namespace="CoinBot", MetricName="ServiceUp", Statistic="Maximum",
        Period=300, EvaluationPeriods=2, Threshold=1.0,
        ComparisonOperator="LessThanThreshold", TreatMissingData="breaching",
    ),
    dict(
        # 워치독이 2분마다 지표를 밀어넣으므로, 15분간 데이터가 없으면
        # 인스턴스가 죽었거나 네트워크가 끊긴 것이다.
        AlarmName="CoinBot-NoHeartbeat",
        AlarmDescription="워치독 지표가 끊겼습니다 (인스턴스 사망/네트워크 단절 의심).",
        Namespace="CoinBot", MetricName="HeartbeatAge", Statistic="Maximum",
        Period=300, EvaluationPeriods=3, Threshold=300.0,
        ComparisonOperator="GreaterThanThreshold", TreatMissingData="breaching",
    ),
    dict(
        AlarmName="CoinBot-DiskLow",
        AlarmDescription="디스크 여유 공간이 15% 미만입니다.",
        Namespace="CoinBot", MetricName="DiskFreePercent", Statistic="Minimum",
        Period=300, EvaluationPeriods=2, Threshold=15.0,
        ComparisonOperator="LessThanThreshold", TreatMissingData="notBreaching",
    ),
    dict(
        AlarmName="CoinBot-StatusCheck",
        AlarmDescription="EC2 상태 검사 실패 (하드웨어/커널).",
        Namespace="AWS/EC2", MetricName="StatusCheckFailed", Statistic="Maximum",
        Period=300, EvaluationPeriods=2, Threshold=0.0,
        ComparisonOperator="GreaterThanThreshold", TreatMissingData="notBreaching",
        Dimensions=[{"Name": "InstanceId", "Value": iid}],
    ),
]

for a in alarms:
    a.update(ActionsEnabled=True, AlarmActions=[topic], OKActions=[topic])
    cw.put_metric_alarm(**a)
    print(f"    [{a['AlarmName']}] {a['AlarmDescription']}")
PYEOF

rm -f /tmp/coinbot_topic_arn
echo
echo "============================================================"
echo "완료. 이메일 구독 확인(Confirm subscription)을 꼭 누르세요."
echo "확인용:"
echo "  aws cloudwatch describe-alarms --alarm-name-prefix CoinBot --region $REGION"
echo "============================================================"
