"""
테스트 공통 설정.

어떤 테스트도 실제 네트워크에 나가지 않는다 (실계정 주문 가능성을 원천 차단):
  1) 외부 호스트 DNS 조회를 막는다 (aiohttp·websockets 모두 이름 해석을 거친다)
  2) COINM_V1_TEST_MODE=1 이면 실제 HTTP/WS 전송 객체 생성 자체가 예외를 낸다
  3) 설정은 .env 를 읽지 않고 테스트가 준 값으로만 만든다
"""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"

_real_getaddrinfo = socket.getaddrinfo
_LOCAL = {"localhost", "127.0.0.1", "::1", None, ""}


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host in _LOCAL:
        return _real_getaddrinfo(host, *args, **kwargs)
    raise RuntimeError(f"테스트 중 외부 네트워크 접근 차단: {host}")


def _guarded_create_connection(address, *args, **kwargs):
    raise RuntimeError(f"테스트 중 외부 네트워크 접근 차단: {address}")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    monkeypatch.setenv("COINM_V1_TEST_MODE", "1")
    # 실계정 관련 환경변수가 테스트에 섞이지 않게 비운다
    for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "EXECUTION_MODE", "LIVE_TRADING_ENABLED",
              "LIVE_CONFIRMATION", "BINANCE_ENV", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    yield


def run(coro):
    """pytest-asyncio 없이 코루틴 실행."""
    return asyncio.run(coro)


@pytest.fixture
def exchange_info():
    with open(FIXTURES / "exchange_info_coinm.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def settings(tmp_path):
    from binance_coinm_v1.config import Settings
    return Settings.build(state_dir=str(tmp_path / "state"))


@pytest.fixture
def db(tmp_path):
    from binance_coinm_v1.storage import Database
    d = Database(str(tmp_path / "t.sqlite3"))
    yield d
    d.close()
