# 공격형 AWS 배치 확인

## 2026-09-25 수정 후 재시작 검증

사용자가 CloudShell에서 `systemctl enable --now btc-portfolio`를 실행했다. `btc-portfolio`와 `btc-portfolio-notify`는 모두 `active/running`, 재시작 횟수 0이었다. 재시작 후 원장 상태는 `aggressive/READY`, halt 없음, 미확정 주문 0이었다. Binance 실제 BTCUSD_PERP 포지션은 롱 3계약이고, 열린 보호주문은 정확히 1건이다. 원장과 Binance의 clientAlgoId `bsg_f86548f4250802cff65a2e9b4961`, `STOP_MARKET`, SELL, 손절가 `$74,704.6`, `closePosition=true`, `MARK_PRICE`, 상태 `NEW`가 일치했다. 현물 원장은 SOL `1.04994900`, BTC `0.00000283`이며 다른 대상 알트는 0이다. 새 선물·현물 체결과 전략 알림 4건의 텔레그램 전달 기록도 확인했다. 보호주문 생성 90초 뒤 추가 평가 주기에서도 `READY`, halt 없음, 포지션 3계약·동일 보호주문 1건이 유지됐고 상태 갱신은 1초 전이었다. 이는 **초기 가동 확인**이며 장기간 무중단·수익성 검증을 뜻하지 않는다.

## 2026-09-25 첫 실행 및 보호주문 수정

사용자가 `btc-portfolio`와 `btc-portfolio-notify`를 시작했다. 첫 점검에서 BTCUSD_PERP 롱 3계약이 체결됐지만, 거래소에 접수된 재난손절 주문을 즉시 조회해 확인하지 못해 봇이 reduce-only 시장가로 3계약을 비상 청산하고 `protection_failed_emergency_close_requested`로 정지했다. Binance 주문 이력에서 손절 주문 `bsg_d6c1f1288dbe899290e879279156`은 접수 뒤 비상 청산 시 `EXPIRED` 상태가 됐음을 확인했다. 진입·청산 수수료 합계는 약 0.00000352 BTC였다.

거래 엔진을 `inactive/dead`, `disabled`로 중지하고 Binance의 실제 포지션·열린 주문·열린 조건부 주문이 모두 0인지 확인했다. 보호주문은 **같은 clientAlgoId를 최대 5회 조회**하며, 재전송하지 않는다. 끝내 확인되지 않으면 기존처럼 비상 청산하고 차단한다. 로컬 `btc_portfolio/tests`와 `binance_coinm_v1/tests` 전체가 통과했다. AWS `/opt/btc-spot/btc_portfolio/engine.py`와 로컬 파일의 SHA-256이 `6d992bf345fbf44d1c32dd8d44cd21adf61f515d655535cfcec0ce40a99593fb`로 일치하고 컴파일도 통과했다. AWS 이전 소스는 `/var/backups/btc-portfolio-aggressive/engine-before-stop-visibility-hotfix.py`에 보존했다.

코드가 원장 식별값에 포함되므로, 이전 소스의 해시가 기존 원장 binding과 일치함을 검증했다. 서비스 정지, 거래소·원장 무포지션, 미확정 주문 0, 현물 잔고 대조를 확인한 뒤 SQLite를 `/var/lib/btc-portfolio/live/ledger-before-stop-hotfix-20260925T113327Z.sqlite3`에 백업하고 binding을 새 코드로 이관하면서 halt를 해제했다. 이후 실계좌 읽기 전용 `prepare`는 `ready=true`, 차단 사유 0, 현재 0계약·목표 3계약이었다. 텔레그램 원장에는 비상 청산과 BLOCKED 상태 전달 기록이 각각 1건 있다. 당시 거래 서비스는 중지 상태였고, 이후 위 재시작 검증을 완료했다.

> 아래 최초 배치 기록보다 나중에 진행된 전환 상태: COIN-M→Spot `0.00029626 BTC` 이체 성공(Binance ID `414883269374`, 원장 기록 완료). 공격형 원장 이관은 `MIGRATED`였고 `/var/lib/btc-portfolio/live/ledger-before-aggressive-migration-20260925.sqlite3`에 추가 백업했다. 활성 설정 `/etc/btc-spot/portfolio.json`은 공격형 50:50·변동성 배율·월간 재배분 알림으로 변경했다. 이 상태에서 실계좌 읽기 전용 `prepare`는 `ready=true`, 차단 사유 없음, 주문·이체 0건이었다. 현물/COIN-M 평가액은 약 `0.00150395/0.00150374 BTC`, SOLBTC 1위, COIN-M 롱, 목표 3계약(예상 실효 2.345배), 재난손절가 약 `$74,880`, 추정 청산가 약 `$65,414`였다. BNB 수수료 할인은 꺼져 있다. 마지막 확인에서 거래·알림 서비스 모두 `inactive/dead`, `disabled`였다. **공격형 실거래 서비스 시작과 시작 후 거래소·보호주문·알림 검증은 아직 남았다.**

2026-09-25 AWS에서 확인했다. 새 코드를 실행 경로에 반영했지만 **공격형 실거래는 시작하지 않았다.**

