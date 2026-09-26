# BTCUSD_PERP 상황 원장

시간마다 받던 Gemini 리포트와 수기 원장을 **거래소 실측값과 자동 채점**으로 대체한다.
5분 봉이 끝날 때마다 공개 Binance API로 스냅샷을 만들어 SQLite에 저장한다.
1·2·4시간 뒤에는 실제 가격으로 예측을 채점한다. 매시 정각에는 한국어 요약을 텔레그램으로 보낸다.
API 키를 쓰지 않고 주문을 내지 않는다. 코드 [`ledger.py`](ledger.py), 검사 [`tests/test_ledger.py`](tests/test_ledger.py).

## 기록하는 항목

| 원장 항목 | 출처 |
|---|---|
| Mark·Index·프리미엄, 예상 펀딩·다음 펀딩 시각 | `dapi/v1/premiumIndex` |
| 일봉 시가(09시 KST), 당일 VWAP, ATR 5분·15분·1시간 | COIN-M 5분봉 (완료 봉만) |
| COIN-M OI (계약·BTC) | `dapi/v1/openInterest` 실시간 |
| USDT-M OI와 15분·1시간·4시간 변화 | `futures/data/openInterestHist` |
| 테이커 B/S 5분·15분·1시간 (USDT-M, COIN-M) | 5분봉의 테이커 매수량 |
| 상위 트레이더 포지션 롱 비중·L/S·1시간 변화, 전체 계정 L/S | USDT-M `futures/data` 통계 |
| 직전 펀딩 | `dapi/v1/fundingRate` |
| 상황 (추세·변동성·흐름·OI, 36개) | 연구와 같은 개발 구간 경계 |
| 1시간·2시간 예측 | 연구 모델(LightGBM)을 전체 이력으로 학습해 JSON 트리로 내보낸 것 |
| swing COIN-M 방향 | 4시간 EMA20/80 (`btc_portfolio` 규칙과 같은 계산) |
| 실제 1·2·4시간 수익, 7일·30일 적중률과 IC | 이후 봉으로 자동 채점 |

시트에 있던 총 시장 OI, 호가 불균형, 청산 매물대는 믿을 만한 공개 출처가 없어 넣지 않았다.
점수·TP/SL·"LONG HOLD" 판정도 넣지 않았다. [검증](REGIME_SWITCH_2026-09-26.md)에서 그 점수는 비용 전에도 예측력이 없었다.
포지셔닝은 USDT-M 값을 쓴다. COIN-M 통계가 며칠씩 같은 값에 멈춰 있는 것을 확인했기 때문이다.

모델 예측은 **체결 시점 참고**로 표시한다. 2시간 예측의 우위는 왕복 비용의 절반 정도여서 단독 진입 근거가 아니다.
[체결 시점 조정 검증](TIMING_OVERLAY_2026-09-26.md)에서는 거래당 약 1~3bp 개선이었다.

## 실시간 값과 백테스트 값의 일치

- metrics API의 T시각 행은 아카이브의 T−5분 행과 같다. 원장은 API 시각에서 5분을 빼서 배치하고, 연구와 같은 1봉 지연을 적용한다.
- API는 롱숏 비율을 소수점 4자리로 반올림한다. 원장 모델은 아카이브 비율도 4자리로 반올림해 학습했다.
- 2026-09-24 23:55 UTC 기준 네 시점(0·5분·1시간·8시간 20분 전)에서 실시간 API로 만든 입력의 예측이
  아카이브 입력의 예측과 **완전히 같았다**. 아카이브 펀딩 캐시에서 빠진 09-24 16:00 한 건은 보정했다.
- JSON 트리 평가 결과는 LightGBM 예측과 최근 3,000행에서 오차 0이다. 서버에는 LightGBM이 필요 없다.

## PC에서 쓰기

```powershell
python -m btc_lab.ledger train                      # 모델 학습·내보내기 → btc_lab/state/ledger_model/
python -m btc_lab.ledger once                       # 스냅샷 1회 + 리포트 출력 (텔레그램 없음)
python -m btc_lab.ledger run                        # 5분마다 반복, 매시 리포트를 화면에 출력
python -m btc_lab.ledger export --out ledger.csv    # 구글 시트로 가져갈 CSV
```

