# 공격형 COIN-M 체결 시점 조정 배포 (2026-09-26)

사용자 요청으로 [체결 시점 조정](../../btc_lab/TIMING_OVERLAY_2026-09-26.md)을 실거래 공격형 봇에 적용했다.
규칙과 안전장치는 [`timing.py`](../timing.py)에 있다.

## 규칙

- 대상: EMA 방향 전환의 청산·반대 진입, 무포지션의 새 진입.
- 매수는 상황 원장의 2시간 예측 ≥ 0, 매도는 ≤ 0일 때 실행한다.
- 신호가 처음 나온 4시간 봉 마감에서 최대 4시간 기다린 뒤에는 조건과 무관하게 실행한다.
  대기 시작 시각은 원장 `timing_wait`에 저장해 재시작이나 새 4시간 봉에도 밀리지 않는다.
- 예측 파일(`/var/lib/btc-ledger/prediction.json`)이 없거나 10분 넘게 오래됐거나 읽을 수 없으면 기다리지 않는다.
- 비상 청산, 재난 손절, 킬 스위치, 일일 계약 수 조정, 알트 순환은 즉시 실행한다.

## 새 도구

- `btc_portfolio.code_update`: 포지션을 유지한 채 코드·설정 변경을 원장에 반영한다.
  - 기존 `migration`은 스윙→공격형 전환용이라 무포지션이 필요하고 킬 스위치 기준 자산을 다시 잡는다.
  - 이 도구는 런타임의 읽기 전용 준비 점검을 그대로 돌리고, 남은 문제가 식별값 변경 하나뿐일 때만 진행한다.
  - 추가로 서비스 정지, 미확정 주문 0건, 원장 계약 수와 거래소 포지션 일치, 원장 손절 주문의 거래소 존재를 확인한다.
  - 원장을 백업한 뒤 식별값만 바꾸고 `code_rebind` 이벤트를 남긴다.
- `deploy/deploy-timing.sh`: 서버 파일이 검토한 바탕인지 해시로 확인한 뒤 적용한다. 정지 이후 어느 단계든 실패하면 파일·설정·식별값을 되돌리고 봇을 다시 켠다.

## 배포 기록

- 06:42 UTC 읽기 전용 점검: 서버 `aggressive.py`(`bf24c655…`), `config.py`(`00a130b1…`)가 검토한 바탕과 일치했다.
  `engine.py` 등 나머지 코드 식별 대상도 로컬과 일치했다. 정지 없음, 미확정 0건, 3계약, 손절 `bsg_f86548f4250802cff65a2e9b4961`, READY 롱이었다.
- 묶음 SHA-256 `bf21299b62a9259951dcb3ae85da61b340a4c5460857f667e376c78ddd65c513`, 서버 `/tmp/btc-timing-bundle.zip`.
- 06:43:53 UTC 파일·설정 백업 `/var/backups/btc-portfolio-timing/20260926T064353Z`.
  `btc-portfolio`를 정지하고 파일 5개를 적용했다. 설정에 `timing_prediction_path`, `timing_max_wait_seconds=14400`을 추가하고 `btc-ledger`를 재시작했다.
- `code_update prepare`: blockers 없음. `apply`: 식별값 `0d3ae7b5…` → `8968494a…`, 원장 백업 `/var/lib/btc-portfolio/live/ledger-before-timing-20260926T064400Z.sqlite3`.
- 런타임 `prepare`는 `ready=true`, blockers 없음이었다. 06:44:03 UTC에 봇을 시작했다.
- 시작 후 3분 동안 평가 6회 모두 READY, 재시작 0회였다. 3계약·손절 주문·지갑이 그대로였고 `prediction.json`이 5분마다 갱신됐다.

## 되돌리기

1. 서비스를 멈춘다.
2. 백업 폴더의 `aggressive.py`·`config.py`·`ledger.py`·`portfolio.json`을 원위치에 복사하고 `timing.py`·`code_update.py`를 지운다.
3. 원장 `binding`을 이전 식별값으로 되돌린다. `deploy-timing.sh`의 `rollback`과 같은 트랜잭션을 쓴다.
4. 봇을 시작한다.
