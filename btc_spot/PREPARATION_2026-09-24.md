# 현물 봇 구현·PC 실행 기록

> **현재 상태 (2026-09-24 22:52 KST 조회):** Binance Spot 거래 권한이 켜졌다.
> PC live 프로세스 실행 중, 차단 사유 없음, 미체결 주문 0건이다.
> 오늘 신호는 BTC 100% 보유로 판단 완료(`NOOP`), 원장 체결 0건·미확정 주문 0건이다.
> 운용 원장 0.003 BTC와 예비분 0.00112273 BTC를 확인했다. 새 명령으로 중복 시작하지 않는다.

아래는 권한을 켜기 전의 이력이다.

> 2026-09-24 후속 실행: PC의 paper 프로세스를 정상 종료하고 live 프로세스를 시작했다.
> Binance API의 `enableSpotAndMarginTrading=false`가 아직 유일한 준비 차단 사유다.
> live 원장 결정 0건, 체결 0건, 운용 지갑 미초기화 상태를 확인했다. 권한을 켜면
> 실행 중인 프로세스가 다음 점검 주기에 자동으로 재확인한다. 중복으로 StartLive를 실행하지 않는다.
> 지연된 서버 시각 조회는 새 샘플 최대 3회 재시도로 보완했고 gateway 테스트를 통과했다.
> live 원장 전략 fingerprint는 `spot-momentum-20-60-120-ioc-v1:3f19dcc79f0f3842b34d9ce0`이다.

2026-09-24 13:36 UTC(한국 시각 22:36) 기준이다.

**실주문 가능한 실행기를 구현했고 이 시점에는 PC paper 모드로 실행 중이었다.**
실제 주문·자금 이체·계좌 권한 변경은 수행하지 않았다.

| 항목 | 결과 |
|---|---|
| 선택한 실행 방식 | BTCUSDT 현물, 20·60·120일 분할 모멘텀, 일 1회 |
| 전체 실제 Spot BTC | 0.00412273 BTC |
| 운용 초기 배정 | 0.003 BTC |
| 실계좌 예비분 | 0.00112273 BTC |
| 실계좌 수수료 조회 | 할인 미적용 taker 0.1% |
| 실제 계좌 미체결 주문 | 없음 |
| 주문 방식 | 가격을 제한한 LIMIT IOC, 가능한 수량만 즉시 체결 |
| 현재 신호 | 세 기간 모두 상승, BTC 목표 100% |
| 현재 예상 주문 | 없음: 이미 BTC를 보유하여 목표 충족 |
| 실거래에 남은 계좌 설정 | `enableSpotAndMarginTrading=false` |

**172개 신규 Spot 테스트를 포함해 전체 745개, subtest 2개 통과**했다.
실행 명령은 다음과 같다.

```powershell
python -m pytest binance_coinm_v1/tests btc_lab/tests btc_spot/tests tests/test_basis_scan.py -q -o addopts=''
```

주문 응답 유실, 부분 체결·만료, 확정 거절 뒤 조회 장애, 전송 전 중단, 실제 수수료 자산,
예비분 유지, 중복 체결 방지, 일별 중복 판단, 상태 폴더 변경과 원장 유실을 검사했다.
가격 급변 중 예비 USDT를 쓰는 문제를 피하려고 무제한 시장가 대신 IOC 가격 한도를 적용했다.
이 테스트는 실제 거래소에 실주문을 제출한 체결 검증은 아니다.

PC에서 현재 공개 시세를 받아 실행한 뒤 **정상 종료→재시작**을 실제로 수행했다.
재시작 결과 `ALREADY_DECIDED`, 원장 BTC 0.003, USDT 0, 체결 0으로 유지되었다.
가상 체결이 0인 이유는 현재 목표가 BTC 100%여서 매매가 필요 없기 때문이다.
매도·재시작·체결 중복 방지 경로는 별도의 모의 거래소 통합 테스트로 확인했다.
과거 `btc_lab.pc`의 COIN-M 관측 프로세스는 정상 종료했고 원장은 보존했다.

상태는 `btc_spot/state/paper/status.json`, 읽기 전용 실제 계좌 점검은
`btc_spot/state/live/readiness.json`에 저장한다. 두 경로는 git에서 제외되어 있다.
paper 지갑에는 배정액 0.003 BTC만 넣으므로 paper 원장의 reserve=0은
실계좌 예비분 0.00112273 BTC가 사라졌다는 뜻이 아니다.

운영 소스 fingerprint:
`spot-momentum-20-60-120-ioc-v1:b61296c2a8b1111cf31b52e0`

다음 실행 절차는 [운영 README](README.md)에 있다.
Binance API 관리의 **Enable Spot & Margin & Stock Trading**을 켠 뒤 `Prepare`로 확인하고,
실제 거래를 시작할 때 `StartLive -Confirm I_UNDERSTAND_LIVE_SPOT`을 사용한다.
추가 7일 대기 조건을 넣지 않았다. 실거래 시작 시에도 계좌·잔액·필터·원장 복구를 다시 확인한다.

AWS는 [서비스 예시와 이전 안내](deploy/README.md)를 준비했다. 리소스는 생성하지 않았다.
현재 PC 단계의 뒤에 진행하며, PC에서 이미 live 원장을 사용했다면 Windows→Linux의
경로 변경을 검증하는 오프라인 재바인딩 도구가 추가로 필요하다.
