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
| 리스크 | `risk_manager.py` | 노출/손실/매수건수 한도, 쿨다운, 디스크 영속화 |
| 안전 | `circuit_breaker.py` | 점성 기반 거래 차단 (곡률·위상은 기본 비활성, 아래 참고) |
| 기하 | `fisher_geometry.py` | 피셔 정보행렬, 측지선 슬리피지, Wasserstein |
| 학습 | `free_energy_ppo.py` | 마켓 리플레이 기반 온라인 PPO |
| 라벨 | `triple_barrier.py` | 동적 삼중장벽, GA, 메타 레이블링 |
| 검증 | `cross_validation.py` | Purged K-Fold, PBO, DSR |
| 뉴스 | `news_feed.py` | CryptoPanic·네이버·NewsAPI 헤드라인 수집(TTL 캐시) |
| 판단 | `multi_agent.py` | Gemini 점수 산출 + 결정론적 진입 규칙 (`STRATEGY_MODE=llm` 일 때 주문) |
| 가격행동 | `price_action.py` | Naked Forex 존·패턴 판정 (순수 함수, 라이브·백테스트 공용) |
| 가격행동 | `naked_strategy.py` | 매수스톱 진입, 구조적 손절, 11장 청산 4종, 종이 매매 장부 |
| 가격행동 | `candle_feed.py` | 업비트 캔들 캐시, 알트 유니버스 선별 |
| 가격행동 | `validate_naked.py` | 과거 캔들 백테스트 + DSR/PBO + 실주문 게이트 리포트 |
| 기록 | `data_recorder.py` | 가격/호가사다리/신호/주문 적재, 자정 Parquet 압축 |
| 메타 | `meta_trainer.py` | 삼중장벽 라벨링 + LightGBM 메타모델 자동 학습 |
| 알림 | `notifier.py` | 텔레그램 실시간 통보 |
| 통합 | `main.py` | 전체 파이프라인 + 스케줄러 |
| 점검 | `smoke_test.py` | 오프라인 통합 점검 (223개 항목) |

### 전략 모드 (`STRATEGY_MODE`)

| 값 | 주문을 내는 경로 | LLM 판정 |
|---|---|---|
| `naked` (기본) | 가격행동 패턴 (아래 절) | 기록만 + 뉴스 점수는 거부권 |
| `llm` | 아래 의사결정 흐름 | 주문 |

두 경로가 같은 종목에 동시에 주문하면 포지션이 엉키므로(가격행동 손절이 LLM
매수분까지 판다) 한쪽만 주문한다.

### 의사결정 흐름 (`STRATEGY_MODE=llm`)

```
웹소켓 → MarketState(특징벡터) → 서킷브레이커 → 리스크 회계
                                        ↓ (통과 시)
      Crypto Agent 점수 ┐
                        ├→ decide_action(0.7c + 0.3n) → RL 확인 → 메타라벨 필터
      News Agent 점수   ┘                                    ↓
                                          RiskManager 최종 관문 → 주문
```

**모든 관문을 통과해야만 주문이 나간다.** 어느 한 단계라도 실패하면 그 주기는 건너뛴다.

최종 BUY/SELL/HOLD 판정은 LLM 이 문장으로 고르지 않는다. LLM 은 crypto/news
점수만 내고, 조합과 판정은 `multi_agent.decide_action()` 이 숫자로 한다
(`|0.7*crypto + 0.3*news| >= MIN_ENTRY_SCORE`). 프롬프트로 판정을 맡겼을 때
실측 59건이 전부 HOLD 로 나왔고(그중 41%는 두 점수의 부호가 일치했다),
근거 문장이 자기 점수 체계를 어기는 사례가 반복돼서 코드로 옮겼다.

### 섀도 모드

`SHADOW_MODE=true`(기본)면 `TARGET_TICKERS` 전체를 판정·기록하되 **주문은
첫 종목(`TARGET_TICKERS[0]`)에만** 낸다. 메타모델은 BUY/SELL 표본 300건이
있어야 학습되는데, 한 종목만 쓰면 실측 4.5건/시간이라 약 3일이 걸린다.
`meta_trainer` 는 라벨을 주문 체결 여부가 아니라 기록된 가격 시계열의
삼중장벽으로 만들기 때문에, 주문을 내지 않아도 표본이 된다.

> 섀도 신호에는 슬리피지·부분체결이 없어 실제보다 낙관적이다. 특히 호가가
> 얇은 종목에서 차이가 크므로, 실매매 확장 판단 시 그만큼 할인해서 읽어야 한다.

---

## 📐 가격행동 전략 (Naked Forex)

> **전략의 모든 세부(설계, 책→코드 대응, 설정, 검증 게이트, 백테스트 숫자, 한계,
> 고친 문제 목록)는 [docs/NAKED_FOREX.md](docs/NAKED_FOREX.md) 한 곳에만 있다.**
> 여기는 요약이다. 숫자·기준을 바꿀 때는 그 문서만 고친다.

