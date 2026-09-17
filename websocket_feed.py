import asyncio
import websockets
import json
import logging
import uuid
from typing import List, Callable, Dict, Any

logger = logging.getLogger(__name__)

class WebsocketFeed:
    def __init__(self, tickers: List[str]):
        self.tickers = tickers
        self.url = "wss://api.upbit.com/websocket/v1"
        self.callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._is_running = False

    def add_callback(self, callback: Callable[[Dict[str, Any]], None]):
        self.callbacks.append(callback)

    async def _handle_message(self, message):
        try:
            data = json.loads(message)
            for callback in self.callbacks:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
        except json.JSONDecodeError:
            logger.error("Failed to decode websocket message.")
        except Exception as e:
            logger.error(f"Error handling message: {str(e)}")

    async def connect_and_listen(self):
        self._is_running = True
        while self._is_running:
            try:
                async with websockets.connect(self.url, ping_interval=60, ping_timeout=30) as websocket:
                    logger.info("Websocket connected.")
                    
                    subscribe_payload = [
                        {"ticket": str(uuid.uuid4())},
                        {"type": "ticker", "codes": self.tickers},
                        {"type": "orderbook", "codes": self.tickers},
                        {"type": "trade", "codes": self.tickers}
                    ]
                    
                    await websocket.send(json.dumps(subscribe_payload))
                    
                    async for message in websocket:
                        if not self._is_running:
                            break
                        await self._handle_message(message)
                        
            except websockets.ConnectionClosed as e:
                logger.warning(f"Websocket connection closed: {e}. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Websocket error: {e}. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

    def stop(self):
        self._is_running = False
