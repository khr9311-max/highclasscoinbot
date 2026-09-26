# BTC 포트폴리오 실행기

COIN-M BTC 담보 롱·숏과 ETHBTC·BNBBTC·SOLBTC·XRPBTC 현물 주문을 구현했다.
BTCUSDT 전용인 `btc_spot`과 별도 패키지이며, 기존 AWS 서비스가 자동으로 바뀌지는 않는다.
`observe`는 공개 신호 조회, `prepare`는 실계좌 읽기 전용 점검, `run`은 실제 주문 제출이다.
2026-09-25 초기 구현 뒤 AWS에 기존 `swing` 모드를 가동했고 SOLBTC 체결 및 텔레그램 알림을 확인했다.
아래 초기 계좌 준비 기록은 전환 전의 기록이다. 장중 대응을 원하는 요청에 맞춰 별도
[`intraday` 모드](INTRADAY.md)와 [설정 예시](config.intraday.example.json)를 추가했다.
새 모드는 로컬에서 구현·기능 확인했으며 AWS의 운용 전략은 아직 전환하지 않았다.

공격형 후보는 [`AGGRESSIVE.md`](AGGRESSIVE.md)에 규칙, 읽기 전용 확인, 기존 원장 이관과
전환 절차를 적었다. 이번 작업에서는 AWS 서비스를 바꾸지 않았으며 `intraday`는 전략 탐색에서
비용 차감 후 손실이어서 사용하지 않는다. 공격형 설정 예시는
[`config.aggressive.example.json`](config.aggressive.example.json)이다.

## 거래와 자금

- 알트 현물: BTC로 알트를 매수하고 BTC로 매도한다. 완료 일봉의 20·60일 BTC 상대수익 평균이 가장 높은
  한 종목을 선택하며 모두 음수면 BTC로 대기한다. 신호 전환 또는 진입가 대비 5% 하락 시 매도한다.
- COIN-M: BTCUSD_PERP의 완료 4시간봉 EMA20/80으로 롱·숏을 정한다. 반전 시 reduce-only로 청산한 뒤
  실제 무포지션과 이전 손절 취소를 확인하고 반대 진입을 평가한다. 손절 거리는 2 ATR, 1~8% 범위다.
- 총 배정 0.003 BTC를 현물 0.0012 / COIN-M 0.0018 BTC로 분리한다. COIN-M 지갑은 초기 배정과 일치해야 한다.
  초기 BTC 이체는 별도 `btc_portfolio.transfer` 명령으로 1회 실행한다. 거래 루프는 자금을 자동 이동하지 않는다.
  현물과 선물의 BTC를 같은 자금으로 중복 사용하지 않는다.
- 거래별 계획 손실은 총 배정액과 현재 BTC 평가액 중 작은 값의 0.5% 이하다. 두 시장에서 동시에
  포지션을 보유하면 계획 위험은 합산 최대 약 1%다. 갭·슬리피지·네트워크 단절로 실제 손실은 초과할 수 있다.
- 당일 BTC 평가액이 시작보다 2% 이상 줄면 신규 진입을 차단한다. 기존 포지션 청산·보호는 계속한다.
  약 50% 낙폭 연구 선호를 실거래 레버리지나 손실 허용값으로 변경하지 않았다.
- 매매 주기마다 BTC 기준 현물·COIN-M 운용자산을 평가하고, 일일 텔레그램 상태에 실제 배분과
  목표 40%/60%를 함께 표시한다. 현물 배분 차이가 운용자산의 5% 또는 0.0001 BTC 중
  큰 값 이상이면 재배분 검토를 알린다. 초기 실거래 관측 중에는 자동 이체·목표 비율·위험한도
  변경을 하지 않는다.
- COIN-M 수량은 역선물 손익 `계약금액 × |1/진입가 − 1/손절가|`와 수수료·손절 슬리피지를 포함해
  정수 계약으로 내린다. 최소 1계약이 위험예산을 넘으면 이유를 기록하고 해당 진입을 건너뛴다.
- 현물 IOC 주문은 최소수량·최소명목금액·호가단위·계좌별 필터를 확인한다. 남은 매도 불가 잔돈은
  실제 보유자산과 BTC 평가액에 계속 포함하되 새 종목 선택을 영구적으로 막지 않는다.

이 조합은 실행 기능을 구현한 것이며 수익성이 입증된 전략이라는 뜻은 아니다. 기존 연구의 수익률은
새 혼합 포트폴리오의 수익률이 아니다. 현재 지갑에서 즉시 거래 가능한 수량은 `prepare` 결과로 확인한다.
이 규칙의 [과거 재생 결과](../btc_lab/PORTFOLIO_BACKTEST_2026-09-25.md)는 2020-10 이후 BTC 기준 +43.38%(비용 2배 +19.65%),
최대낙폭 10.55%였다. 이익이 소수 거래에 집중되고 종목 구성에 사후 선택 편향이 있어 검증 통과로 보지 않는다.
[포트폴리오 백테스트 재검토와 실행상 한계](BACKTEST_REVIEW_2026-09-25.md)를 전환 전에 확인한다.

