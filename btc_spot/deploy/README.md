# PC에서 AWS로 옮기기

이 폴더는 설치 참고 자료다. AWS 리소스를 만들거나 서비스를 설치·시작·자동 시작 등록하지 않는다. `spotpaper.service`는 공개 시세만 사용하는 paper이고, `spotlive.service.example`은 명시적으로 live 실행 인수가 들어간 별도 예시다. 파일 이름 변경이나 paper 설정 변경으로 실거래를 자동 전환하지 않는다.

## 설치 파일

- `requirements.txt`: 기존 저장소 고정 버전 중 실행에 필요한 `aiohttp`, `python-dotenv`만 포함한다. Python 3.12 기준이다.
- `spotpaper.service`: `/opt/btc-spot`의 소스와 가상환경을 사용한다. 모의 거래 상태는 `/var/lib/btc-spot/paper`에 저장한다.
- `spotlive.service.example`: 실거래 상태와 자격 증명 경로를 paper와 분리한다. 서비스를 사용할 때도 별도 계좌 검증이 필요하다.
- `spotnotify.service.example`: live 원장을 읽어 텔레그램 현황·체결 알림을 보낸다. 같은 자격 증명 파일의 텔레그램 설정을 사용한다.

`btc_spot`만 복사하면 충분하지 않다. 현재 실행기는 `btc_lab.market_fit`의 순수 주문 수량 계산과 `binance_coinm_v1.runtime.instance_lock`을 가져오므로 같은 버전의 저장소 소스가 필요하다. 루트 `.env`, PC의 가상환경, 불필요한 과거 로그를 소스와 함께 배포하지 않는다.

서버에서 소스와 실행용 `btcspot` 사용자를 준비한 뒤 설치할 가상환경의 예시는 다음과 같다. 아래에는 서비스 시작 명령을 포함하지 않았다.

```sh
python3.12 -m venv /opt/btc-spot/.venv
/opt/btc-spot/.venv/bin/python -m pip install -r /opt/btc-spot/btc_spot/deploy/requirements.txt
/opt/btc-spot/.venv/bin/python -m btc_spot --help
```

서비스 계정은 소스를 읽고 지정한 상태 폴더를 쓰면 된다. systemd의 `StateDirectory=btc-spot`은 `/var/lib/btc-spot`을 준비한다. live 예시를 사용할 경우 공통 계좌 잠금을 위한 `/opt/btc-spot/btc_spot/state` 디렉터리도 미리 만들고 `btcspot`이 쓸 수 있게 해야 한다. 자격 증명은 `/etc/btc-spot/binance.env`에 별도로 배치하고 해당 실행 계정만 읽도록 제한한다. 파일에는 `BINANCE_API_KEY`, `BINANCE_API_SECRET`을 넣으며 서비스 인수나 로그에 실제 값을 넣지 않는다. paper에는 이 파일이 필요하지 않다.

## 중복 실행 없이 상태 옮기기

1. PC 프로세스를 정상 종료하고, 해당 Python 프로세스가 실제로 끝났는지 확인한다. AWS 실행을 먼저 시작하지 않는다.
2. 같은 소스 버전과 전략 fingerprint를 서버에 배치한다. PC의 상태 폴더 전체를 종료 후 일관된 시점에서 백업한다. `ledger.sqlite3`뿐 아니라 남아 있다면 대응하는 `-wal`, `-shm`도 함께 보존한다. paper에서는 `paper_exchange.json`도 같은 시점의 파일이어야 한다. 작동 중인 DB 본체만 복사하면 체결 기록을 잃을 수 있다.
3. SQLite Backup API로 별도 DB 사본을 만들 수도 있지만, paper 거래소 JSON과 DB의 시점을 맞추려면 실행기를 먼저 멈춰야 한다. 빈 DB를 만들어 기존 포지션을 재초기화하지 않는다.
4. 모드별 폴더를 그대로 구분해 옮긴다. **paper의 DB·거래소 상태를 live로 가져오지 않는다.** live 최초 시작은 실제 계좌와 새 live 원장을 검증하는 별도 단계다. 기존 live 이동은 실제 live 원장을 이어받는다.
5. 복사 후 남아 있는 `stop.request`는 서버도 즉시 종료시키므로, PC 종료와 복사 완료를 확인한 뒤 목적지에서 해당 종료 요청 파일만 따로 보관한다. SQLite journal·체결 파일을 임의 삭제하지 않는다. OS 잠금 파일이 남아 있다는 사실 자체는 실행 중이라는 뜻이 아니다.
6. 서버에서 상태 복구 결과, 가상/실제 계좌 모드, 배정 BTC, 예비 BTC, 미확정 주문을 확인한다. live IP 제한이 있으면 서버의 출발 IP 설정도 일치해야 한다. 서버를 운영하는 동안 PC에서 같은 계좌 실행기를 다시 시작하지 않는다.

