# BTC 수량 증가를 위한 현물 실행 봇

소액 재검증에서 선정한 **BTCUSDT 현물 분할 모멘텀**을 실제 주문 실행기로 연결했다.
기존 COIN-M 봇과 상태·전략·실행 명령이 별개다. 자금이 이미 있는 Spot 지갑을 사용한다.

구현·테스트와 PC 실행 결과는 [2026-09-24 준비 기록](PREPARATION_2026-09-24.md)에 있다.

2026-09-24 현재 PC에서 `live` 프로세스가 실행 중이고 Binance 키의 Spot 거래 권한도 켜져 있다.
현재 신호는 BTC 100% 보유라 오늘 판단은 추가 주문 없이 완료됐다. 체결은 0건이며
운용 원장 0.003 BTC와 예비분 0.00112273 BTC를 확인했다.

운용 배정은 **0.003 BTC**다. 2026-09-24 13:18 UTC 읽기 전용 조회에서 전체 Spot 잔액은
**0.00412273 BTC**, 예비분은 **0.00112273 BTC**였다. 초기 배정 외 BTC와 기존 USDT는
원장에 예비분으로 고정하며 거래 수량에 포함하지 않는다. 실제 체결로 생긴 수익은 운용 잔고에 남는다.

## 실행 방식

- 완료된 UTC 일봉의 종가를 20·60·120일 전과 비교한다. 상승은 +1, 하락은 -1, 같으면 0이다.
- BTC 목표 비중은 `(3 + 세 신호의 합) / 6`. 통상 0%, 33.3%, 66.7%, 100%이며 동률일 때 중간 비중도 가능하다.
- 처음 켜면 최신 완료 일봉 신호를 **현재 호가**에 적용한다. 이후 UTC 날짜당 한 번 결정한다.
  날짜 경계는 한국 시각 오전 9시다. 정상 운영 시 다음 polling 주기에 판단한다.
- PC가 꺼져 있던 날의 주문은 소급 실행하지 않는다. 재개 시 최신 신호만 사용한다.
- 상승 추세에는 BTC를 보유하고 약세 신호 때 일부를 USDT로 바꿔 이후 BTC를 더 많이 다시 사는 방식이다.
  레버리지·차입·선물·옵션·자금 이체는 사용하지 않는다. AI/RSS 정보가 주문 수량을 바꾸지 않는다.
- 수량은 Decimal로 계산하고 거래소 단위로 내린다. 최소 주문 미달은 건너뛰며 억지로 늘리지 않는다.
- 수수료는 계좌 API로 확인한다. 조회 당시 할인 미적용 시장가 수수료는 **0.1%**였고 BNB 잔액은 없었다.
  실원장은 예상 수수료 대신 실제 체결별 수수료와 자산 종류를 사용한다.
- 주문은 **LIMIT IOC**다. 현재 호가에서 약 3bps(0.03%)+최대 1호가 단위로 가격을 제한한다.
  제한 가격 안에서 즉시 가능한 수량만 체결하고 나머지는 만료된다. 다음 날까지 무리하게 추격하지 않는다.
  매수 비용과 지불 자산의 수수료까지 보수적으로 예약해 예비 BTC·USDT를 보호한다.
  미체결·부분체결은 정상 결과로 기록하므로 연구의 전량 시장가 체결 가정과 성과 차이가 생긴다.

연구 결과는 [소액 비교](../btc_lab/SMALL_CAPITAL_2026-09-24.md)에 있다. 과거 성과는 약속된 수익이 아니다.
실행은 현재 호가·실제 체결을 사용하므로 일봉 다음 시점의 연구 가격과 차이가 난다.
USDT 보유 중 BTC가 오르면 BTC 환산 자산이 줄어들 수 있다. 자동 손절/원금 보장/강제 BTC 전환은 없다.
사용자가 말한 약 50% 낙폭은 연구 허용 범위이며, 실거래 강제 손절값으로 옮기지 않았다.

## PC 명령

저장소 루트에서 실행한다. 현재 환경의 Python, aiohttp, python-dotenv만 필요하다.

```powershell
# 실계좌 GET 조회, 권한/잔액/수수료와 현재 신호의 첫 주문 예상치를 저장. 주문 없음.
.\run_btc_spot.ps1 Prepare

# 현재 시세로 작동하는 별도 모의 거래 프로세스
.\run_btc_spot.ps1 StartPaper
.\run_btc_spot.ps1 Status
.\run_btc_spot.ps1 Stop
```

기본 인증 자료는 `binance_coinm_v1/.env`의 Binance 키 두 개만 읽는다.
기존 `EXECUTION_MODE`와 레버리지 등은 가져오지 않는다. 루트의 업비트 `.env`는 거부한다.
다른 파일은 `python -m btc_spot prepare --credentials-file btc_spot/.env`처럼 명시한다.

## 계정 권한과 PC live

기존 키의 **Enable Spot & Margin & Stock Trading** 항목이 켜진 것을 계좌 API로 확인했다.
이 항목 이름에 Margin이 포함되어 있어도 이 실행기는 Spot 주문 엔드포인트만 호출한다.
출금·자금 이체·마진 대출 권한은 필요 없다. 기존 IP 제한도 유지한다.

