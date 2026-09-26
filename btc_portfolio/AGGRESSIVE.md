# 공격형 BTC 포트폴리오 후보 (2026-09-25)

목표는 운용분의 BTC 수량 증가다. **README 기록상 AWS 모드는 `swing`이다. 이번 작업에서 AWS 서비스 상태와 원장은 직접 확인하지 못했고, 이 코드는 AWS에 배치하거나 실행하지 않았다.**
전략 변경과 BTC 이체는 아래 절차를 별도로 진행한다. 기존 원장이나 registry를 지우지 않는다.
`deploy/cutover.sh`는 과거 `btc_spot`에서 `swing`으로 옮기던 일회성 스크립트이므로 이 전환에 재사용하지 않는다.

> **2026-09-26 추가:** COIN-M 방향 전환·진입은 상황 원장의 2시간 예측으로 최대 4시간 안에서 체결 시점을 고른다.
> 예측이 없거나 오래되면 즉시 실행한다. 규칙과 배포 기록은 [AWS_TIMING_2026-09-26.md](deploy/AWS_TIMING_2026-09-26.md)에 있다.

## 규칙과 근거

- 일봉 완료 뒤 ETHBTC·BNBBTC·SOLBTC·XRPBTC의 20·60일 BTC 상대수익 평균이 가장 높은 한 종목에 현물 운용분을 둔다. 최고 점수가 0 이하이면 BTC로 대기한다. 알트 손절은 없다. IOC 부분 체결은 하루 최대 3회이며, 목표 95% 이상이면 완료로 본다.
- COIN-M BTCUSD_PERP은 완료 4시간봉 200개 EMA20/80으로 롱·숏을 정한다. 방향이 같으면 보유하고, 바뀌면 reduce-only 청산·무포지션과 기존 손절 취소 확인 후 반대 진입한다. 재난 손절은 최초 진입가에서 12%, 거래소 보관 STOP_MARKET closePosition MARK_PRICE다. 발동 후 같은 방향으로 재진입하지 않는다.
- `vol` 모드 목표 배율은 `clip(2 × 0.0113579 / 직전 180개 완료 4시간 로그수익의 표본표준편차, 1, 3)`이다. 매일 00:00 UTC 첫 점검에 계약 차이가 `max(1, round(현재 계약의 25%))` 이상일 때 조정한다. `fixed`는 진입 시만 계약 수를 정한다. 거래소 격리·원웨이·레버리지 3 설정과 실제 청산가 확인이 필요하다. 3배를 넘는 정수 계약은 제출하지 않는다.
- 매월 1일 UTC 00:00 뒤 첫 점검에서 50:50 목표에서 5%p 이상 벗어나면 알림(`alert`) 또는 BTC 내부 이체(`auto`)를 한다. `auto`에는 Universal Transfer 권한이 지속적으로 필요하다. 출금 권한은 필요하지 않다.
- 운용 합계가 이관 시점의 25% 이하가 되면 신규 알트 매수·COIN-M 진입·계약 증가·재배분을 멈춘다. 기존 포지션의 청산·재난손절·알트 매도는 계속한다.

과거 재현은 [`btc_lab/AGGRESSIVE_BACKTEST_2026-09-25.md`](../btc_lab/AGGRESSIVE_BACKTEST_2026-09-25.md)에 있다.
0.003 BTC, 비용 1배, 50:50 `vol`/매월 재배분은 개발 +87%, 검증 +209%, 전체 +497%, 최대낙폭 61%였다.
비용 2배 전체는 +223%다. 동일 기간 알트 전액만은 전체 +495%, 최대낙폭 67%였다.
조합이 5년 수익을 늘린 증거는 없다. 여러 변형을 검증 결과를 본 뒤 비교했고, 현재 생존 알트 4종만 사용했다.
실제 IOC 부분 체결과 지연, 급변 시 손절 가격은 이 수치와 다를 수 있다.

## 로컬에서 읽기 전용 확인

```powershell
python -m btc_portfolio observe --config btc_portfolio/config.aggressive.example.json
python -m btc_portfolio prepare --config btc_portfolio/config.aggressive.example.json
```

로컬에는 AWS 실거래 원장이 없으므로 두 번째 명령의 현물 평가액은 **계좌 전체 추정치**다.
예비 BTC까지 운용분으로 세므로, 이때 이체량은 표시하지 않는다. 실제 이체량과 `ready`는 AWS의 등록된 원장 경로에서 다시 계산한다.
`prepare`는 σ, 목표 배율·계약 수, 실제/추정 청산가, 재난 손절가, 이체량과 차단 사유를 보여 준다.
BNB 현물 수수료 할인 설정이 켜져 있으면 공격형 진입을 차단한다. BNBBTC 보유 시 다른 거래의 수수료가 BNB로 빠져 원장과 어긋날 수 있기 때문이다.

## AWS 전환 순서 — 사용자 실행 단계

아래 명령은 설명용이며 지금 실행하지 않았다. 이체와 원장 변경은 **서버의 실제 상태를 다시 확인한 후** 실행한다.
두 모드를 동시에 실행하지 않는다.