## 주문과 복구

주문 의도를 SQLite에 동기 저장한 뒤 1회 전송한다. 응답 유실은 client order ID로 조회만 하며
불명확한 주문을 재전송하지 않는다. 최종 주문 수량과 실제 체결 내역이 일치해야 다음 주문을 허용한다.
현물 수수료는 실제 부과 자산별로 반영하고 초기 예비 BTC 및 기존 알트 보유량을 보호한다.
다른 수수료 자산이 청구되면 비용을 기록하고 신규 거래를 차단한다.

COIN-M 진입 뒤 거래소에 `STOP_MARKET / closePosition / MARK_PRICE` 보호 주문을 확인한다.
보호 확인에 실패한 소유 포지션에는 기록된 reduce-only 시장가 비상 청산을 한 번 요청하고 중단 사유를 남긴다.
비상 청산도 체결을 보장하지 않는다. 통신이 끊기면 확인되지 않은 주문을 중복 전송하지 않는다.
**알트 현물의 5% 손절은 프로그램이 30초마다 감시하는 방식이며 거래소에 보관된 손절이 아니다.**
서버/API 장애 동안 알트 손절은 실행되지 않을 수 있다. COIN-M 보호 주문은 정상 등록 후 거래소에 남는다.

지갑 수량 변화, 다른 주문, 계좌 변경, 설정·실행 코드 변경은 자동으로 수용하지 않는다.
실거래 원장을 지우거나 경로를 바꿔 새 계좌처럼 시작하지 못하도록 registry와 프로세스 잠금을 사용한다.
잠금은 같은 호스트에서만 유효하다. 다른 PC와 AWS를 동시에 같은 계좌로 실행해서는 안 된다.

## 최초 `swing` 도입 전 기록 (현재 지침 아님)

아래 수치와 절차는 배치 전 기록이다. 2026-09-25 이번 로컬 읽기 전용 조회에서는 COIN-M BTC 잔고 0.0018 BTC, 격리·3배·원웨이, 무포지션이었다. AWS 서비스 상태와 등록된 운용 원장은 이번 작업에서 직접 확인하지 못했다. 공격형 전환 절차는 [`AGGRESSIVE.md`](AGGRESSIVE.md)를 따른다.

2026-09-25 읽기 전용 조회 결과: 현물 가용 0.00412273 BTC, COIN-M 가용 0 BTC.
당시 COIN-M은 교차(cross)·20배·원웨이였으며, 실행기의 격리·최대 3배 조건과 일치하지 않았다.
계좌 설정과 자금 준비를 완료하지 않으면 두 시장을 함께 사용하는 `run`은 시작하지 않는다.
당시 2 ATR 손절 약 2.54%에서 최소 1계약 계획 손실은 약 0.000033 BTC로, 총액 기준
거래당 위험예산 0.000015 BTC를 넘었다. 입금만 하면 해당 COIN-M 신호가 바로 주문된다는 뜻은 아니다.
위험예산을 넘는 주문을 강제로 만들기 위해 배율이나 거래당 손실 한도를 높이지 않았다.
당시 현물 후보 SOLBTC의 매수 미리보기는 약 0.204 SOL, 수수료 포함 최대 약 0.00028260 BTC로
거래소 주문 필터를 통과했다. 시세·수수료·신호가 바뀌면 수량도 달라지며 아직 제출한 주문은 아니다.

1. AWS에서 기존 `spotlive`, `spotnotify`를 정상 종료하고 원장·registry를 백업한다.
   미확정 주문이 있다면 먼저 해결한다. PC의 기존 봇도 종료 상태를 유지한다.
2. 바이낸스에서 COIN-M BTC 지갑에 **0.0018 BTC**를 배정한다. 아래 명시적 API 이체 명령도 사용할 수 있다.
   BTCUSD_PERP 격리 및 최대 3배 설정을 확인한다.
   원웨이 설정은 USDⓈ-M과 공유되므로 다른 포지션이 있다면 임의 변경하지 않는다.
3. 현물에 최소 **0.0012 BTC**를 남긴다. 위 잔액 기준 두 배정 후 예비분은 0.00112273 BTC다.
4. AWS에 새 소스를 배치하고 아래 설치 및 `prepare`를 실행한다. 기존 `.env` 값은 출력하지 않는다.
5. `ready=true`와 주문 미리보기를 확인한 뒤 새 서비스를 시작한다. 기존 서비스와 새 서비스를 함께 활성화하지 않는다.

## 실행

```powershell
python -m btc_portfolio observe
python -m btc_portfolio prepare --config btc_portfolio/config.example.json
# 실제 주문을 전송할 수 있는 명령. 위 계좌 준비와 기존 서비스 종료 후 실행한다.
python -m btc_portfolio run --config btc_portfolio/config.example.json --confirm I_UNDERSTAND_LIVE_PORTFOLIO
python -m btc_portfolio.notify
```