## 서버 설치 (사용자 실행)

매매 서비스는 멈추거나 바꾸지 않는다. 새 파일은 `btc_lab`의 연구 모듈 3개와 모델, 서비스 파일뿐이며 매매 봇의 코드 식별 해시 대상(`btc_portfolio/config.py`의 `identity`)과 겹치지 않는다. 패키지는 원장 전용 가상환경 `/opt/btc-ledger/.venv`에 설치해 매매 봇 환경을 건드리지 않는다.

1. PC에서 묶음을 만든다: `python btc_lab/deploy/build_ledger_bundle.py` → `btc_lab/deploy/btc-ledger-bundle.zip`
2. AWS CloudShell(서울)에 `btc-ledger-bundle.zip`과 `btc_spot/deploy/ssm_upload.py`를 올리고 전송한다.
   ```sh
   python3 ssm_upload.py btc-ledger-bundle.zip i-0ef98450609acb433 --destination /tmp/btc-ledger-bundle.zip   # 해시 확인
   ```
3. 세션 관리자로 서버에 접속해 설치 스크립트를 실행한다. 스크립트는 다음 일을 한다.
   - 해시 확인 후 압축 해제
   - 원장 전용 가상환경 생성과 패키지 설치
   - 텔레그램 설정만 담은 `/etc/btc-spot/ledger-telegram.env` 생성
   - 텔레그램 없이 스냅샷 1회 시험(실패하면 서비스를 등록하지 않음)
   - 서비스 등록

   서비스 시작은 하지 않는다.
   ```sh
   sudo python3 -c "import zipfile; open('/tmp/install-ledger.sh','wb').write(zipfile.ZipFile('/tmp/btc-ledger-bundle.zip').read('btc_lab/deploy/install-ledger.sh'))"
   sudo sh /tmp/install-ledger.sh /tmp/btc-ledger-bundle.zip
   ```
4. 시험 출력에 한국어 리포트가 보이면 시작한다.
   ```sh
   sudo systemctl enable --now btc-ledger
   journalctl -u btc-ledger -n 20 --no-pager
   ```
   원장은 `/var/lib/btc-ledger/ledger.sqlite3`, 상태는 `status.json`이다. 다음 정각부터 텔레그램 리포트가 온다.
   CSV가 필요하면 서버에서 `cd /opt/btc-spot && sudo -u btcspot /opt/btc-ledger/.venv/bin/python -m btc_lab.ledger export --state-dir /var/lib/btc-ledger --out /var/lib/btc-ledger/ledger.csv`를 실행한다.

## AWS 배포 기록 (2026-09-26)

- 로컬 AWS CLI 로그인(계정 `350469506331`)으로 SSM을 통해 전송·설치했다. 묶음 SHA-256 `f10d384ff43f4c3234431dad9a9e3634aee90e58dba9b9feae77ce1205d18e56`,
  서버 경로 `/tmp/btc-ledger-bundle.zip`. 기존 `/tmp/btcspot-stage.zip`은 건드리지 않았다.
- 새 파일은 `btc_lab/ledger.py`, `btc_lab/regime_switch.py`, 모델, 배포 파일이다. `btc_lab/strategy_search.py`는 서버 파일과 해시가 같았다(`f048ef08…`).
- 원장 전용 가상환경 `/opt/btc-ledger/.venv`(Python 3.12.3)를 쓴다. 텔레그램 설정은 `/etc/btc-spot/ledger-telegram.env`(root:btcspot 0640)에 둔다.
- 설치 스크립트의 시험 스냅샷이 서버에서 정상 출력된 뒤 `systemctl enable --now btc-ledger`로 시작했다.
  15:10·15:15 KST 봉을 기록했다(정상 주기는 봉 마감 20초 뒤). `metrics_fresh=true`, 메모리 약 68MB, 재시작 0회였다.
