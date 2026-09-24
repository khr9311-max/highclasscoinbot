# AWS 이전 현황 (2026-09-25 KST)

- 계정 `350469506331`, 서울 `ap-northeast-2`, 인스턴스 `binancebot` (`i-0ef98450609acb433`), Elastic IP `3.39.78.13`.
- Ubuntu 24.04 / t3.small. 루트는 암호화된 gp3 20 GiB `vol-0ed6aba1e503b9a4f`. 교체 작업 `replacevol-0861fb21f7d257924` 성공. 원래 암호화되지 않은 볼륨 `vol-0c20bc03cedff5134`는 복구용으로 보존했다. 키 파일은 교체 후에 배치했으므로 이전 볼륨에는 없다.
- EC2 역할 `btcspot-ssm`: `AmazonSSMManagedInstanceCore`만 연결. SSM 온라인. 보안 그룹 `sg-022850bf1ebad8bf6`: 인바운드 없음, 아웃바운드 TCP 443만 허용.
- `/opt/btc-spot`에 소스와 Python 3.12 가상환경 설치. 전략 fingerprint `spot-momentum-20-60-120-ioc-v1:3f19dcc79f0f3842b34d9ce0`으로 PC와 일치.
- `/etc/btc-spot/binance.env`는 루트 소유, `btcspot` 그룹 읽기(`0640`)이며 상위 폴더는 `0750`. API·텔레그램 설정은 일회용 공개키로 암호화해 전달했고, 복호화용 일회용 개인키는 제거했다.
- Binance 키 `binancebot`에 `3.39.78.13` 허용 IP가 추가된 것을 확인했다. AWS의 읽기 전용 `prepare`가 통과했다.
- PC live와 PC 텔레그램 동반 프로세스를 정상 종료했다. PC 원장을 SQLite Backup API로 일관되게 복사하고 registry와 함께 일회용 공개키로 암호화하여 서버에 전송했다. 전송 해시를 확인하고 일회용 개인키를 폐기했다.
- `migrate_live` 사전 검사와 적용 성공. 원장 UUID `401065bd-db28-4bfe-86fc-9418dc2a9b9f`, 결정 1건, 체결 0건, 배정액 0.003 BTC를 보존했다. 서버 대상은 `/var/lib/btc-spot/live/ledger.sqlite3`와 `/opt/btc-spot/btc_spot/state/live-registry.json`이다.
- 이전 후 AWS `prepare`: `ready_for_explicit_live_start=true`, blockers 없음, 열린 주문 0건. 계좌·원장 정합성 통과.
- 사용자가 `spotlive.service`를 직접 시작했다. 2026-09-24 15:23:42 UTC부터 `active/running`, 재시작 0회. 최신 심박, `mode=live`, `ALREADY_DECIDED`, 미확정 주문 0건, 차단 요인 없음. PC 봇은 종료 상태로 재확인했다.
- `spotnotify.service` 시작 성공. 2026-09-24 15:25 UTC에 텔레그램 현황 알림 `status:2026-09-25` 발송 성공 로그를 확인했다. 두 서비스 모두 **active**이나 재부팅 자동 시작은 아직 **disabled**.

남은 단계: 재부팅 시 자동 시작을 사용자 직접 설정한 뒤 재확인하고, 다음 UTC 일간 결정·체결을 관측한다. PC를 다시 시작하지 않는다. PC IP 제거는 AWS 안정화 후에만 한다.

암호화 전의 원본 스냅샷 `snap-00a7050ff969dce17`과 암호화 복사본 `snap-00e77f03e7dd9efb8`은 복구를 위해 당분간 보존한다. 불필요해진 볼륨·스냅샷은 정상 운영과 백업 정책 확인 후 따로 정리한다.