Nekritin & Peters, *Naked Forex* (2012)의 규칙을 업비트 현물로 옮긴 것.
4시간봉 존에서 1시간봉 촉매 패턴(캥거루 꼬리·빅 섀도·와미·라스트 키스·추세 캥거루)이
찍히면 매수스톱으로 진입하고, 구조적 손절(꼬리 아래)과 다음 존 목표/추적으로 청산한다.
모든 신호는 종이 매매로도 굴려 검증 표본을 만든다. 알트는 급등 추격이 아니라
경고 없는 거래대금 상위 종목의 '급등 뒤 첫 쉬어가기'만 잡는다.

**현재 상태:** 책 규칙 그대로는 비용을 넘지 못했고, 가장 나은 후보(추세 캥거루)도
DSR·PBO 기준 미달이라 **실주문은 검증 게이트가 막고 있다.** 봇이 매일 스스로
재평가하며, 표본 외 종이 매매가 쌓여 기준을 넘어야 열린다.
판정은 로그의 `가격행동 검증:` 줄이나 `state/naked_validation.json` 에서 본다.

---

## 🛡️ 안전장치

| 장치 | 기본값 | 설명 |
|---|---|---|
| `DRY_RUN` | `true` | 모의주문. 리스크 로직은 전부 동일하게 태움 |
| `ORDER_SIZE_KRW` | 10,000 | 1회 주문금액 |
| `MAX_POSITION_KRW` | 50,000 | 종목당 최대 보유 평가액 |
| `MAX_TOTAL_EXPOSURE_KRW` | 150,000 | 전체 최대 노출 |
| `DAILY_LOSS_LIMIT_KRW` | 30,000 | 초과 시 당일 신규 매수 정지 (재시작해도 유지) |
| `MAX_BUYS_PER_DAY` | 100 | 일일 **매수** 건수 상한. 매도(청산)는 세지도 막지도 않는다 |
| `ORDER_COOLDOWN_SEC` | 60 | 동일 종목 재주문 최소 간격 |
| `MAX_SLIPPAGE_RATE` | 0.005 | 스프레드가 이보다 넓으면 시장가 주문 안 냄 |
| `MARKET_DATA_STALE_SEC` | 30 | 시세가 정체되면 매매 중단 |
| `MIN_ENTRY_SCORE` | 0.4 | `\|0.7*crypto + 0.3*news\|` 가 이 값 미만이면 진입 안 함 |
| `META_FALLBACK_MIN_STRENGTH` | = `MIN_ENTRY_SCORE` | 메타모델 학습 전 대체 게이트 |
| `SHADOW_MODE` | `true` | 전 종목 판정·기록, 주문은 첫 종목만 |
| `NEWS_CACHE_TTL_SEC` | 900 | 헤드라인 캐시. 무료 API 쿼터 보호용 |
| `STRATEGY_MODE` | `naked` | 주문을 내는 전략 (`naked` / `llm`) |
| `NAKED_LIVE` | = `DRY_RUN` | 가격행동 주문. 실주문 모드에선 명시 + 검증 게이트 통과 필요 |
| `NAKED_*` | | 나머지 가격행동 설정은 [docs/NAKED_FOREX.md §9](docs/NAKED_FOREX.md#9-설정) |

이 외에 업비트 최소 주문금액(5,000원) 검증, 보유수량 초과 매도 차단,
미보유 종목 매도 차단, 재시작 시 미체결 주문 복구가 항상 작동한다.

> 일일 건수 상한을 매수에만 거는 이유: 예전에는 매수·매도가 상한을 공유해서,
> 상한을 다 쓰면 급락 중에 보유분을 청산하지도 못하는 상태가 됐다.

### 서킷 브레이커

조건 중 **하나만 걸려도** 거래를 차단하고 미체결 주문을 전량 취소한다 (60초 쿨다운).

| 조건 | 기본 상태 | 판정 |
|---|---|---|
| **점성 게이트** | ✅ 활성 | 스프레드 확대 + 호가 불균형 + 실현변동성을 0~1 로 결합, `z > 0.8` |
| **곡률 붕괴** | ❌ 비활성 | `kappa < -0.3` (`curvature_enabled=True` 로 켬) |
| **호가창 단절** | ❌ 비활성 | Betti-0 가 이력 중앙값+8 을 10틱 연속 초과 (`topology_enabled=True` 로 켬) |

**곡률과 위상은 지표가 의도대로 동작하지 않아 기본 비활성이다.** 임계값을 높여
증상을 가리는 대신 껐고, 재설계 전까지 이 상태를 유지한다. 두 지표의 값은
계속 기록되므로(가격 CSV 의 `betti0`/`kappa` 컬럼, `depth/*.jsonl`) 재설계용
데이터는 끊기지 않는다.

> **위상(Betti-0)** — 감지하려는 사건에 역방향으로 반응한다. 실측 BTC 사다리
> (60단계)에서 호가를 취소해가며 측정하면 취소 0%/40%/60%/80%/95% 에서
> Betti-0 가 7/6/5/3/1 로 **내려간다**. 호가창이 95% 증발한 상태가 '완벽히
> 연결된 정상 호가창'으로 판정된다. 갭을 같은 스냅샷의 중앙값 갭으로
> 정규화하기 때문에, 호가가 사라지면 모든 갭이 같이 커져 비율이 유지되고
> 셀 수 있는 갭 자체가 없어진다. 이 조건으로 발동한 것은 전부 노이즈였다
> (프로덕션 실측 분포: 중앙값 7 / p90 13 / p99 16 / 최대 19, 자기상관 lag1 0.82).
>
> **곡률(kappa)** — '위험'이 아니라 '한산함'을 잰다. 표준화된 피처 상관행렬의
> 스펙트럼 엔트로피라, 값이 낮다는 것은 피처가 한 방향으로 몰렸다 = 호가가
> 얇고 거래가 뜸하다는 뜻이다. 실측 음수 비율은 BTC 2.5% / ETH 10.1% /
> XRP 37.5% / SOL 49.2% 로, 얇은 종목일수록 상시 음수에 가깝다.
>
> 재설계한다면 연결성분이 아니라 **단계 개수 / 총 잔량 / 가격 스팬**을 직접
> 보는 쪽이어야 한다. 그때까지는 `z_t` 가 이 역할을 한다 — `rel_spread` 가
> 들어 있어 호가가 증발하면 스프레드 확대로 반응한다.

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

# 뉴스 피드 (전부 선택. 하나도 없으면 News Agent 가 중립 고정으로 동작한다)
CRYPTOPANIC_API_KEY="..."
NAVER_CLIENT_ID="..."
NAVER_CLIENT_SECRET="..."
NEWSAPI_KEY="..."
```

뉴스 소스는 각각 독립이다. 키가 없거나 호출이 실패한 소스는 조용히 건너뛰고
나머지로 계속 동작하며, 전부 실패하면 중립 문구로 폴백한다.

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
5. **[알려진 한계](#-알려진-한계) 절을 읽고 감수할 수 있는지 판단** —
   특히 가격행동 전략의 엣지가 아직 표본 외에서 확인되지 않았다는 점
6. 가격행동 검증 리포트가 PASSED — 절차는
   [docs/NAKED_FOREX.md §10](docs/NAKED_FOREX.md#10-실주문-전환-절차)
7. **별도 계정 + 잃어도 되는 소액**으로 `DRY_RUN=false` + `NAKED_LIVE=true` 전환
8. 리스크 한도를 작게 시작해 점진 증액
9. (`STRATEGY_MODE=llm` 인 경우) 메타모델이 학습될 때까지(BUY/SELL 300건) 관찰. 학습 완료 시 텔레그램
   알림이 오며, 그 전까지의 진입은 검증되지 않은 신호다

---

## ⚠️ 면책 조항

- 본 저장소의 코드는 학술적 연구 및 개념 증명(PoC) 목적으로 작성됐다.
- 암호화폐 매매는 극심한 변동성을 수반하며 금전적 손실이 발생할 수 있다.
- 메타 레이블링 모델은 **BUY/SELL 표본이 `min_samples`(기본 300) 이상 쌓이기
  전까지 비활성**이며, 이 경우 진입 강도 임계(`META_FALLBACK_MIN_STRENGTH`)로
  대체 판정한다. 표본이 모이면 1시간 주기 점검에서 자동 학습된다.
- 어떠한 직·간접적 투자 손실에 대해서도 개발자는 책임지지 않는다.

---

## 🚧 알려진 한계

구현되지 않았거나 의도대로 동작하지 않는 것들. 실전 투입 전 반드시 확인할 것.

- **LLM 경로(`STRATEGY_MODE=llm`)에는 손절/익절이 없다.** 그 경로에서
  `place_market_sell` 은 판정 규칙이 SELL 을 낼 때만 불린다.
  가격행동 경로(기본)는 모든 거래에 구조적 손절·목표·보유기한이 있다.
  `DAILY_LOSS_LIMIT_KRW` 는 신규 매수만 막고 보유분을 강제 청산하지 않는다.
- **가격행동 전략의 엣지는 확인되지 않았고, 전략 고유의 한계가 여럿 있다**
  (봇이 감시하는 손절, 알트에는 없는 뉴스 거부권, 생존자 편향, 추정 체결가 등).
  [docs/NAKED_FOREX.md §11](docs/NAKED_FOREX.md#11-남은-한계) 참고.
- **곡률·위상 지표가 비활성이다.** 사유는 위 서킷 브레이커 절 참고.
  현재 살아 있는 차단 조건은 점성 게이트 하나뿐이다.
- **수익성이 검증되지 않았다.** 메타모델 학습 전까지는 모든 진입이 검증되지
  않은 신호다. 시장가 주문이라 왕복 약 0.12%(수수료 0.05%x2 + 스프레드)의
  마찰비용이 확정적으로 나가며, 전략은 최소한 이를 넘어야 본전이다.
- **서킷 브레이커는 `TARGET_TICKERS[0]` 기준으로만 판정한다.** 종목별 평가로
  확장할 경우 곡률을 켜서는 안 된다(얇은 종목은 상시 음수).
- **LLM 경로 매도에는 60초 재주문 쿨다운이 적용된다.** 가격행동 청산은
  `urgent=True` 로 쿨다운을 건너뛴다(보유량·최소금액 검증은 그대로).
