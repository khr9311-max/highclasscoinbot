# PC 검증 후 AWS로 옮기는 실행 절차

현재 이 배포가 실행하는 것은 **공개 시세 기반 paper 프로세스 `btc_lab.forward`**다. 주문 실행기, 실계좌 복구, 보호 주문, 원격 청산은 포함하지 않는다. 서비스 이름·로그·CloudFormation 태그에도 paper를 명시했다. 키를 넣거나 설정 이름을 live로 바꿔 실거래로 전환할 수 없다.

이 문서와 템플릿만 작성·정적 검사했다. AWS 계정이나 자격증명을 읽지 않았고 리소스를 생성하거나 서버에서 설치를 실행하지 않았다. 실거래 주문 실행기를 만들 때도 동일한 서비스에 몰래 추가하지 않고 별도 실행 모드와 계정 대조 검증을 거쳐야 한다.

## 준비된 파일

| 파일 | 용도 |
|---|---|
| `ec2-paper.cloudformation.json` | EC2·고정 EIP·SSM 전용 IAM·보안 그룹·암호화 디스크 템플릿 |
| `install-paper.sh` | 검토한 릴리스를 전용 계정·systemd에 처음 설치. 자동 시작하지 않음 |
| `btc-lab-paper.service` | 공개 paper 프로세스 감시·재시작·SIGINT 정상 종료 |
| `journald-btc-lab.conf` | 이 서비스 로그만 100MB/7일 이내로 순환 |
| `requirements-paper.txt` | 독립 runner에 필요한 numpy만 고정 |
| `future-live-parameter-policy.example.json` | 향후 실거래용 정확한 두 SecureString 읽기 예시. 현재 템플릿에는 미부착 |

## 1. PC에서 먼저 확인

읽기 전용 계정 점검으로 확인한 투입할 BTC 예산과 실제 taker 수수료를 `--equity`, `--fee`에 사용한다. 수수료는 퍼센트 숫자가 아니라 소수다. 예를 들어 0.05%는 `0.0005`다. 현재 계획 예시는 paper 예산 `0.003 BTC`, 수수료 `0.0005`, 계정 첫 구간에서 확인한 유지증거금률 `0.004`다. BTC 예산은 전체 실계좌 잔고를 뜻하지 않는다.

`--maint-margin-rate 0.004`를 PC와 EC2 양쪽에 반드시 똑같이 전달한다. runner의 생략 기본값은 연구용 `0.01`이므로 생략하면 기존 원장 매니페스트와 달라진다. 실제 거래소는 명목 규모에 따라 유지증거금 구간과 누적 공제액이 달라질 수 있다. 여기서는 한 구간의 비율을 고정하고 공통 지갑을 모델링하므로, 첫 구간을 벗어나는 노출이나 격리 증거금의 실제 청산가를 재현하지 않는다.

```powershell
python -m btc_lab.forward --equity 0.003 --fee 0.0005 --maint-margin-rate 0.004 --candidate momentum60_stop20 --state-dir btc_lab/state/forward_pc --duration 120
```

잔여 프로세스 없이 정상 종료하고 `state.json`이 생성되는지, 같은 설정·디렉터리로 재시작했을 때 BTC 원장과 마지막 평가 시점이 이어지는지 확인한다. 후보·시작자산·수수료 변경은 기존 원장에 섞지 말고 별도의 실험으로 남긴다. 아직 120초 관측으로 거래 성과가 검증되는 것은 아니다.

## 2. AWS 인프라 선택

### 현재 자금에서는 서버 비용이 먼저 걸린다

2026-09-24에 조회한 **서울(ap-northeast-2), Linux Shared On-Demand** 공개 요금이다. AWS EC2 지역 가격표 발행 시각은 2026-09-21 19:47:12 UTC다. 원본의 관련 SKU·요율·계산은 [요금 추출 자료](aws-prices-seoul-2026-09-24.json)에 보존했다.