1. `btc-portfolio` 서비스를 정상 정지하고 원장(SQLite backup API)·`btc_portfolio/state/live-registry.json`을 백업한다. 미확정 주문, 포지션, 보호주문이 남아 있으면 먼저 기존 원장대로 대조·정리한다. COIN-M 포지션이 있으면 임의 청산하지 말고 정리 방법을 결정한다.
2. 새 코드를 서버에 배치한다. 바이낸스 현물의 “BNB로 수수료 지불”을 해제하고, 격리·원웨이·거래소 레버리지 3을 읽기 전용으로 확인한다. 이번 개발 작업에서 설정은 변경하지 않는다.
3. `config.aggressive.example.json`을 서버 설정 파일로 복사한 뒤 사용자가 `spot_fraction`을 `0.5` 또는 `1.0`, `rebalance_mode`를 `alert` 또는 `auto`로 선택한다. `auto`면 API의 Universal Transfer 권한을 유지한다.
4. 등록된 AWS 원장에서 이체 미리보기와 이관 미리보기를 실행한다. 현물 운용분과 예비 BTC를 구분한다. 현재 40:60 배분을 50:50으로 바꾸는 COIN-M→Spot 예상액은 약 0.0003 BTC이나 시세·잔고에 따라 달라진다.

```bash
PY=/opt/btc-spot/.venv/bin/python
CONFIG=/etc/btc-spot/portfolio-aggressive.json
CREDS=/etc/btc-spot/binance.env
STATE=/var/lib/btc-portfolio/live
sudo -u btcspot "$PY" -m btc_portfolio.portfolio_transfer prepare --config "$CONFIG" --credentials-file "$CREDS" --state-dir "$STATE"
sudo -u btcspot "$PY" -m btc_portfolio.migration prepare --config "$CONFIG" --credentials-file "$CREDS" --state-dir "$STATE"
```

이체 미리보기에 `ready=true`, 방향 `CMFUTURE_MAIN`, 금액과 등록 원장이 맞는지 확인한다. 50:50 전환용 실제 이체 명령은 다음과 같다. **응답이 불명확하면 재시도하지 않고** `allocation-transfer-intent.json`, Binance 이체 내역, 두 지갑과 원장을 대조한다.

```bash
sudo -u btcspot "$PY" -m btc_portfolio.portfolio_transfer transfer --config "$CONFIG" --credentials-file "$CREDS" --state-dir "$STATE" --confirm I_UNDERSTAND_PORTFOLIO_BTC_TRANSFER
sudo -u btcspot "$PY" -m btc_portfolio.migration prepare --config "$CONFIG" --credentials-file "$CREDS" --state-dir "$STATE"
```

불명확한 이체의 거래소 내역은 재전송 없이 읽기 전용으로 조회한다.

```bash
sudo -u btcspot "$PY" -m btc_portfolio.portfolio_transfer history --config "$CONFIG" --credentials-file "$CREDS" --state-dir "$STATE"
```

이관 미리보기가 `ready=true`면 기존 `old_identity`를 읽고 **새 백업 파일 경로**를 지정해 동일 원장 안에서 binding만 명시적으로 이관한다. 이관은 등록된 원장·UID·예비 잔고를 검증하고 SQLite 백업을 만든다. 기존 보유 알트와 체결 내역은 그대로 둔다. 이관 직후 실계좌 `prepare`를 다시 실행한다.

```bash
OLD_ID='prepare에_표시된_old_identity'
sudo -u btcspot "$PY" -m btc_portfolio.migration apply --config "$CONFIG" --credentials-file "$CREDS" --state-dir "$STATE" --backup-path /var/backups/btc-portfolio/aggressive-before.sqlite3 --expected-old-identity "$OLD_ID" --confirm I_UNDERSTAND_AGGRESSIVE_LEDGER_MIGRATION
sudo -u btcspot "$PY" -m btc_portfolio prepare --config "$CONFIG" --credentials-file "$CREDS" --state-dir "$STATE"
```

`prepare ready=true`, 서비스 충돌 없음, 현물/COIN-M 원장 일치, 수수료 설정, 손절·청산 거리, 주문 미결 상태를 확인한 뒤에만 사용자가 서비스를 시작한다. 이 문서에는 자동 서비스 재시작 명령을 넣지 않았다.

## 실패 시 처리

- 이체 결과가 불명확하면 `PENDING`을 유지한다. 같은 요청을 다시 제출하지 않는다. 이체 내역과 두 지갑을 수동 대조한다.
- IOC 주문 응답이 불명확하면 client ID로 조회만 한다. 확정 체결 없이 다음 주문을 내지 않는다.
- 청산가와 손절가 간격이 진입가 기준 5%p 미만이거나 실효 레버리지가 3배를 넘으면 신규 진입·증가를 막는다. 이미 체결된 경우 reduce-only 청산 요청과 중단 상태를 기록한다.
- `btc_portfolio` 원장 binding이 이전 값이면 서버에서 이전 버전으로 임의 재시작하지 않는다. 백업과 거래소 계좌를 비교한 뒤 복구 절차를 정한다.