`Prepare`는 계좌를 읽기 전용으로 다시 확인한다. 이미 PC의 live 프로세스가 실행 중이므로
`StartLive`를 중복 실행하지 않는다.
실행 중인 프로세스가 멈춘 경우에만 아래 시작 명령을 사용한다.
시작 즉시 최신 목표와 현재 보유 비중이 다르면 실제 IOC 주문이 나갈 수 있다.

```powershell
.\run_btc_spot.ps1 StartLive -Confirm I_UNDERSTAND_LIVE_SPOT
.\run_btc_spot.ps1 Status -Mode live
.\run_btc_spot.ps1 Stop -Mode live
```

## 텔레그램 현황과 체결 알림

live 거래 봇과 별도로 **읽기 전용 알림 프로세스**를 실행한다. 매매 원장의 실제
체결마다 매수·매도 수량, 체결가, 수수료, 원화 환산을 전송한다. 매일 한국 시각 기준
첫 점검에는 운용금과 예비금을 구분한 현황을 한 번 보낸다. 메시지 발송 실패는 다음
점검에 재시도한다. 체결 ID와 알림 ID가 붙어 있어 응답 유실 후 드문 재전송도 식별할 수 있다.

`binance_coinm_v1/.env`에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`를 설정한다.
원화는 업비트 공개 USDT/KRW 참고 시세를 조회하며, 조회 실패 때만
`USD_KRW_RATE`를 **대체값이라고 표시**해서 사용한다. USDT/KRW는 실제 달러 고시환율이
아니고 원화 금액은 체결 또는 현금화 보장액이 아닌 참고 평가다.

```powershell
.\run_btc_spot.ps1 NotifyStart
.\run_btc_spot.ps1 NotifyStatus
.\run_btc_spot.ps1 NotifyStop
```

`btc_spot/state/live/telegram.sqlite3`에 발송 완료 ID를 기록하고 `telegram.log`에
상태만 기록한다. 텔레그램 전송은 거래 프로세스에 연결되지 않아 네트워크 지연이
주문 처리에 영향을 주지 않는다. PC 절전·종료 시 알림도 멈추며 재시작하면 미발송
체결을 다시 읽는다. AWS 이전 시 이 알림 원장도 같이 백업해야 중복 현황·체결
전송을 줄일 수 있다.

`paper`와 `live`는 서로 다른 디렉터리와 원장을 사용한다. paper 원장을 live에 복사하지 않는다.
준비 명령이나 모의 프로세스가 자동으로 실거래 모드로 바뀌는 일은 없다.
별도 7일 대기 조건이나 과거 V1 전략의 검증 게이트는 이 실행 경로에 없다.

## 상태와 복구

`btc_spot/state/paper/`, `btc_spot/state/live/` 아래:

| 파일 | 내용 |
|---|---|
| `status.json` | 최근 판단, 처리 결과, 잔고 원장, 시세, heartbeat |
| `ledger.sqlite3` | 일별 결정, 제출 의도, 체결·수수료와 예비분 |
| `paper_exchange.json` | paper에만 존재하는 모의 거래소의 계좌·체결 원장 |
| `runtime.log` | 실행 결과 로그. API 키·서명·인증 URL을 쓰지 않음 |
| `readiness.json` | `Prepare` 당시의 계좌 점검과 첫 주문 예상치 |

`btc_spot/state/live-registry.json`은 실계좌와 원장 파일·UUID를 연결한다.
이미 실거래를 시작한 뒤 다른 빈 상태 폴더로 바꾸거나 원장만 삭제하면 시작을 거부한다.
이 파일도 live 이전·백업 대상이며, 경로 이전은 두 프로세스를 끈 상태에서 원장과 함께 처리해야 한다.

주문 의도를 DB에 먼저 저장하고 `bsg_` 주문 ID로 조회한다. 응답 유실·재시작 때
같은 주문을 다시 제출하지 않는다. 거래소에 존재하는 체결만 중복 없이 반영하고,
미확정 주문이 있으면 이후 주문도 멈춘다. 거래소 최소 수량과 계좌별 제한을 검사한다.
수동 거래·입출금 때문에 원장과 잔액이 달라지면 자동으로 새 잔액을 운용금에 편입하지 않는다.
지원하지 않는 수수료 자산이나 제한이 발견되면 기록하고 후속 주문을 차단한다.

`Stop`은 신규 주문을 중단하고 프로세스를 종료한다. BTC를 팔거나 USDT를 강제 환전하지 않는다.
종료 직전에 이미 전송된 주문은 취소되지 않을 수 있으므로 원장을 보존하고 같은 상태로 재개한다.
알 수 없는 주문 상태를 해결하려고 live DB를 삭제하면 중복 주문 위험이 생긴다.
PC 절전·종료 중에는 실행되지 않으며 부팅 시 자동 시작은 아직 등록하지 않았다.

AWS 이전은 [별도 배포 안내](deploy/README.md)를 따른다. 먼저 PC를 정상 종료하고 하나의
실거래 원장만 이전해야 한다. 기존 `btc_lab/deploy` 서비스는 과거 COIN-M paper 연구용이다.

## 검증과 공식 사양

```powershell
python -m pytest btc_spot/tests -q -o addopts=''
```

수량·필터는 [Binance 공식 필터](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md),
계좌 수수료·체결·주문 조회는 [Spot 계좌 API](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account),
주문 전송은 [Spot 거래 API](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade)를 기준으로 구현했다.
