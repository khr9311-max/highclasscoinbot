"""
Binance COIN-M Futures 자동매매봇 V1 (BTCUSD 무기한, BTC 증거금·BTC 회계).

기존 업비트 현물 봇(저장소 루트)과 완전히 분리된 패키지다. 루트의 모듈·DB·.env 를
읽지 않는다. 가격행동 규칙은 strategy/price_action.py 에 복사(vendoring)해 두었고,
원본과 결과가 같은지는 tests/test_strategy_parity.py 가 확인한다.

실행:  python -m binance_coinm_v1 --help
"""

__version__ = "1.0.0"
