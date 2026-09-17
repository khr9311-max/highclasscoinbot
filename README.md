# AI 퀀트 자동매매 마스터 시스템 (Upbit)

![License](https://img.shields.io/badge/License-MIT-blue.svg)
![Python](https://img.shields.io/badge/Python-3.12-blue)
![Upbit SDK](https://img.shields.io/badge/Upbit%20SDK-Official-green)
![Gemini](https://img.shields.io/badge/Gemini-3.6%20Flash-orange)
![Mode](https://img.shields.io/badge/Default-DRY--RUN-red)

업비트 Open API 기반 자동매매 봇. 리만 기하학 기반 리스크 지표, 자유에너지 강화학습,
메타 레이블링, LLM 다중 에이전트를 결합했고, AWS 상시 운영과 다중 안전장치를 전제로 설계됐다.

> ## ⚠️ 기본값은 DRY-RUN 입니다
> `DRY_RUN` 환경변수를 명시적으로 `false` 로 바꾸기 전까지 **실주문은 나가지 않습니다.**
> 최소 1~2주 모의 운영으로 로그를 확인한 뒤 실전으로 전환하세요.

---

## 🚀 아키텍처

| 계층 | 모듈 | 역할 |
|---|---|---|
| 설정 | `config.py` | 환경변수/SSM 시크릿 로딩, 리스크 한도, 실행 모드 |
| 시세 | `websocket_feed.py` | 업비트 웹소켓 스트리밍 (지수 백오프 재연결) |
| 상태 | `market_state.py` | 호가/체결/티커를 실시간 특징 벡터로 변환 |
| 주문 | `upbit_client.py` | 공식 SDK 래퍼, 레이트리밋(토큰버킷), 잔고/미체결 조회 |
| 실행 | `execution_engine.py` | 중앙 클럭, 주문 추적, DRY-RUN, 재시작 복구 |
| 리스크 | `risk_manager.py` | 노출/손실/건수 한도, 쿨다운, 디스크 영속화 |
| 안전 | `circuit_breaker.py` | 점성·곡률·위상 기반 거래 차단 |
| 기하 | `fisher_geometry.py` | 피셔 정보행렬, 측지선 슬리피지, Wasserstein |
| 학습 | `free_energy_ppo.py` | 마켓 리플레이 기반 온라인 PPO |
| 라벨 | `triple_barrier.py` | 동적 삼중장벽, GA, 메타 레이블링 |
| 검증 | `cross_validation.py` | Purged K-Fold, PBO, DSR |
| 판단 | `multi_agent.py` | Gemini 3-에이전트 합의 |
| 알림 | `notifier.py` | 텔레그램 실시간 통보 |
| 통합 | `main.py` | 전체 파이프라인 + 스케줄러 |
| 점검 | `smoke_test.py` | 오프라인 통합 점검 (58개 항목) |

### 의사결정 흐름

```
웹소켓 → MarketState(특징벡터) → 서킷브레이커 → 리스크 회계
                                        ↓ (통과 시)
              LLM 3-에이전트 합의 → RL 에이전트 확인 → 메타라벨 필터
                                        ↓
                         RiskManager 최종 관문 → 주문
```

**모든 관문을 통과해야만 주문이 나간다.** 어느 한 단계라도 실패하면 그 주기는 건너뛴다.

---

## 🛡️ 안전장치

| 장치 | 기본값 | 설명 |
|---|---|---|
| `DRY_RUN` | `true` | 모의주문. 리스크 로직은 전부 동일하게 태움 |
| `ORDER_SIZE_KRW` | 10,000 | 1회 주문금액 |
| `MAX_POSITION_KRW` | 50,000 | 종목당 최대 보유 평가액 |
| `MAX_TOTAL_EXPOSURE_KRW` | 150,000 | 전체 최대 노출 |
| `DAILY_LOSS_LIMIT_KRW` | 30,000 | 초과 시 당일 신규 매수 정지 (재시작해도 유지) |
| `MAX_ORDERS_PER_DAY` | 40 | 일일 주문 건수 상한 |
| `ORDER_COOLDOWN_SEC` | 60 | 동일 종목 재주문 최소 간격 |
| `MAX_SLIPPAGE_RATE` | 0.005 | 스프레드가 이보다 넓으면 시장가 주문 안 냄 |
| `MARKET_DATA_STALE_SEC` | 30 | 시세가 정체되면 매매 중단 |

이 외에 업비트 최소 주문금액(5,000원) 검증, 보유수량 초과 매도 차단,
미보유 종목 매도 차단, 재시작 시 미체결 주문 복구가 항상 작동한다.

### 서킷 브레이커

세 신호 중 **하나만 걸려도** 거래를 차단하고 미체결 주문을 전량 취소한다 (60초 쿨다운).

- **점성 게이트** — 스프레드 확대 + 호가 불균형 + 실현변동성을 0~1 로 결합, `z > 0.8`
- **곡률 붕괴** — 시장 특징행렬의 스펙트럼 엔트로피 기반 지표, `kappa < -0.3`
- **호가창 단절** — Betti-0(연결성분). 종목별 자기 이력 중앙값 + 5 를 **3틱 연속** 초과 시

> Betti-0 임계값은 실측으로 보정했다. 업비트 라이브 호가 약 3,800표본 기준
> KRW-BTC 는 평상시 중앙값 10(최대 21), KRW-ETH/XRP/SOL 은 1 로 종목 구조가
> 전혀 달라, 고정 임계값을 쓰면 BTC 에서 상시 오발동한다.

---

## ⚙️ 설치

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env     # 키 입력
```

`.env` (로컬 개발용. **커밋 금지 — `.gitignore` 에 등재돼 있음**):

```env
UPBIT_OPEN_API_ACCESS_KEY="..."
UPBIT_OPEN_API_SECRET_KEY="..."
TELEGRAM_BOT_TOKEN="..."      # 선택
TELEGRAM_CHAT_ID="..."        # 선택
GEMINI_API_KEY="..."
```

### 점검

```bash
python smoke_test.py     # 오프라인 통합 점검 (네트워크 호출 없음)
python main.py           # DRY-RUN 기동
```

각 모듈은 `python <모듈>.py` 로 개별 자체 테스트를 돌릴 수 있다.

---

## ☁️ AWS 배포

`deploy/` 에 systemd 유닛과 EC2 세팅 스크립트가 들어 있다.

**권장 구성**

- 리전 `ap-northeast-2`(서울), 인스턴스 `t3.small`(2GB), 스토리지 gp3 30GB
- **Elastic IP 필수** — 업비트 Open API 는 허용 IP 등록을 요구하므로, 고정 IP가
  없으면 재부팅마다 인증이 깨진다
- 시크릿은 `.env` 대신 **SSM Parameter Store(SecureString)** + IAM 인스턴스 역할
- systemd `Restart=always`, 로그는 journald → CloudWatch
- chrony 시간 동기화 (업비트 JWT 가 타임스탬프를 검증)
- 인바운드 전면 차단 + SSM Session Manager 로 접속

```bash
sudo bash deploy/setup_ec2.sh
sudo systemctl start coinbot
sudo journalctl -u coinbot -f | grep HEARTBEAT
```

Lambda 는 쓰지 말 것 — 상시 웹소켓 + 이벤트 루프 구조라 15분 제한에 걸린다.

### 모니터링

`HEARTBEAT` 로그가 60초마다 찍힌다. 끊기면 봇이 멈춘 것이다.

```
HEARTBEAT ticks=121 ws_msgs=4069 ws_age=0.0s replay=61 z=0.071 kappa=0.450 orders=0 cb=False
```

CloudWatch Logs 메트릭 필터로 `HEARTBEAT` 를 잡아 5분 결측 시 SNS 알람을 거는 것을 권장한다.

---

## 📋 실전 전환 체크리스트

1. `python smoke_test.py` 전 항목 통과
2. DRY-RUN 으로 최소 1~2주 상주, 로그에서 주문 판단 흐름 검증
3. Elastic IP 를 업비트 허용 IP 에 등록
4. 텔레그램 알림 수신 확인
5. **별도 계정 + 잃어도 되는 소액**으로 `DRY_RUN=false` 전환
6. 리스크 한도를 작게 시작해 점진 증액

---

## ⚠️ 면책 조항

- 본 저장소의 코드는 학술적 연구 및 개념 증명(PoC) 목적으로 작성됐다.
- 암호화폐 매매는 극심한 변동성을 수반하며 금전적 손실이 발생할 수 있다.
- 메타 레이블링 모델은 **학습 데이터를 투입해 직접 훈련하기 전까지 비활성**이며,
  이 경우 LLM 신뢰도 임계(0.7)로 대체 판정한다.
- 어떠한 직·간접적 투자 손실에 대해서도 개발자는 책임지지 않는다.