| 항목 | 공개 단가 | 월 730시간 기준 |
|---|---:|---:|
| t3.small Linux | $0.026/시간 | $18.98 |
| gp3 기본 성능 20GB | $0.0912/GB·월 | $1.824 |
| 공인 IPv4/EIP 1개 | $0.005/시간 | $3.65 |
| **합계** | | **$24.454/월** |

EC2·EBS는 [AWS 서울 공식 가격표의 고정 버전](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/20260921194712/ap-northeast-2/index.csv), IPv4는 [AWS VPC 요금표](https://aws.amazon.com/vpc/pricing/)에서 확인했다. 이 계산은 **VAT·세금, 트래픽, 추가 CPU 크레딧, 백업/스냅샷, 추가 gp3 IOPS·처리량을 제외**한다. 무료 크레딧·할인도 적용하지 않았다. 계정별 실제 청구액은 별도다.

BTC/USD **83,461** 스냅샷으로 환산하면 매월 약 **0.000293 BTC**, 현재 확인된 전체 지갑 0.00412273 BTC의 **7.11%**, 운용 예정 0.003 BTC의 **9.77%**에 해당한다. 같은 단가의 12개월 비용은 약 **$293.45**다. BTC 가격이 바뀌면 BTC 환산 비용도 달라진다. AWS 청구를 다른 돈으로 내더라도 전체 순성과를 평가할 때 이 비용을 빼야 한다.

**현재 자금으로는 AWS를 바로 켜기보다 PC에서 더 길게 검증하는 편이 BTC 증가 목표에 맞다.** 이미 사용하는 PC의 추가 전력·연결 비용도 확인하고, 실제 비용 후 우위나 운영 규모가 서버 비용을 감당할 근거를 확보한 뒤 이전한다. 아래 템플릿은 그때 사용할 준비물이다.

- **Ubuntu Server 24.04 LTS, x86_64/amd64**, 공식 AMI를 선택한다. SSM Agent가 설치된 이미지인지 확인한다. AMI ID는 리전마다 달라 템플릿 입력값으로 받는다. [AWS Ubuntu SSM 안내](https://docs.aws.amazon.com/systems-manager/latest/userguide/agent-install-ubuntu.html)
- 작은 단일 paper 프로세스에는 `t3.small`을 시작 후보로 제공한다. 실제 메모리·CPU 크레딧·초기 자료 다운로드 시간을 확인하고 조정한다. 재학습/대규모 백테스트는 이 서버에서 실행하지 않는다. 가격은 리전·과금 방식·시점에 따라 별도 확인한다.
- 기존 VPC와 **Internet Gateway 기본 경로가 있는 public subnet**을 지정한다. EIP가 있어도 NAT Gateway를 거쳐 나가면 거래소가 보는 IP가 달라질 수 있으므로 이 템플릿은 직접 IGW 경로를 전제로 한다.
- 인바운드는 비어 있다. 22번 SSH를 열지 않고 **SSM Session Manager**로 접속한다. 관리자의 IAM 접속 권한은 EC2 역할과 별개다. [AWS Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html)
- EC2 역할은 AWS 공식 최소 Session Manager 정책의 다섯 동작만 사용한다. EC2 생성·삭제, S3, Parameter Store, Secrets Manager 접근권한은 없다. SSM 제어 채널 동작은 리소스 ARN 제한을 지원하지 않아 해당 동작만 `Resource: "*"`이다. 세션 로그의 CloudWatch/S3 업로드나 추가 KMS 세션 암호화는 별도 권한·설정이 필요하다. [AWS 최소 역할 예시](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-create-iam-instance-profile.html)
- 아웃바운드는 HTTPS 443, Ubuntu apt 미러용 HTTP 80, AWS 시간 동기화용 UDP 123이다. apt 저장소를 HTTPS로 바꾸고 확인한 뒤 80 규칙을 제거할 수 있다. DNS는 VPC 기본 resolver를 사용한다.
- IMDSv2를 요구하고 디스크를 암호화한다. 원장 보존을 위해 루트 EBS의 `DeleteOnTermination=false`를 설정했다. 종료 후 남은 EBS는 계속 비용이 생길 수 있어 확인 후 별도 정리해야 한다.
- EIP는 고정 IPv4다. 사용 중/미사용 상태 모두 과금될 수 있다. 실제 Binance private API를 나중에 사용할 때 **거래소가 확인하는 출발지 EIP만 API 키 IP 제한에 등록**한다. Binance 서비스의 계정·지역별 이용 가능 여부도 먼저 확인한다. [AWS EIP와 비용 설명](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/elastic-ip-addresses-eip.html)

콘솔에서 CloudFormation 템플릿을 올리고 AMI/VPC/subnet/리전을 점검한 뒤 변경 집합을 검토한다. IAM 생성이 포함되어 `CAPABILITY_IAM` 승인이 필요하다. 이 단계는 실제 비용 발생 작업이다. 생성 성공 자체가 SSM 또는 Binance 접속 성공을 보장하지 않는다. 템플릿은 봇을 설치하거나 시작하지 않는다.

## 3. 서버에 코드만 배치

Session Manager 연결 후 Ubuntu를 업데이트하고 `python3`, `python3-venv`, `ca-certificates`를 준비한다. Ubuntu 24.04의 Python 3.12를 사용한다.

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv ca-certificates
```

검토한 **동일 바이트의 소스**를 `/opt/btc-lab/releases/20260924/` 아래에 둔다. `btc_lab/`의 소스·배포 파일과 함께, 재사용한 OS 잠금 코드에 필요한 아래 세 파일도 원래 상대 경로로 포함해야 한다. `research.py`, `engine.py`, `growth_metrics.py`의 전이 의존성은 numpy와 표준 라이브러리뿐이다.

```text
btc_lab/__init__.py
btc_lab/forward.py
btc_lab/engine.py
btc_lab/research.py
btc_lab/growth_metrics.py
btc_lab/deploy/...
binance_coinm_v1/__init__.py
binance_coinm_v1/runtime/__init__.py
binance_coinm_v1/runtime/instance_lock.py
```

`binance_coinm_v1/` 전체를 복사하지 않는다. 위 세 파일은 패키지 선언과 표준 라이브러리 OS 잠금만 포함하며 기존 설정·주문·실거래 모듈은 가져오지 않는다. `state/`, `.env`, `.pem`, `.git`, 가상환경, 캐시, 개인 데이터, 기존 Upbit 배포물은 코드 묶음에 넣지 않는다. private 저장소의 짧은 수명 배포 권한이나 검토한 비공개 아티팩트 전달 경로를 쓰고 패키지 SHA256을 PC 값과 비교한다. 공개 URL에 코드를 올리거나 장기 토큰을 서버 명령에 붙이지 않는다.

원장은 소스 파일 바이트의 SHA256을 저장한다. **Windows CRLF → Linux LF 자동 변환도 다른 코드로 판정**되므로 Git 재체크아웃이나 전송기의 줄바꿈 변환을 사용하지 않는다. 검증된 PC 파일을 바이트 그대로 담은 아카이브를 전달하고 압축 해제한 소스별 SHA256도 대조한다. 소스 해시를 맞추려고 `state.json`을 수정하지 않는다.

코드는 root 소유, 디렉터리 0755, 파일 0644로 준비한다. 서비스 계정은 코드를 읽기만 하며, 원장만 `/var/lib/btc-lab-paper/`에 쓴다. 설치기는 릴리스 절대 경로를 제한하고 심볼릭 링크·외부 쓰기 가능한 소스를 거부한다. 준비된 디렉터리만 대상으로 소유권과 모드를 확인한다.

```bash
sudo bash /opt/btc-lab/releases/20260924/btc_lab/deploy/install-paper.sh /opt/btc-lab/releases/20260924 0.003 0.0005 0.004 momentum60_stop20
```

이 스크립트는 첫 설치용이다. 기존 `paper.conf` 또는 실행 중 서비스가 있으면 거부한다. pip 설치는 공개 패키지 저장소에 연결하지만 Binance/AWS API 호출은 하지 않는다. `/etc/btc-lab/paper.conf`에는 시작자산·수수료·고정 유지증거금률·후보 이름만 들어간다. API 키 파일로 사용하지 않는다. 설치 성공 후에도 서비스는 시작·enable 되지 않는다.

설치된 가상환경에서 아래 짧은 공개 paper 점검을 **별도 임시 원장**으로 수행할 수 있다. 아직 PC 원장을 옮기는 단계가 아니다.

```bash
sudo install -d -o btc-lab -g btc-lab -m 0700 /var/lib/btc-lab-paper/smoke
cd /opt/btc-lab/current
sudo -u btc-lab .venv/bin/python -m btc_lab.forward --equity 0.003 --fee 0.0005 --maint-margin-rate 0.004 --candidate momentum60_stop20 --state-dir /var/lib/btc-lab-paper/smoke --duration 120
```

HTTP 지역 제한, 시각 오류, 패키지 오류가 있으면 해결될 때까지 이어서 시작하지 않는다. 초기 신호 계산에는 공개 과거 자료 다운로드가 필요하여 120초보다 초기화가 길 수 있다. 짧은 점검은 해당 EC2가 공개 API를 읽고 상태를 저장한다는 확인이다.

## 4. PC → EC2 원장 인계

**한 원장에 쓰는 실행자는 하나만 유지한다. PC와 EC2의 파일 잠금은 서로 통신하지 않으므로 두 호스트 동시 실행을 막아주지 못한다.**

1. PC 프로세스에 Ctrl+C를 보내 정상 종료시키고 해당 프로세스가 사라졌는지 확인한다. 자동 실행 작업이 있다면 중지한다.
2. PC `state.json`의 SHA256·파일 크기·마지막 시각, 후보·시작자산·수수료·유지증거금률, 소스별 바이트 SHA256을 인계 기록에 남긴다. 종료된 원장의 별도 백업을 보존한다.
3. EC2 서비스가 inactive인지 확인한다. PC의 `state.json`을 비공개 경로로 전달하고 SHA256이 같은지 확인한다. `instance.lock`은 호스트별 잠금 파일이므로 전달하지 않는다.
4. EC2의 `/var/lib/btc-lab-paper/state.json`에 원장을 둔다. 소유자는 `btc-lab:btc-lab`, 권한은 0600으로 맞춘다. 실제 원장이 이미 있으면 덮어쓰지 말고 어느 것이 최종인지 먼저 대조한다.
5. `/etc/btc-lab/paper.conf` 네 값과 소스별 바이트를 PC와 일치시킨다. 기본 350일이 아닌 warmup을 썼다면 `--warmup-days`도 서비스에 명시해 일치시킨다. 시작한 뒤 원장의 마지막 시각·BTC 잔액·모의 포지션이 이어지는지 확인한다.
6. EC2 하나만 자동 실행을 켠다. PC에서는 같은 원장을 다시 시작하지 않는다. 다른 실험을 원하면 다른 원장 이름과 식별자를 쓴다.

```bash
sudo systemctl enable --now btc-lab-paper.service
sudo systemctl status btc-lab-paper.service --no-pager
sudo journalctl --namespace=btc-lab -u btc-lab-paper.service -n 80 --no-pager
```

재시작·장애 대응과 원장 검사:

```bash
# SIGINT 후 최대 300초간 정상 저장을 기다린다.
sudo systemctl stop btc-lab-paper.service
sudo systemctl is-active btc-lab-paper.service

# 문제 해결·원장 확인 후 하나의 실행자만 다시 시작한다.
sudo systemctl start btc-lab-paper.service
```

실패 재시작은 15초 간격이며 10분 내 5회로 제한된다. `failed`이면 원인과 원장을 먼저 조사하고 문제를 해결한 다음 `systemctl reset-failed`를 사용한다. 원장·모의 거래 기록은 로그 회전에 넣지 않는다. 일별 백업은 서비스를 정상 중지한 뒤 원장을 복사하고 SHA256을 기록하거나, 일관성이 보장된 원자 스냅샷을 사용한다. EBS 스냅샷 복원도 서비스 시작 전에 원장을 검증해야 한다.

이 runner는 재시작 후 다운타임 동안의 완성 봉도 소급 재생한다. 서버가 꺼져 있던 기간에도 모의 체결이 생성될 수 있으므로 그 결과를 서버 가동률이나 실제 보호 주문·체결의 증거로 해석하지 않는다. 펀딩 수정은 최근 겹침 조회 범위에서 감지하면 중단하지만, 모든 과거 정정의 실시간 발견을 보장하지 않는다.

새 릴리스 배포는 PC/서버 원장 인계와 동일하게 **중지 → 백업 → 소스·상태 스키마 확인 → current 링크 교체 → 확인 → 시작**으로 한다. 이전 릴리스로 되돌리기 전 현재 원장을 읽을 수 있는지 확인한다. 실행 중인 원장을 과거 백업으로 덮어쓰면 중복 모의 체결이 발생할 수 있다.

## 5. 향후 실거래 키 저장 설계

현재 paper runner에는 키 조회 로직이 없고 EC2 역할에 키 권한도 없다. 아래는 별도 live 실행기를 구현할 때 적용할 설계다.

- Parameter Store의 `/btc-lab/live/binance-api-key`, `/btc-lab/live/binance-api-secret` 두 값을 **SecureString + 고객 관리 KMS 키**로 저장한다. 정책 예시의 REGION/ACCOUNT_ID/KEY_ID를 실제 ARN으로 바꾸고 두 리소스에만 `GetParameter`, 해당 암호화 컨텍스트의 `kms:Decrypt`를 허용한다. `GetParametersByPath`, `PutParameter`, 모든 비밀 조회 권한은 부여하지 않는다.
- KMS 키 정책에도 해당 EC2 역할의 제한된 사용을 허용해야 한다. 기본 `aws/ssm` 키만으로 계정 내 사용자별 복호화 분리를 기대하지 않는다. [AWS SecureString 암호화](https://docs.aws.amazon.com/systems-manager/latest/userguide/secure-string-parameter-kms-encryption.html), [복호화 접근 제한](https://docs.aws.amazon.com/systems-manager/latest/userguide/ps-restrict-decryption.html)
- 애플리케이션이 EC2 역할의 단기 자격증명으로 SDK를 호출하고 메모리에서만 키를 사용하도록 구현한다. 콘솔에 복호화 값을 출력하는 CLI 예시는 사용하지 않는다. 키를 user-data, 코드, systemd `ExecStart`, 셸 명령, 로그, 지원용 압축 파일에 넣지 않는다.
- Binance 키는 거래에 필요한 권한만 사용하고 출금 권한은 주지 않는다. PC에서 EC2로 옮길 때 기존 PC 실행자를 중지하고 IP 제한과 계좌 잔고·미체결 주문·보호 주문·포지션을 모두 대조한다. **모의 `state.json`을 실거래 원장으로 복사하지 않는다.**
- 실제 live 서비스를 시작하려면 주문 중복 방지, 부분 체결, 재시작 시 계정 대조, 거래소 측 보호 주문, 장애 시 신규 진입 차단, 청산과 수수료 실계정 반영이 구현되고 검증돼야 한다. 이 배포 템플릿은 그 검증을 대신하지 않는다.

검토일: 2026-09-24. 정적 문법·구성 검사 범위이며 Ubuntu/EC2 실배포 확인은 아직 수행하지 않았다.
