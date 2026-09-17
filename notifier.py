import aiohttp
import logging
from config import Config

logger = logging.getLogger(__name__)

class TelegramNotifier:
    def __init__(self):
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        
    async def send_message(self, message: str):
        if not self.token or not self.chat_id:
            logger.debug(f"Telegram Notification Disabled. Message: {message}")
            return
            
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Failed to send telegram message: {error_text}")
        except Exception as e:
            logger.error(f"Exception during telegram notification: {str(e)}")

    async def notify_trade(self, side: str, ticker: str, price: float, amount: float):
        emoji = "🟢" if side.upper() == "BID" else "🔴"
        msg = f"<b>{emoji} Trade Executed</b>\nTicker: {ticker}\nSide: {side}\nPrice: {price:,.0f}\nAmount: {amount}"
        await self.send_message(msg)

    async def notify_error(self, error_msg: str):
        msg = f"<b>⚠️ System Error</b>\n{error_msg}"
        await self.send_message(msg)
        
    async def notify_circuit_breaker(self, reason: str):
        msg = f"<b>🚨 CIRCUIT BREAKER TRIGGERED</b>\nReason: {reason}\nAll trading halted."
        await self.send_message(msg)