`btc_portfolio/state/live/stop.request`를 만들면 신규 주문을 중지하고 루프를 종료한다.
서비스 종료 자체가 보유자산 청산을 뜻하지 않는다. 기존 COIN-M 보호 주문은 유지한다.
`observe`와 `prepare`는 주문·계좌 설정·이체를 하지 않는다. 자동매매 기본 실행 명령은 별도 `run`이다.

### 현물 BTC를 COIN-M으로 1회 이체

바이낸스 API 키에서 `Permits Universal Transfer`를 켜야 한다. `Enable Withdrawals`는 필요하지 않다.
기존 현물 실거래 봇과 알림 서비스를 멈춘 후, **같은 AWS 호스트에서** 등록된 기존 원장과 실제 계좌를
대조하여 예비 BTC를 보존하는 이체 미리보기를 확인한다. PC 미리보기는 AWS 서비스 실행 여부를
확인할 수 없으므로 `ready=false`로 표시한다. `prepare`는 이체하지 않는다.

```bash
sudo -u btcspot /opt/btc-spot/.venv/bin/python -m btc_portfolio.transfer prepare \
  --credentials-file /etc/btc-spot/binance.env \
  --old-spot-ledger /var/lib/btc-spot/live/ledger.sqlite3 \
  --state-dir /var/lib/btc-portfolio/live
# ready=true와 amount, 잔여 현물 BTC를 확인한 뒤에만 1회 실행
sudo -u btcspot /opt/btc-spot/.venv/bin/python -m btc_portfolio.transfer transfer \
  --credentials-file /etc/btc-spot/binance.env \
  --old-spot-ledger /var/lib/btc-spot/live/ledger.sqlite3 \
  --state-dir /var/lib/btc-portfolio/live \
  --confirm I_UNDERSTAND_BTC_COINM_TRANSFER
```

기존 봇이 실행 중이거나, 원장의 미확정 주문,
다른 미체결 주문·포지션·잠긴 잔액이 있으면 이체를 막는다. 전송 직전에 의도를 디스크에 저장하고
`MAIN_CMFUTURE` BTC 이체를 **한 번만** 제출한다. 응답이 유실된 경우 자동 재시도하지 않으며
`transfer-intent.json`과 바이낸스 이체 내역 및 두 지갑 잔액을 대조해 수동으로 처리해야 한다.
이체 후 API 키의 Universal Transfer 권한을 다시 끄고 새 실행기의 `prepare`를 재실행한다.

AWS `/opt/btc-spot`에 이 패키지를 포함한 소스를 배치한 뒤:

```bash
sudo sh /opt/btc-spot/btc_portfolio/deploy/install.sh
sudo -u btcspot /opt/btc-spot/.venv/bin/python -m btc_portfolio prepare \
  --config /etc/btc-spot/portfolio.json --credentials-file /etc/btc-spot/binance.env \
  --state-dir /var/lib/btc-portfolio/live
# ready=true 확인 및 전환 결정 후:
sudo systemctl disable --now spotlive spotnotify
sudo systemctl enable --now btc-portfolio btc-portfolio-notify
```

설치 스크립트는 서비스 파일만 준비한다. 기존 서비스를 끄거나 신규 실거래를 시작하지 않는다.
`btc-portfolio.service`는 기존 `spotlive`와 동시 실행 충돌 방지 설정을 포함한다.
서버에 설치된 Python 환경 및 Binance 전용 자격 파일을 재사용한다.

텔레그램은 별도 프로세스에서 확인된 체결·손절·보호 실패와 상태를 전송한다. BTC 체결금액 또는
실현손익에 현재 참고 BTC/USD와 USDT/KRW를 적용해 원화를 함께 표시한다. 고정 대체 환율이면 표시한다.
SQLite 전송 기록으로 일반적인 재시작 중복을 방지하지만, 전송 성공 직후 응답 유실은 중복될 수 있다.

```powershell
python -m pytest btc_portfolio/tests btc_spot/tests -q -o addopts=''
```

확인 기록: COIN-M 기존 검사까지 포함한 통합 검사 550개 통과. 이후 추가한 영속 원장 재열기와
이전 손절값 초기화 검사를 포함해 새 실행기 검사와 현물 엔진 검사를 함께 실행했다. 2026-09-25
이체 기능 추가 후 관련 검사 208개 통과. 공개 시세 조회와 실계좌 읽기 전용 `prepare`도 실행했다.
이후 AWS 서비스 가동·BTC 초기 이체·SOLBTC 현물 체결은 확인했다. 새 장중 모드의
실체결과 수익성을 확인했다는 뜻은 아니다. 거래소 선물 손절 발동도 아직 확인하지 않았다.

공식 주문 명세:
- https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade
- https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-coin-m-futures/api/rest-api/trade
- https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/asset
