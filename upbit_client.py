import aiohttp
import jwt
import uuid
import hashlib
from urllib.parse import urlencode
from config import Config
import logging

logger = logging.getLogger(__name__)

class UpbitClient:
    def __init__(self):
        self.access_key = Config.UPBIT_ACCESS_KEY
        self.secret_key = Config.UPBIT_SECRET_KEY
        self.server_url = 'https://api.upbit.com'

    def _generate_headers(self, query=None):
        payload = {
            'access_key': self.access_key,
            'nonce': str(uuid.uuid4())
        }
        
        if query:
            query_string = urlencode(query).encode('utf-8')
            m = hashlib.sha512()
            m.update(query_string)
            query_hash = m.hexdigest()
            payload['query_hash'] = query_hash
            payload['query_hash_alg'] = 'SHA512'

        jwt_token = jwt.encode(payload, self.secret_key, algorithm='HS256')
        authorize_token = f'Bearer {jwt_token}'
        headers = {
            'Authorization': authorize_token
        }
        return headers

    async def get_accounts(self):
        headers = self._generate_headers()
        url = f"{self.server_url}/v1/accounts"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as res:
                if res.status == 200:
                    return await res.json()
                else:
                    error = await res.text()
                    logger.error(f"Failed to fetch accounts: {error}")
                    return None

    async def get_order_chance(self, market: str):
        query = {'market': market}
        headers = self._generate_headers(query)
        url = f"{self.server_url}/v1/orders/chance"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=query) as res:
                if res.status == 200:
                    return await res.json()
                return None

    async def place_order(self, market: str, side: str, volume: str, price: str, ord_type: str = 'limit'):
        query = {
            'market': market,
            'side': side,
            'volume': volume,
            'price': price,
            'ord_type': ord_type
        }
        headers = self._generate_headers(query)
        url = f"{self.server_url}/v1/orders"
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=query) as res:
                if res.status in (200, 201):
                    return await res.json()
                else:
                    error = await res.text()
                    logger.error(f"Order failed: {error}")
                    return None

    async def cancel_order(self, uuid_str: str):
        query = {'uuid': uuid_str}
        headers = self._generate_headers(query)
        url = f"{self.server_url}/v1/order"
        
        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=headers, params=query) as res:
                if res.status == 200:
                    return await res.json()
                else:
                    error = await res.text()
                    logger.error(f"Cancel order failed: {error}")
                    return None
