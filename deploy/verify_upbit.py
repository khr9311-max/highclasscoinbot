"""
EC2 에서 실행하는 업비트 연동 검증 스크립트.

실주문을 켜기 전에 반드시 통과해야 하는 항목들을 확인한다.
주문은 내지 않는다 (조회 전용 + 주문가능정보 조회).

    /opt/coinbot/.venv/bin/python deploy/verify_upbit.py
"""

import asyncio
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK, NG = [], []


def check(name, ok, detail=""):
    (OK if ok else NG).append(name)
    print(f"  [{'OK' if ok else 'NG'}] {name}" + (f"  -- {detail}" if detail else ""))


def http_get(url, timeout=5, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode().strip()


def get_public_ip():
    """IMDSv2 우선, 실패 시 외부 조회."""
    try:
        token_req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        with urllib.request.urlopen(token_req, timeout=3) as r:
            token = r.read().decode()
        return http_get(
            "http://169.254.169.254/latest/meta-data/public-ipv4",
            headers={"X-aws-ec2-metadata-token": token},
        ), "EC2 메타데이터"
    except Exception:
        pass
    for url in ("https://checkip.amazonaws.com", "https://api.ipify.org"):
        try:
            return http_get(url, timeout=8), "외부 조회"
        except Exception:
            continue
    return None, None


async def main():
    print("=" * 64)
    print("업비트 연동 검증 (주문 없음)")
    print("=" * 64)

    # ---- 1. 공인 IP ----
    print("\n[1] 서버 공인 IP")
    ip, src = get_public_ip()
    check("공인 IP 확인", ip is not None, f"{ip} ({src})")
    if ip:
        print(f"       -> 업비트 Open API 허용 IP 에 '{ip}' 가 등록돼 있어야 합니다.")

    # ---- 2. 시계 ----
    print("\n[2] 시계 동기화 (업비트 JWT 는 타임스탬프를 검증)")
    try:
        import subprocess
        out = subprocess.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=10).stdout
        line = [l for l in out.splitlines() if "System time" in l]
        offset_ok = False
        if line:
            parts = line[0].split()
            offset = float(parts[3])
            offset_ok = offset < 1.0
            check("시계 오차 1초 미만", offset_ok, f"{offset:.6f}초")
        else:
            check("chrony 동작", False, "tracking 출력 없음")
    except Exception as e:
        check("시계 확인", False, f"{type(e).__name__}: {e}")

    # ---- 3. 설정 로딩 ----
    print("\n[3] 설정 및 자격증명")
    try:
        from config import Config
        has_keys = bool(Config.UPBIT_ACCESS_KEY and Config.UPBIT_SECRET_KEY)
        check("업비트 키 로딩", has_keys,
              f"소스={os.environ.get('COINBOT_SECRETS', 'env')}")
        check("DRY_RUN 값", True, f"{Config.DRY_RUN} ({'모의' if Config.DRY_RUN else '실주문'})")
        if not has_keys:
            print("\n키가 없어 이후 검증을 진행할 수 없습니다.")
            return
    except Exception as e:
        check("config 로딩", False, f"{type(e).__name__}: {e}")
        return

    # ---- 4. 업비트 인증 + 잔고 ----
    print("\n[4] 업비트 인증 및 잔고 (허용 IP 등록 여부가 여기서 드러남)")
    from upbit import AsyncUpbit

    client = AsyncUpbit(access_key=Config.UPBIT_ACCESS_KEY, secret_key=Config.UPBIT_SECRET_KEY)
    krw_balance = 0.0
    try:
        accounts = await client.accounts.list()
        check("계좌 조회 API 호출", True, f"{len(accounts)}개 자산")

        total = 0.0
        if accounts:
            print("\n       보유 자산:")
        for a in accounts:
            amt = float(a.balance or 0) + float(a.locked or 0)
            if amt <= 0:
                continue
            if a.currency == "KRW":
                krw_balance = float(a.balance or 0)
                total += amt
                print(f"         KRW   : {amt:,.0f}원 (사용가능 {krw_balance:,.0f})")
            else:
                val = amt * float(a.avg_buy_price or 0)
                total += val
                print(f"         {a.currency:6s}: {amt:.8f} (평가 {val:,.0f}원)")
        print(f"\n       총 평가액: {total:,.0f}원")
        check("KRW 잔고 존재", krw_balance > 0, f"{krw_balance:,.0f}원")
    except Exception as e:
        msg = str(e)[:300]
        check("계좌 조회 API 호출", False, f"{type(e).__name__}: {msg}")
        if "ip" in msg.lower() or "forbidden" in msg.lower() or "401" in msg:
            print("\n       >>> 허용 IP 미등록으로 보입니다. 업비트 Open API 관리에서")
            print(f"       >>> '{ip}' 를 허용 IP 로 등록하세요.")
        await client.close()
        return

    # ---- 5. 주문 권한 ----
    print("\n[5] 주문 권한 (실제 주문은 내지 않음)")
    try:
        ch = await client.orders.retrieve_chance(market="KRW-BTC")
        check("주문 권한 보유", True, f"매수수수료 {float(ch.bid_fee)*100:.3f}%")
        try:
            min_total = ch.market.bid.min_total
            check("최소 주문금액 확인", True, f"{float(min_total):,.0f}원")
        except Exception:
            pass
    except Exception as e:
        msg = str(e)[:250]
        check("주문 권한 보유", False, f"{type(e).__name__}: {msg}")
        print("       >>> API 키에 '주문하기' 권한이 없거나 허용 IP 문제입니다.")

    # ---- 6. 웹소켓 ----
    print("\n[6] 시세 웹소켓")
    try:
        import websockets, uuid
        async with websockets.connect("wss://api.upbit.com/websocket/v1", ping_interval=20) as ws:
            await ws.send(json.dumps([
                {"ticket": str(uuid.uuid4())},
                {"type": "ticker", "codes": ["KRW-BTC"]},
                {"format": "DEFAULT"},
            ]))
            msg = await asyncio.wait_for(ws.recv(), timeout=15)
            d = json.loads(msg)
            check("웹소켓 시세 수신", "trade_price" in d,
                  f"KRW-BTC {d.get('trade_price', 0):,.0f}원")
    except Exception as e:
        check("웹소켓 시세 수신", False, f"{type(e).__name__}: {e}")

    await client.close()

    # ---- 7. 잔고 기반 한도 제안 ----
    if krw_balance > 0:
        print("\n[7] 잔고 기반 권장 한도 (전체 노출 = 사용가능 KRW 의 30%)")
        exposure = round(krw_balance * 0.30, -3)
        per_ticker = round(exposure / 3, -3)
        order_size = max(10_000, round(exposure / 15, -3))
        daily_loss = round(exposure * 0.20, -3)
        print(f"       MAX_TOTAL_EXPOSURE_KRW = {exposure:,.0f}")
        print(f"       MAX_POSITION_KRW       = {per_ticker:,.0f}")
        print(f"       ORDER_SIZE_KRW         = {order_size:,.0f}")
        print(f"       DAILY_LOSS_LIMIT_KRW   = {daily_loss:,.0f}")

    print("\n" + "=" * 64)
    print(f"결과: {len(OK)} OK / {len(NG)} NG")
    if NG:
        print("실패 항목:")
        for n in NG:
            print("  -", n)
        print("\n>>> 실패 항목이 있으면 실주문을 켜지 마세요.")
    else:
        print(">>> 전 항목 통과. 실주문 전환 가능 상태입니다.")
    print("=" * 64)
    sys.exit(1 if NG else 0)


if __name__ == "__main__":
    asyncio.run(main())