파일 잠금은 **한 호스트의 같은 저장소/상태 경로**에서 중복 작성자를 막는다. PC와 AWS 사이에는 분산 잠금이 없고, 서로 다른 서버·컨테이너에 복제한 상태는 동시 실행을 막아 주지 못한다. 이사 절차에서 이전 실행기를 먼저 완전히 멈추는 것이 필수다.

**live 원장을 이미 사용한 경우의 추가 조건:** 새 실행기는 `btc_spot/state/live-registry.json`과
DB 메타데이터 양쪽에 계좌 해시·원장 UUID·절대 경로를 묶는다. Windows→Linux 복사만으로는
실거래 시작을 허용하지 않는다. PC 실행기를 멈춘 뒤 `python -m btc_spot.migrate_live`에
원본 원장·registry 복사본과 비어 있는 대상 원장·registry 경로를 지정한다. 먼저 `--apply` 없이
검사하고, 결과의 UUID·체결 수·초기 BTC가 일치할 때만 `--apply`로 원본을 보존한 채 새 경로에
복사한다. 대상 원장은 `/var/lib/btc-spot/live/ledger.sqlite3`, 대상 registry는
`/opt/btc-spot/btc_spot/state/live-registry.json`이다. registry나 DB를 삭제해서 이 검사를
피하지 않는다. PC에서 paper만 운영한 뒤 AWS에서 처음 live를 시작한다면 paper를 이전해
live로 쓰지 않고, 실제 계좌로 새 live 원장을 초기화한다.

## 확인용 실행 형식

paper는 키 없이 현재 시세로 가상 체결한다. 다음 명령의 `--once`는 한 번 실행한 뒤 종료하며 모의 주문을 만들 수 있다.

```sh
/opt/btc-spot/.venv/bin/python -m btc_spot run --mode paper --initial-btc 0.003 --state-dir /var/lib/btc-spot/paper --once
```

live 계좌의 읽기 전용 준비 확인 형식은 다음과 같다. 주문 제출을 허용하지 않는다.

```sh
/opt/btc-spot/.venv/bin/python -m btc_spot prepare --mode live --initial-btc 0.003 --state-dir /var/lib/btc-spot/live --credentials-file /etc/btc-spot/binance.env
```

실거래 서비스 예시는 명시적인 `--mode live`와 `--confirm I_UNDERSTAND_LIVE_SPOT`을 포함한다. 확인 문자열은 수익이나 운영 적합성을 보장하는 검증 결과가 아니다. 실제 실행 승인을 받은 범위와 준비 보고서를 검토한 뒤에만 별도로 서비스 설치·시작을 결정한다.

systemd 템플릿은 정상 종료에 `SIGINT`를 사용하여 실행기의 `finally`에서 DB, 네트워크 연결, 잠금을 닫도록 한다. 강제 종료되었거나 제한 시간을 넘겼다면 주문이 없었다고 가정하지 않고 client order ID와 체결 원장으로 먼저 복구한다. 서비스 중지 뒤 상태를 옮기고, paper와 live 서비스를 함께 시작하지 않는다.
