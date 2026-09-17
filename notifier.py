import asyncio
import html
import logging
from typing import Optional

import aiohttp

from config import Config

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self):
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        self.enabled = bool(self.token and self.chat_id)
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        # 메시지마다 세션을 새로 여는 대신 재사용한다.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def send_message(self, message: str):
        if not self.enabled:
            logger.debug("텔레그램 비활성 - 메시지: %s", message)
            return

        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
        try:
            async with self._lock:
                session = await self._get_session()
                async with session.post(self.api_url, json=payload) as response:
                    if response.status != 200:
                        logger.error(
                            "텔레그램 전송 실패 (%s): %s",
                            response.status, await response.text(),
                        )
        except Exception as e:
            # 알림 실패가 매매 루프를 죽여서는 안 된다.
            logger.error("텔레그램 예외: %s", e)

    @staticmethod
    def _esc(value) -> str:
        """
        parse_mode=HTML 이라 <, >, & 를 이스케이프하지 않으면 400 이 난다.
        LLM 추론 텍스트에 <reasoning> 태그가 섞여 들어오므로 실제로 발생한다.
        """
        return html.escape(str(value), quote=False)

    # ------------------------------------------------------------------
    async def notify_trade(self, side: str, ticker: str, price: float,
                           amount: float, dry_run: bool = False):
        emoji = "🟢" if side.upper() == "BID" else "🔴"
        tag = " <i>[DRY-RUN]</i>" if dry_run else ""
        price_txt = f"{price:,.2f}" if price else "시장가"
        msg = (
            f"<b>{emoji} 주문 체결{tag}</b>\n"
            f"종목: {self._esc(ticker)}\n"
            f"방향: {self._esc(side)}\n"
            f"가격: {price_txt}\n"
            f"수량/금액: {self._esc(amount)}"
        )
        await self.send_message(msg)

    async def notify_error(self, error_msg: str):
        await self.send_message(f"<b>⚠️ 시스템 오류</b>\n{self._esc(error_msg)}")

    async def notify_circuit_breaker(self, reason: str):
        await self.send_message(
            f"<b>🚨 서킷 브레이커 발동</b>\n사유: {self._esc(reason)}\n매매를 중단합니다."
        )

    async def notify_risk_halt(self, reason: str):
        await self.send_message(
            f"<b>🛑 리스크 한도 도달</b>\n사유: {self._esc(reason)}\n당일 신규 매수를 중단합니다."
        )

    async def notify_startup(self, mode: str, detail: str = ""):
        await self.send_message(
            f"<b>🤖 봇 기동</b>\n모드: {self._esc(mode)}\n{self._esc(detail)}"
        )

    async def notify_shutdown(self, reason: str = "정상 종료"):
        await self.send_message(f"<b>🔌 봇 종료</b>\n{self._esc(reason)}")