- 설치 전후 모두 `btc-portfolio`와 `btc-portfolio-notify`가 `active`였다. 매매 봇의 코드 식별 해시 대상 파일은 바꾸지 않았다.
- 첫 텔레그램 리포트는 16:00 KST다. 오지 않으면 `journalctl -u btc-ledger -n 30`에서 오류 종류를 확인한다.

## Gemini 섀도 판단 (2026-09-26 추가)

사용자 요청으로 [`llm_judge.py`](llm_judge.py)를 붙였다. 15분 경계 봉마다 원장 실측 스냅샷과 최근 48시간·3일 흐름을 Gemini(`gemini-3.6-flash`)에 보낸다.
Gemini는 1시간·4시간 롱/숏/관망과 확신도, 짧은 이유를 JSON으로 답한다. **주문은 내지 않는다(섀도).**

- **입력 제한:** 주어진 숫자만 쓰라고 지시한다. 이전에 받던 Gemini 리포트에는 지어낸 수치가 있었다.
- **채점:** 원장 스냅샷의 실제 1시간·4시간 수익으로 매긴다. 같은 시각의 "항상 롱"과 swing 추세 방향 적중률을 함께 적는다. 매시 텔레그램 리포트에 최신 판단과 7일 채점이 붙는다.
- **실패 처리:** 호출 실패는 오류 종류만 기록한다. 응답 본문은 요청을 인용할 수 있어 남기지 않는다.
- **키 관리:** 키는 `/etc/btc-spot/ledger-gemini.env`(`GEMINI_API_KEY`, 선택 `GEMINI_MODEL`, root:btcspot 0640)에 둔다. 파일이 없으면 판단만 꺼진다.
- **실거래 연결 조건:** 충분한 표본에서 두 기준보다 적중률이 높을 때만 논의한다.

**배포 기록 (2026-09-26):**
- **키 전달:** Gemini 키는 서버에서 만든 일회용 RSA 키로 이 PC에서 암호화해 보냈고, 서버에서 복호화한 뒤 일회용 개인키를 삭제했다(`shred`).
- **첫 시험 실패:** `maxOutputTokens=1024`가 생각 토큰(약 900~960)에 거의 다 쓰여 답이 잘렸다(JSON 오류). 한도를 8192로 올리고, 종료 사유가 `STOP`이 아니면 오류로 기록하게 고쳤다.
- **첫 기록:** 서비스는 11:30 UTC 봉부터 판단을 기록했다(숏/숏, 확신 0.58).
- **판단 흔들림:** 같은 입력으로 1분 안에 세 번 물었을 때 답이 롱/롱 → 숏/관망 → 숏/관망으로 바뀌었다. 판단이 흔들린다는 점을 채점할 때 함께 본다.

**과거 검증과 종료 (2026-09-26):** [`llm_backtest.py`](llm_backtest.py)로 사전에 고정한 조건에서 시험했다.
2026-08-01 ~ 09-24의 매시 1,317개 판단이다. 세 모델 모두 이 기간을 학습하지 못했다(3.6 Flash의 마지막 업데이트가 2026년 7월).
입력은 실시간과 같은 형식으로, 날짜·가격·OI 수준 없이 변화율만 넣었다.

| 모델 | 4시간 적중 | 4시간 평균 방향수익 | 1시간 적중 | 판정 |
|---|---:|---:|---:|---|
| gemini-3.6-flash (생각 과정 사용) | 49.3% | +3.0bp | 46.5% | 불합격 |
| gemini-3.5-flash-lite | 49.6% | +4.4bp | 47.1% | 불합격 |
| gemini-3.1-flash-lite | 49.8% | +3.8bp | 47.1% | 불합격 |
| 기준: 항상 롱 | 54.1% | +8.6bp | 51.1% | – |
| 기준: swing 추세 방향 | 49.9% | +3.2bp | 50.3% | – |

합격 기준은 4시간 적중률이 두 기준보다 3%p 이상 높고, 평균 방향수익이 swing보다 높은 것이었다. 모두 불통과했다.
세 모델 모두 "항상 롱"보다 못했고, 1시간 판단은 50%에도 못 미쳤다. 가장 비싼 모델이 가장 낮았다. 비용은 약 $7였다.
합의한 대로 실시간 판단을 끄고 서버의 키 파일을 삭제(`shred`)했다. 원장과 매매 봇은 영향 없이 계속 동작한다.
1시간 넘게 지난 AI 판단은 텔레그램에 표시하지 않는다.

