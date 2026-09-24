# Binance COIN-M Futures 자동매매봇 V1

BTCUSD 무기한(BTCUSD_PERP) · BTC 증거금 · BTC 회계 · 격리 · 원웨이 · 3x · LONG+SHORT ·
1시간봉 신호 / 4시간봉 존 · Trendy Kangaroo · 돌파 진입 · Ladder + Trailing 청산.
**기본 실행은 종이 매매(paper)** 이며, 실거래 주문은 기본적으로 막혀 있다.

저장소 루트의 업비트 현물 봇과 **완전히 분리된 패키지**다. 루트의 코드·DB·`.env`·실행
파일을 읽거나 바꾸지 않는다. 가격행동 규칙만 `strategy/price_action.py` 로 복사했고,
원본과 결과가 같은지는 테스트가 매번 확인한다.

> **현재 결론 (2026-09-24):** Binance COIN-M BTCUSD_PERP 6년 백테스트에서 기본 전략은
> 비용 후 음수(평균 −0.097%/거래, PF 0.70, DSR 0.014)다. 검증 게이트가 닫혀 있으므로
> 실거래는 열리지 않는다. 자세한 수치는 [백테스트 결과](#백테스트-결과-2026-09-24).

---

## 실행

저장소 루트(`d:\고도화코인매매봇`)에서:

```bash
pip install -r binance_coinm_v1/requirements.txt       # 이미 설치돼 있으면 생략
copy binance_coinm_v1\.env.example binance_coinm_v1\.env   # 필요할 때만 (없어도 paper 로 돈다)

python -m binance_coinm_v1 check                 # 공개 API·서버 시간·계약 사양 (키 불필요)
python -m binance_coinm_v1 check --private       # + 계정·잔고·포지션·미체결 (읽기 전용, 키 필요)
python -m binance_coinm_v1 run                   # 봇 실행 (기본 paper)
python -m binance_coinm_v1 run --duration 120    # 2분만 (점검용)
python -m binance_coinm_v1 backtest              # 과거 데이터 증분 다운로드 + 백테스트
python -m binance_coinm_v1 validate              # 백테스트 + 표본 외 종이매매 -> 검증 게이트
python -m binance_coinm_v1 status                # 현재 거래·최근 신호·스냅샷
python -m binance_coinm_v1 gate                  # 실거래 게이트 상태와 닫힌 이유

cd binance_coinm_v1 && python -m pytest          # 테스트 (네트워크 차단, 실주문 불가)
```

상태·로그·캐시는 `binance_coinm_v1/state/` (git 제외):
`coinm_v1.sqlite3`, `logs/coinm_v1.log`, `cache/*.csv`, `backtest_report.json`,
`backtest_trades.jsonl`, `validation_report.json`.

## 구조

```
binance_coinm_v1/
  config/settings.py          설정 로딩·검증·설정 지문 (binance_coinm_v1/.env 만 읽음)
  exchange/
    rest_client.py            서명·시간 동기화·재시도 정책·mutation_guard(LiveOrderGate)
    contract.py               exchangeInfo -> ContractSpec (contractSize·tick·step·minQty ...)
    market_data.py            klines(체결가/마크/지수)·premiumIndex·펀딩 이력 (마감 봉만)
    models.py / gateway.py    공통 모델 · 게이트웨이 인터페이스
    binance_gateway.py        실거래/테스트넷 (algoOrder 조건부 주문 포함)
    paper_gateway.py          종이 거래소 (같은 인터페이스·같은 웹소켓 이벤트 형식)
    websocket.py              시장 스트림·사용자 스트림·listenKey·재연결·stale·중복 제거
  strategy/
    price_action.py           원본 가격행동 규칙 (복사본) + 숏용 가격 반전
    signals.py                Trendy Kangaroo LONG/SHORT 신호, 존 목표, 반전 청산 신호
    ladder.py                 청산 규칙 (사다리 손절·TP1~3 부분청산·3봉 추적·기한·반전)
  risk/
    inverse_math.py           인버스 계약 수식 (BTC 손익·증거금·수수료·펀딩·청산가)
    sizing.py                 13단계 BTC 사이징
    limits.py                 동시 포지션·일일 손실 한도
  execution/
    state_machine.py          상태 기계 (14개 상태, 허용 전이만)
    order_ids.py              clientOrderId 규칙 (cm1<거래id>-<역할><순번>)
    context.py                선기록 주문 제출·결과 불명 확정·체결 귀속·거래별 손익
    entry_manager.py          돌파 대기 -> 시장가 진입 -> 실제 체결 확인
    protection_manager.py     보호 손절 생성·확인·이동, TP 주문
    exit_manager.py           reduce-only 청산·비상 청산·정리
    recovery_manager.py       시작/재연결/주기 복구·대사 (12단계)
    user_events.py            ORDER_TRADE_UPDATE/ACCOUNT_UPDATE/ALGO_UPDATE 파싱·순서 규칙
    live_gate.py              LiveOrderGate (실거래 관문)
    engine.py                 봉 마감·시세·체결 이벤트·복구를 한 줄로 처리
  backtest/                   데이터 캐시·시뮬레이터·지표(DSR/PBO)·워크포워드·리포트
  validation/gate.py          바이낸스 전용 검증 게이트
  storage/                    SQLite (orders·fills·positions·signals·...) · 비밀값 마스킹
  notifications/telegram.py   알림 전용 (큐 + 별도 작업자, 실패해도 매매 계속)
  runtime/                    봇 런타임·계좌 스냅샷·로깅
  tests/                      198개 (네트워크 차단)
```

흐름:

```
1H 마감 봉 ─► 4H 존(마감된 4H 만) ─► Trendy Kangaroo LONG/SHORT ─► 돌파 대기(2봉)
   ─► 시장가 진입 ─► 실제 체결 수량·평균가 조회 ─► 보호 손절(closePosition) 생성·확인
   ─► PROTECTED ─► TP1/TP2/TP3 부분청산(reduceOnly) + 사다리 손절 ─► 3봉 추적 ─► 청산
```

## COIN-M 처리 방식

- **계약 사양은 전부 exchangeInfo 에서 동적으로 읽는다.** symbol, pair, contractType,
  contractStatus, contractSize, marginAsset, baseAsset, quoteAsset, tickSize, stepSize,
  minQty, maxQty, MARKET_LOT_SIZE, orderTypes, PERCENT_PRICE, MAX_NUM_(ALGO_)ORDERS,
  유지증거금률. BTCUSD_PERP 의 contractSize 는 현재 100(USD) 이지만 코드에 박혀 있지 않다
  (ETHUSD_PERP=10 으로 테스트).
- 시작할 때 exchangeInfo 를 못 받거나, 심볼이 `PERPETUAL`·`TRADING`·증거금 BTC·기초자산
  BTC 가 아니거나, 필수 필터·주문 유형이 없으면 **거래하지 않고 종료**한다.
- 수량 = 계약 수. 명목가치(BTC) = 계약수 × contractSize / 가격. USDⓈ-M 식을 쓰지 않는다.
- 가격은 tickSize, 수량은 stepSize 로 맞춘다. 손절은 진입에서 먼 쪽, 돌파 트리거는 돌파를
  더 확실히 요구하는 쪽, 목표는 진입 쪽으로 반올림한다 (규칙을 느슨하게 만들지 않는 방향).
- **2026-06-30 COIN-M/USDⓈ-M 통합 반영:**
  - 조건부 주문(STOP_MARKET/TAKE_PROFIT_MARKET)은 `POST /dapi/v1/algoOrder`
    (`algoType=CONDITIONAL`, `triggerPrice`, `clientAlgoId`). `/dapi/v1/order` 로 보내면 `-4120`.
    미체결 조건부 주문은 `GET /dapi/v1/openAlgoOrders` 로 따로 조회한다.
  - 주문 응답에 `avgPrice` 가 없다 → 실제 체결가는 `GET /dapi/v1/order`/`userTrades` 로 확인.
  - 포지션 모드(dualSidePosition)는 UM 과 공유된다 → **봇이 절대 바꾸지 않는다.**
    헤지 모드면 거래를 멈추고 알린다.
  - 사용자 이벤트 `ALGO_UPDATE` 처리, 발동된 조건부 주문의 실제 주문 id(`ai`)로 체결을 연결.
- 가격 종류를 구분한다: 진입 돌파 = 체결가(설정 가능), 보호 손절 트리거 = 마크가
  (`MARKET_TRIGGER_TYPE`, 기본 MARK_PRICE), 목표 = 체결가(`TP_TRIGGER_TYPE`),
  미실현손익·청산가 = 마크가, BTC 의 USD 평가 = 지수가.

## BTC 기반 사이징 (`risk/sizing.py`)

```
 1. equity_btc                         (지갑 + 미실현, 거래소 margin balance)
 2. risk_budget_btc = equity x RISK_PER_TRADE_PCT
 3. entry  = 현재가 (돌파 순간) x (1 ± 진입 슬리피지)
 4. stop   = 전략 손절가 x (1 ∓ 손절 슬리피지)
 5. stop_distance = |entry - stop| / entry
 6. 1계약 손실(BTC) = CS x |1/stop - 1/entry| + CS/entry x taker + CS/stop x taker
    원하는 계약 수 = budget / 1계약 손실
 7. contractSize (CS) = exchangeInfo 값
 8. 계약 수 = floor(원하는 계약 수 / stepSize) x stepSize        (절대 올림 없음)
 9. minQty 미만이면 거래 안 함 · maxQty/MARKET maxQty · 명목 <= equity x MAX_EXPOSURE_MULTIPLE
10. 필요 증거금 = 계약 x CS / entry / LEVERAGE (+진입 수수료) > 가용이면 줄이거나 거부
11. 예상 수수료 (진입+손절 청산)
12. 예상 펀딩 = 명목 x |현재 펀딩비율| x (최대 보유 기간의 펀딩 횟수)   (참고, 필터 아님)
13. 청산가 거리 >= 손절 거리 x LIQ_GUARD_MIN_RATIO 아니면 거부
```

예: equity 0.007 BTC, 위험 0.5% → 예산 0.000035 BTC. 진입 84,000 / 손절 83,160(1%) →
1계약 손실 ≈ 0.0000142 BTC (수수료·기본 슬리피지 포함) → **2계약**, 증거금 0.00079 BTC (3x).
손절 폭이 약 2.7% 를 넘으면 1계약 손실이 예산을 넘어 그 신호는 건너뛴다 (반올림으로 예산을
넘기지 않는다).
레버리지를 바꿔도 계약 수는 같고 증거금만 바뀐다 (테스트로 고정).

## 손익 (BTC 와 USD 를 섞지 않는다)

| 항목 | 정의 |
|---|---|
| `realized_pnl_btc` | 체결별 실현손익 합 (거래소 값). 로컬 인버스 계산으로 교차 확인 |
| `unrealized_pnl_btc` | 방향 x 계약 x CS x (1/평단 − 1/마크가) |
| `trading_fee_btc` | 체결별 수수료 합 (BTC) |
| `funding_fee_btc` | 보유 중 펀딩 (받으면 +, 내면 −), 거래소 income 기록이 출처 |
| `net_pnl_btc` | realized − fee + funding |
| `realized_pnl_usd` 등 | 각 BTC 금액 x **그 사건 시점 가격** 의 합 |
| `net_pnl_krw` | net_pnl_usd x USD/KRW (표시용) |

계좌 스냅샷(15분): `wallet_balance_btc`, `available_balance_btc`, `equity_btc`,
`used_margin_btc`, `equity_usd`(= equity x 지수가), `equity_krw`.

## 상태 기계

```
IDLE -> SIGNAL_DETECTED -> ENTRY_PENDING -> ENTRY_FILLED -> PROTECTING -> PROTECTED
     -> TP1 -> TP2 -> TP3 -> TRAILING -> CLOSING -> CLOSED
보유 상태 어디서든 -> CLOSING,  이상 -> RECOVERY_REQUIRED,  복구 불가 -> ERROR
```

허용 전이 외에는 `InvalidTransition` 으로 막고 `risk_events` 에 남긴다. 보유 단계는 앞으로만
간다 (TP2 -> TP1 금지). 모든 전이는 `state_transitions` 에 기록된다.

## 주문 안전

- **보호 순서:** 실제 체결 수량 조회 → 실제 평균가 확인 → 보호 손절(`STOP_MARKET`,
  `closePosition=true`) 생성 → `GET algoOrder` 로 존재 확인 → `PROTECTED`.
  3번 실패하거나 손절가가 이미 현재가 너머(`-2021`)면 **즉시 비상 청산**.
- 부분청산(TP)은 `TAKE_PROFIT_MARKET reduceOnly` + 수량, 청산은 시장가 reduceOnly →
  포지션을 늘리거나 뒤집을 수 없다. 부분청산 수량은 '누적 내림'이라 합이 초기 수량을
  넘지 않는다. closePosition 손절은 부분청산 뒤에도 남은 전량을 닫는다.
- 손절 이동은 "새 손절 생성·확인 → 옛 손절 취소" (보호 공백 없음).
- 청산 중에도 손절은 포지션 0 확인까지 유지. 정리가 끝나야 `CLOSED` (이전 거래의 주문이
  남은 채 다음 거래를 시작하지 않는다).
- **즉시 반전 금지:** LONG 보유 중 SHORT 신호 → LONG 만 닫고, 그 신호는 청산에만 쓴다.
  다음 봉 이후의 **새** SHORT 신호를 기다린다.

## 중복 주문 방지

- clientOrderId = `cm1<거래id>-<역할><순번>` (EN 진입, SL 손절, TP 목표, EX 청산, EM 비상).
- 보내기 **전에** DB 에 `PENDING_SUBMIT` 로 기록 → 프로세스가 죽어도 흔적이 남는다.
- 결과를 모르는 오류(시간 초과·5xx·`-1007`)는 자동 재전송하지 않고 같은 id 로 조회해 확정.
  '없음' 이 반복 확인될 때만 새 순번으로 1회 재시도, 진입은 포지션까지 확인.
- 웹소켓 중복·역순 메시지: 체결 id 로 한 번만 기록, 주문 상태는 되돌리지 않는 단조 규칙.
- 모든 상태 변경·주문은 하나의 asyncio 락 안에서 순서대로.

## 복구 (Binance 가 기준)

시작 / 사용자 스트림 재연결 / 5분마다:
1 계정 → 2 BTC 잔고 → 3 포지션 → 4 미체결 일반 주문 → 5 미체결 조건부 주문 → 6 DB 비교 →
7 **고아 포지션**(로컬 기록 없음): 채택 후 손절(기본, 진입가 ∓3%) 또는 청산, 사람 확인 전
신규 진입 차단 → 8 **고아 주문**(이 봇 접두사): 취소 / 남의 주문: 건드리지 않고 신규 진입 차단 →
9 **손절 누락**: 즉시 재생성 → 10 **오래된 진입**: 결과 불명 진입 주문을 조회로 확정(체결됐으면
이어서 보호), 재시작 공백이 길었던 대기 신호 취소 → 11 대사: 수량 동기화, 오프라인 중 청산된
거래 정리(REST 로 체결·펀딩 복원), 청산 중/청산 실패 거래는 청산 재개 → 12 이상 없으면 신규 신호 허용.
**로컬 DB 에 포지션이 없다고 새 포지션을 만들지 않는다.**

## 실거래 관문 (LiveOrderGate)

실제 계정으로 가는 모든 서명 변경 요청(주문·취소·레버리지·마진 타입)은 REST 클라이언트의
`mutation_guard` 에서 검사한다 (상위 코드 버그로도 우회 불가).

| 조건 | 신규 진입·계정 변경 | 보호·축소(손절·reduceOnly·취소) |
|---|---|---|
| `BINANCE_ENV=live` · `EXECUTION_MODE=live` · `LIVE_TRADING_ENABLED=true` · `LIVE_CONFIRMATION=I_UNDERSTAND_LIVE_TRADING` · 실거래 호스트 | 필수 | 필수 |
| 바이낸스 검증 게이트 통과 | 필수 | 불필요 (리포트 만료로 손절이 막히면 포지션이 무방비가 되므로) |

`paper` 는 거래소로 주문을 보내지 않는다. `testnet` 은 테스트넷 호스트로만 보낸다.

**검증 게이트** (업비트 PASSED 결과와 무관, 이 패키지 DB 의 리포트만 봄): 백테스트 거래 100건 이상 ·
비용 후 기대값 > 0 · 최대낙폭 ≤ 30% · DSR ≥ 0.95 · PBO ≤ 0.5 · 워크포워드 OOS 평균 > 0 이고
양수 구간 절반 이상 · 백테스트 이후 신호의 종이 거래 30건 이상 · 그 평균 > 0 · 설정/계약 지문 일치 ·
리포트 3일 이내.

## 백테스트 결과 (2026-09-24)

Binance COIN-M 공개 API 데이터만 사용 (업비트 결과를 쓰지 않음).
BTCUSD_PERP 1h 53,642봉·4h 13,411봉·마크 1h 53,641봉(공백 0), 펀딩 6,707건,
2020-08-21 ~ 2026-09-24. 시작 1 BTC, 위험 0.5%, 3x 격리, taker 0.05%, 슬리피지 3bp(손절 5bp),
손절 트리거 마크가, 실제 펀딩 반영, 정수 계약.

| 기본 전략 `ladder|both` | 값 |
|---|---|
| 신호 / 거래 | 147 (롱 89·숏 58) / 117 |
| 승률 | 24.8% |
| 평균 / 중앙값 (비용 후, equity 대비) | **−0.097%** / −0.497% (비용 전 −0.042%) |
| Profit factor / 기대값 | 0.70 / −0.00094 BTC (−0.19R) |
| 총수익 / 최대낙폭 | −11.0% / 13.1% |
| Sharpe / Sortino (일간, 연율) | −0.50 / −0.19 |
| **DSR / PBO** (시행 15) | **0.014** / 0.47 |
| 롱 / 숏 | 75건 −0.113% (PF 0.66) / 42건 −0.069% (PF 0.78) |
| 연도별 | 2020 −0.24% · 2021 −0.02% · 2022 −0.21% · **2023 +0.36%** · 2024 −0.02% · 2025 −0.36% · 2026 −0.00% |
| 워크포워드 (6구간 앵커드) | 기본 전략 OOS −0.12% (90건), 양수 구간 1/5 |
| 청산 사유 | 손절 70 · 추적 손절 30 · 반전 신호 11 · 사다리 손절 5 · 기한 1 |
| 실계정 규모 0.007 BTC | 77건 체결, **40건 건너뜀**(1계약 손실 > 예산), 총 −4.3% |

- 15개 시행(청산 5종 × 방향 3종) **전부 음수**. 가장 나은 `zone|short` 도 −0.016%.
- 원본 업비트 규칙 그대로(`ladder_ratchet|long`)도 −0.096% (75건).
- 업비트 백테스트의 양수(+0.92%)는 94/109건이 알트였고, 업비트 코어(BTC 등) 15건은
  +0.32% 였다. BTC 1시간봉 단일 종목에서는 이 패턴이 비용을 넘지 못한다.
- 검증 결과: **NOT PASSED** (기대값·DSR·워크포워드·종이매매 표본). 실거래 게이트 닫힘.

봉 내부 가정 (보수적): 같은 봉 진입+손절 → 손절, 손절+목표 → 손절, 진입 봉 목표 불인정,
갭은 시가 체결, 봉 마감 청산은 다음 봉 시가 + 슬리피지.

## 테스트

`cd binance_coinm_v1 && python -m pytest` — **198개 통과**. 외부 DNS·소켓 연결을 막고,
실제 HTTP/웹소켓/텔레그램 전송 객체 생성을 금지한 상태로 돈다 (실주문 불가).

| 파일 | 내용 |
|---|---|
| test_config_storage | 설정 검증, 비밀값 마스킹(repr·로그·DB) |
| test_exchange_connectivity | 서버 시간, 서명, 재시도 정책, exchangeInfo, 심볼 해석·실패 시 차단 |
| test_market_data | 마감 봉만, 가격 종류 분리, 200일 페이지네이션, 펀딩 이력 |
| test_account_sync | 잔고·포지션·미체결(일반/조건부), algoOrder 라우팅, 결과 불명 |
| test_paper_gateway | 인버스 손익·증거금·수수료, reduceOnly, 마크 트리거, 펀딩, 강제청산 |
| test_websocket | 재연결, stale, 수명 교체, 중복, listenKey 만료, 메시지 순서 |
| test_strategy(_parity) | 원본 동일성, 롱/숏 대칭, 미래 봉 미사용, 래더 수량·사다리·추적 |
| test_risk | 인버스 수식, 13단계 사이징, 레버리지 독립, 청산가, 일손실 |
| test_execution | LONG/SHORT, 부분 체결, 보호 손절, TP1~3, 추적, reduce-only, 취소, 즉시 반전 금지 |
| test_recovery | 고아 포지션/주문, 손절 누락, 타임아웃 중복 방지, 재연결, 진입·체결·청산 중 재시작 |
| test_live_safety | LiveOrderGate 전 경로, 레버리지 자동 상향 금지 |
| test_backtest / test_validation / test_runtime | 시뮬레이터 규칙·지표·DSR/PBO, 검증 게이트, 런타임·텔레그램 장애 격리 |

## 아직 없는 것 / 한계

- **전략이 검증을 통과하지 못했다.** 표본 외 종이매매 30건도 BTC 단일 종목 신호 빈도
  (연 약 20건)로는 1년 반가량 걸린다.
- 실거래·테스트넷 주문 경로는 가짜 전송 테스트로만 검증했다 (실제 계정·테스트넷 계정으로
  주문해 본 적 없음). COIN-M algoOrder 의 일부 세부(발동된 주문의 clientOrderId, 동시
  closePosition 손절 허용 여부, 미존재 조회 오류 코드)는 공개 문서가 USDⓈ-M 기준이라
  확인하지 못했다 → 코드가 양쪽을 다 처리하도록 방어적으로 작성했다.
- 일일 손실은 equity 변화로 잰다 (입출금 보정 없음).
- 고아 포지션은 손절만 걸고 사다리·추적은 하지 않는다 (사람 확인 대상).
- 미결제약정·롱숏비율·뉴스·AI 는 신호에 쓰지 않으며 주기 수집도 하지 않는다
  (`MarketData.open_interest` 조회만 있음).
- 웹소켓 API(`ws-dapi`) 주문은 쓰지 않는다 (REST 주문 + 웹소켓 이벤트).
- 백테스트 시장가는 전량 체결 가정 (소량이라 현실적이지만 급변 시 슬리피지는 더 클 수 있다).
- 워치독·서버 배포 스크립트 없음 (로컬 실행 기준).

## 실거래로 가려면

1. 종이 매매를 상주시켜 표본 외 거래를 쌓는다 (`run`).
2. `validate` 가 PASSED 인지 확인한다. **통과 전에는 실거래를 켜지 않는다.**
3. 가능하면 먼저 테스트넷: `BINANCE_ENV=testnet`, `EXECUTION_MODE=testnet`, 테스트넷 키.
4. 별도 소액 계정·주문 권한만 있는 키(출금 권한 없음, IP 제한)로
   `BINANCE_ENV=live`, `EXECUTION_MODE=live`, `LIVE_TRADING_ENABLED=true`,
   `LIVE_CONFIRMATION=I_UNDERSTAND_LIVE_TRADING`.
5. 계정은 원웨이여야 한다 (UM 과 공유 설정, 봇이 바꾸지 않음).
