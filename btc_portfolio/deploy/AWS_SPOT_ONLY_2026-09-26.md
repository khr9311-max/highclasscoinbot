# 현물 전용 전환 배포 (2026-09-26)

사용자 요청으로 실거래 봇의 선물(COIN-M) 기능을 껐다. 봇은 BTC↔알트 현물 순환만 한다.
COIN-M 지갑은 사용자가 직접 매매하므로 봇이 조회하지도 주문하지도 않는다. COIN-M에 남은 BTC는 옮기지 않았다.

## 코드

- 설정 `coinm_managed: false`
  - 필요 조건: 공격형 모드, `spot_fraction` 1, 체결 시점 조정 없음, 재배분은 알림만.
- `venues.py`: COIN-M 개인 API 요청(계좌·포지션·주문·손절·수수료)을 하지 않는다.
  - COIN-M 주문·취소 요청은 전송 전에 거부한다.
  - 공개 시세(서버 시각, BTC 달러 기준가)는 계속 쓴다.
- `engine.py`: 현물만 점검한다.
  - 원장이 COIN-M 계약·손절·미확정 주문을 하나라도 갖고 있으면 매매하지 않는다.
  - 자산 평가에서 COIN-M을 뺀다.
- `aggressive.py`: COIN-M 청산·진입·조정·재배분·안전거리 검사를 건너뛴다.
  - 킬 스위치 기준은 원장 `spot_only_initial_equity`에 새로 잡는다. 이전 `aggressive_initial_equity`는 보존한다.
- `code_update.py`: 현물 전용이면 거래소 포지션 대신 "원장이 COIN-M에 아무것도 갖고 있지 않음"을 확인한다.
- `runtime.py`의 `status.json`에 `coinm_managed`를 남긴다.
  - 알림과 원장 텔레그램은 이 값을 보고 "선물은 직접 관리"로 표시한다.
  - 자산 줄은 "봇 자산(현물)"으로 바뀐다.
- 검사: [`tests/test_spot_only.py`](../tests/test_spot_only.py).
  - COIN-M 개인 호출이 한 번이라도 일어나면 실패하는 가짜 거래소로 확인한다.

## 배포 기록

1. **12:46 UTC, 1단계:** 기존 코드 그대로 봇이 스스로 청산하게 했다.
   - `spot_fraction`을 1로 바꾸고 시점 조정을 껐다(`code_update`로 식별값 재결합).
   - 롱 3계약을 84,001.3달러에 감소 전용으로 청산했다(실현 −0.0000375 BTC, 수수료 0.0000018 BTC).
   - 다음 주기에 손절 `bsg_f86548f4250802cff65a2e9b4961`을 취소했다.
   - 확인: 거래소 포지션 0, 조건부 주문 0, 미체결 0, COIN-M 지갑 0.00145928 BTC.
   - 백업 `/var/backups/btc-portfolio-spotonly/20260926T124629Z`.
2. **12:55 UTC, 2단계:** [`deploy-spot-only.sh`](deploy-spot-only.sh)로 적용했다.
   - 서버 파일 8개가 검토한 바탕(해시)과 일치하고, 원장이 COIN-M을 갖고 있지 않은지 먼저 확인했다.
   - 봇을 정지하고 파일을 적용한 뒤 `coinm_managed: false`로 바꿨다.
   - 재결합하고 준비 점검을 통과했다(`coinm: manual`). 봇을 켜고 알림·원장을 재시작했다.
   - 번들 SHA-256 `a726d16eab5e61e60efe12977a9803484d355917f5f6b549102296740de0297c`.
   - 백업 `/var/backups/btc-portfolio-spotonly/code-20260926T125552Z`.
3. **13:00 UTC 확인:**
   - READY, 봇 자산 0.001515 BTC(SOL 보유), 배포 후 런타임 오류 0건, 재시작 0회.
   - 거래소 COIN-M은 포지션 0, 지갑 0.00145928 BTC 그대로였다.
   - 킬 스위치 기준 0.0015144 BTC.

## 되돌리기

- 되돌리려면 백업의 파일과 `portfolio.json`을 복원하고 식별값을 이전 값으로 되돌려야 한다(스크립트의 `rollback`과 같은 절차).
- COIN-M을 봇이 다시 운용하려면 사용자의 수동 포지션이 없어야 한다. 원장 계약 수 0과도 맞아야 한다.