## 월간 자동 재학습 (2026-09-26 추가)

[`retrain.py`](retrain.py)가 매월 3일 03:30 UTC(±15분)에 `btc-ledger-retrain.timer`로 돈다. 바꾸는 것은 원장이 쓰는
1시간·2시간 모델뿐이다. 매매 규칙은 바꾸지 않는다. 절차는 연구에서 검증한 방식(매월 이전 전체 자료로 재학습)과 같다.

1. 지난달 말까지의 공개 아카이브(BTCUSD_PERP 5분봉, 흐름·포지셔닝, 펀딩)를 `/var/lib/btc-ledger/retrain`에 받는다.
2. 점검용 모델: 지난달을 빼고(하루 간격) 학습한 뒤 지난달을 겹치지 않는 창으로 예측한다.
3. 결과를 보기 전에 고정한 관문:
   - 피처 동일, 트리 평가 일치
   - 아카이브 완전
   - 현재 모델보다 최신 자료로 학습
   - 점검 2시간 IC ≥ −0.05
   - 현재 모델과의 예측 상관 ≥ 0.3
   한 달은 독립 2시간 창이 약 360개뿐이므로, 이 관문은 데이터나 계산의 고장을 거르는 장치다. 최근 성과로 모델을 고르지 않는다.
4. 통과하면 지난달까지 전부로 학습한 모델로 `/var/lib/btc-ledger/model/ledger_model.json`을 원자적으로 교체한다.
   이전 모델은 `history/`에 남긴다. 원장은 파일 변경을 감지해 재시작 없이 새 모델을 쓴다.
5. 보고서를 `retrain/reports/`에 남기고 텔레그램으로 판정을 보낸다. 실패하면 기존 모델을 유지하고 실패를 알린다.

재학습 서비스는 원장 전용 가상환경에 LightGBM·scikit-learn·scipy를 설치해 쓴다.
LightGBM에 필요한 `libgomp`는 `apt`의 서명된 목록과 SHA-256을 대조한 `.deb`를 `/opt/btc-ledger/gomp`에 풀어 이 서비스만 쓴다.
시스템 패키지와 매매 봇 환경은 바꾸지 않는다. 제한은 메모리 900MB, nice 10, 1스레드다.

**배포 기록 (2026-09-26):** 서버 시험 실행(`--dry-run`, 8월 점검)이 97초에 끝났다. 점검 2시간 IC +0.0258(n=371),
1시간 IC +0.0609(n=743), 현재 모델과 예측 상관 0.885로 PC 결과와 같았다. 메모리 최대 725MB였고 판정은 "최신 자료 아님"이라 교체하지 않았다.
새로 받은 자료는 연구 자료와 겹치는 구간에서 바이트 단위로 같았다. 첫 정기 실행은 2026-10-03 03:39 UTC로 잡혔다.
같은 날 원장 첫 정각 리포트(16:00 KST)가 텔레그램으로 발송됐다.

## 한계와 다음 단계

- 재학습 메모리는 자료가 한 달 늘 때마다 약 12MB씩 커진다. 725MB에서 시작했으므로 1년쯤 뒤에는 900MB 상한에 가까워진다. 그 전에 상한이나 학습 방식을 다시 정해야 한다.
- 매월 3일에 지난달 아카이브가 아직 없으면 교체하지 않고 다음 달에 다시 시도한다.
- 서비스가 3일 넘게 멈추면 그 구간의 스냅샷은 채점하지 못한다(최근 1,000개 5분봉만 받는다).
- 채점 IC는 겹치는 표본으로 계산한다. 표시된 n만큼 독립적인 증거는 아니다.
- 뉴스, 청산 스트림, 호가 깊이는 아직 기록하지 않는다. 이 항목들은 과거 자료가 없으므로, 원장에 쌓기 시작해야 나중에 검증할 수 있다.