- 배치 경로: `/opt/btc-releases/aggressive-fea301d14b703f51`
- 소스 178개와 manifest SHA-256 대조 완료.
- ZIP SHA-256: `d2a54b432909566db73f4b1f4fa6c1e02c08c698e3b3132ae9a9226c348a583e`
- 11:10 UTC에 원장·registry·설정·기존 소스를 `/var/backups/btc-portfolio-aggressive/20260925T111027Z`에 백업했다. SQLite 무결성은 `ok`였다.
- manifest의 178개 파일을 `/opt/btc-spot`에 반영했고 Python 컴파일을 통과했다. 원장·registry·자격증명·가상환경은 덮어쓰지 않았다.
- `btc-portfolio`와 `btc-portfolio-notify`는 정상 정지하고 비활성화했다. 기존 `spotlive`도 inactive/dead다.
- 현재 실행 설정: `swing`. 원장 무결성 `ok`, registry와 계좌 일치, 현물 원장 대조 통과, 미확정 주문 0, halt 없음.
- 보유 운용분: BTC 0.00091844, SOL 0.20279700, COIN-M BTC 0.0018. 예비 BTC 0.00112273은 운용에서 제외했다.

## AWS 읽기 전용 미리보기

새 릴리스 코드와 서버의 기존 Python 환경을 사용했다. 거래소 요청은 조회만 허용했고 SQLite는 `mode=ro`로 열었다. 결과는 시세에 따라 달라진다.

| 항목 | 조회 결과 |
|---|---|
| 알트 1위 / COIN-M 방향 | SOLBTC / 롱 |
| σ / 목표 배율 | 0.007472125879531353 / 3.0 |
| 현물 / COIN-M 운용 평가액 | 0.0012089061431 / 0.0018 BTC |
| 50:50 필요 이체 | COIN-M→Spot 0.00029554 BTC (실행 명령이 다시 계산·대조) |
| 재배분 후 목표 / 실효 배율 | 3계약 / 약 2.3451배 |
| 예상 진입 / 재난손절 | $85,032.90 / $74,828.9 |
| 추정 청산가 | $65,369.04 (현재 무포지션이므로 실제 청산가 없음) |
| 마진·거래소 설정 | 격리, 원웨이, 3배 |
| BNB 현물 수수료 할인 | 꺼짐 (`spot_bnb_burn=false`) |
| Universal Transfer 권한 | 켜짐 |

`portfolio_transfer prepare`와 `migration prepare`는 모두 `ready=true`, 차단 사유 0개다. 자금 이체·원장 binding 이관·서비스 시작은 아직 실행하지 않았다.

## 사용자가 실행할 전환 순서

자세한 이체·이관 명령은 `btc_portfolio/AGGRESSIVE.md`를 따른다. 아래 사항은 이번 별도 배치 경로에 맞춘 추가 조건이다.

1. Binance 현물의 **BNB로 수수료 지불**을 끈다. 출금 권한은 켤 필요가 없다.
2. `btc-portfolio`와 알림 서비스를 정상 정지하고 미확정 주문·COIN-M 포지션·보호주문이 없는지 다시 확인한다. 현재 조회에서 0이더라도 전환 시점에 재확인한다. 포지션이 생겼다면 먼저 정리 방법을 결정한다.
3. `/var/lib/btc-portfolio/live/ledger.sqlite3`는 SQLite backup API로, `/opt/btc-spot/btc_portfolio/state/live-registry.json`과 `/etc/btc-spot/portfolio.json`은 파일 복사로 백업한다. 기존 소스도 함께 백업한다.
4. 서비스가 정지된 상태에서 릴리스 manifest의 파일만 `/opt/btc-spot`에 반영한다. `state`, 원장, registry, `.env`, 기존 가상환경은 지우거나 덮어쓰지 않는다. **별도 릴리스 경로에서 새 live 원장을 만들지 않는다.**
5. 설정 예시를 `/etc/btc-spot/portfolio-aggressive.json`에 준비한다. 기본값은 `spot_fraction=0.5`, `leverage_mode=vol`, `rebalance_mode=alert`다. 매월 자동 이체까지 원하면 사용자가 `rebalance_mode=auto`를 선택한다. 백테스트의 매월 재배분 결과는 `auto`에 대응하며 `alert`는 이체를 실행하지 않는다.
6. 작업 디렉터리를 `/opt/btc-spot`으로 옮겨 기존 원장 경로를 사용한 `portfolio_transfer prepare`와 `migration prepare`를 실행한다. 최신 실제 잔고로 이체량을 재계산한다.
7. 사용자가 확정한 이체를 한 번만 실행하고 결과를 대조한다. 응답 불명확 시 재전송하지 않고 `portfolio_transfer history`로 조회한다.
8. `migration apply`로 같은 원장 안에서 binding을 이관하고 공격형 `prepare`의 `ready=true`를 확인한다. 이관이 끝난 원장을 구버전 swing으로 다시 시작하지 않는다.
9. **기존 systemd 서비스는 `/etc/btc-spot/portfolio.json`을 읽는다.** 성공한 사전 점검에 사용한 설정을 이 활성 설정 경로에 복사해야 한다. 별도 `portfolio-aggressive.json`만 만들면 서비스에 반영되지 않는다.

```bash
# ready=true 확인 후 사용자가 직접 실행한다.
sudo install -o root -g btcspot -m 0640 /etc/btc-spot/portfolio-aggressive.json /etc/btc-spot/portfolio.json
sudo systemctl start btc-portfolio
sudo systemctl start btc-portfolio-notify
sudo systemctl show btc-portfolio btc-portfolio-notify -p ActiveState -p SubState -p NRestarts
```

기존 unit은 `/opt/btc-spot`과 같은 원장을 사용하므로 새 unit을 만들거나 동시에 두 봇을 실행하지 않는다. 서비스 시작 이후에는 계좌 대조, 보호주문, 첫 평가 결과와 텔레그램 전달을 다시 확인한다. 이 문서의 전환 명령은 이번 배치 작업에서 실행하지 않았다.
