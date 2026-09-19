"""
크립토 뉴스 헤드라인 수집기. News Agent 의 입력을 만든다.

기존에는 main.py 가 "추가 뉴스 피드 미연동 - 중립으로 간주." 라는 고정 문자열을
넘겼다. crypto_score 가 아무리 강해도 news_score 가 항상 0 근처라, Trading
Agent 프롬프트의 "신호가 명확히 합의할 때만 매매" 조건이 구조적으로 거의
성립하지 않았다 (실측: 76건 전부 HOLD).

소스:
  - 네이버 뉴스 검색 : 국내 코인 뉴스. 종목별 검색어 사용 (일 25,000건 쿼터)
  - NewsAPI     : 영문 보조. 무료 100건/일 이라 종목별로 쪼개면 초과하므로
                  공유 쿼리 하나로 둔다 (4종목 x 96주기 = 384건/일 > 100)
  - CryptoPanic : 크립토 전용이지만 v1 API 가 403 (2026-09-18 확인)
  - GDELT       : 키 불필요하나 레이트리밋으로 기본 비활성 (Config 주석 참고)

종목별 검색어를 쓰는 이유는 TICKER_QUERIES 주석 참고. 요약하면 공유 뉴스가
BTC 중심이라 알트 판정을 깎고 있었다.

원칙:
  - 소스별 키가 없으면 조용히 건너뛴다 (전부 선택 기능)
  - 한 소스가 실패해도 나머지는 계속 시도한다 (return_exceptions)
  - LLM_INTERVAL_SEC(5분)마다 그대로 때리면 무료 쿼터가 금방 빠듯해진다
    (NewsAPI 무료 플랜 100건/일) -> TTL 캐시로 호출 빈도를 낮춘다
  - 전부 실패/미설정이면 기존과 동일한 중립 문구로 폴백한다 (하위호환)
"""

import asyncio
import html as html_lib
import logging
import re
import time
from typing import Any, Dict, List, Optional

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_FALLBACK_TEXT = "뉴스 피드 응답 없음 - 중립으로 간주."

# 종목별 검색어.
#
# 처음에는 4종목에 같은 뉴스("비트코인 암호화폐")를 넣고 같은 점수를 썼다.
# 실측 196주기 결과 그 헤드라인이 BTC 중심이라, 공유 뉴스 점수가 BTC 판정은
# 보강하고(+4%) 알트 판정은 오히려 깎았다(SOL -25%, 부호 불일치 76%).
# 진입 건수도 BTC 71 / XRP 31 / ETH 11 / SOL 8 로 벌어졌다.
# "비트코인 호재"를 솔라나 매수 근거로 쓰던 셈이라 종목별로 분리한다.
TICKER_QUERIES: Dict[str, Dict[str, str]] = {
    "KRW-BTC": {"ko": "비트코인", "en": "bitcoin"},
    "KRW-ETH": {"ko": "이더리움", "en": "ethereum"},
    "KRW-XRP": {"ko": "리플 XRP", "en": "ripple XRP"},
    "KRW-SOL": {"ko": "솔라나", "en": "solana"},
}
DEFAULT_QUERY = {"ko": "암호화폐 가상자산", "en": "cryptocurrency"}


def queries_for(ticker: Optional[str]) -> Dict[str, str]:
    return TICKER_QUERIES.get(ticker or "", DEFAULT_QUERY)


def _strip_html(text: str) -> str:
    """네이버 검색 API 는 제목에 <b> 태그와 HTML 엔티티(&quot; 등)를 섞어 준다."""
    return html_lib.unescape(_TAG_RE.sub("", text or "")).strip()


# ---------------------------------------------------------------------------
# 순수 파싱 함수. 네트워크와 분리해 오프라인 테스트가 가능하게 한다.
# ---------------------------------------------------------------------------
def parse_cryptopanic(data: Optional[Dict[str, Any]], limit: int = 8) -> List[str]:
    if not isinstance(data, dict):
        return []
    out = []
    for item in (data.get("results") or [])[:limit]:
        title = _strip_html(item.get("title") or "")
        if title:
            out.append(title)
    return out


def parse_naver(data: Optional[Dict[str, Any]], limit: int = 8) -> List[str]:
    if not isinstance(data, dict):
        return []
    out = []
    for item in (data.get("items") or [])[:limit]:
        title = _strip_html(item.get("title") or "")
        if title:
            out.append(title)
    return out


def parse_newsapi(data: Optional[Dict[str, Any]], limit: int = 8) -> List[str]:
    if not isinstance(data, dict) or data.get("status") != "ok":
        return []
    out = []
    for item in (data.get("articles") or [])[:limit]:
        title = _strip_html(item.get("title") or "")
        if title:
            out.append(title)
    return out


def parse_gdelt(data: Optional[Dict[str, Any]], limit: int = 8) -> List[str]:
    """
    GDELT DOC 2.0 artlist 응답. 제목이 토큰 단위로 띄어쓰기돼 오는 경우가
    있어(" Shares Up 6 . 8 % ") 중복 공백을 정리한다.
    """
    if not isinstance(data, dict):
        return []
    out = []
    for item in (data.get("articles") or [])[:limit]:
        title = re.sub(r"\s+", " ", _strip_html(item.get("title") or "")).strip()
        if title:
            out.append(title)
    return out


def merge_headlines(*groups: List[str], limit: int = 15) -> str:
    """중복 제거 후 LLM 프롬프트용 텍스트로 합친다. 전부 비면 중립 문구."""
    seen, out = set(), []
    for group in groups:
        for h in group:
            if h and h not in seen:
                seen.add(h)
                out.append(h)
    if not out:
        return _FALLBACK_TEXT
    return "\n".join(f"- {h}" for h in out[:limit])


# ---------------------------------------------------------------------------
class NewsFeed:
    def __init__(self, ttl_sec: Optional[float] = None):
        self.ttl_sec = Config.NEWS_CACHE_TTL_SEC if ttl_sec is None else ttl_sec
        self._session: Optional[aiohttp.ClientSession] = None
        # 종목별 캐시. 예전에는 단일 캐시라 모든 종목이 같은 헤드라인을 봤다.
        self._cache: Dict[str, tuple] = {}     # ticker -> (text, monotonic_ts)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_json(self, url: str, *, params: Optional[Dict[str, Any]] = None,
                        headers: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
        try:
            session = await self._get_session()
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning("뉴스 소스 응답 실패 (%s): HTTP %s", url, resp.status)
                    return None
                return await resp.json(content_type=None)
        except Exception as e:
            # 뉴스 조회 실패가 매매 루프를 죽여서는 안 된다.
            logger.warning("뉴스 소스 호출 실패 (%s): %s", url, e)
            return None

    async def _fetch_cryptopanic(self, ticker: Optional[str]) -> List[str]:
        if not Config.CRYPTOPANIC_API_KEY:
            return []
        cur = (ticker or "").replace("KRW-", "") or "BTC,ETH,XRP,SOL"
        # 2026-09-18 확인: v1 API 가 403 을 반환함(정책 변경 또는 플랜 문제로
        # 추정, 원인 미확인). 키가 있으면 계속 시도하고, 성공하면 자동으로
        # 살아난다 - 실패해도 조용히 [] 로 넘어가므로 다른 소스에는 영향 없다.
        data = await self._get_json(
            "https://cryptopanic.com/api/v1/posts/",
            params={"auth_token": Config.CRYPTOPANIC_API_KEY,
                   "currencies": cur, "public": "true"},
        )
        return parse_cryptopanic(data)

    async def _fetch_naver(self, ticker: Optional[str]) -> List[str]:
        """
        종목별 한국어 검색. 네이버 검색 API 는 일 25,000건이라 종목 수만큼
        늘려도 여유가 있다 (4종목 x 96주기 = 384건/일).
        """
        if not (Config.NAVER_CLIENT_ID and Config.NAVER_CLIENT_SECRET):
            return []
        data = await self._get_json(
            "https://openapi.naver.com/v1/search/news.json",
            params={"query": queries_for(ticker)["ko"], "display": 10, "sort": "date"},
            headers={"X-Naver-Client-Id": Config.NAVER_CLIENT_ID,
                    "X-Naver-Client-Secret": Config.NAVER_CLIENT_SECRET},
        )
        return parse_naver(data)

    async def _fetch_newsapi(self) -> List[str]:
        if not Config.NEWSAPI_KEY:
            return []
        # q 대신 qInTitle 을 쓴다. q 는 본문까지 검색해 "cryptocurrency" 가
        # 한 줄 섞인 무관한 기사(랜섬웨어, 호스팅 광고 등)까지 끌려온다.
        data = await self._get_json(
            "https://newsapi.org/v2/everything",
            params={"qInTitle": "bitcoin OR crypto OR ethereum", "language": "en",
                   "sortBy": "publishedAt", "pageSize": 10, "apiKey": Config.NEWSAPI_KEY},
        )
        return parse_newsapi(data)

    async def _fetch_gdelt(self, ticker: Optional[str]) -> List[str]:
        """
        GDELT DOC 2.0. API 키가 없고 전 세계 영문 기사를 다룬다.

        다만 레이트리밋이 공격적이다. 실측(2026-09-19, EC2 에서 10초/60초
        간격 시도) 대부분이 HTTP 429 로 거절됐다. 문서에는 '5초에 1회' 라고
        돼 있지만 그보다 훨씬 엄격하게 걸린다.
        그래서 보조 소스로만 쓴다 - 실패해도 다른 소스가 헤드라인을 채우므로
        매매에는 영향이 없고, 성공하면 커버리지가 넓어진다.
        """
        if not Config.GDELT_ENABLED:
            return []
        q = queries_for(ticker)["en"]
        data = await self._get_json(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={"query": f"{q} sourcelang:eng", "mode": "artlist",
                   "maxrecords": 8, "format": "json", "sort": "datedesc",
                   "timespan": "24H"},
        )
        return parse_gdelt(data)

    async def _fetch_all(self, ticker: Optional[str]) -> str:
        results = await asyncio.gather(
            self._fetch_cryptopanic(ticker),
            self._fetch_naver(ticker),
            self._fetch_newsapi(),
            self._fetch_gdelt(ticker),
            return_exceptions=True,
        )
        groups = [r for r in results if isinstance(r, list)]
        for r in results:
            if isinstance(r, Exception):
                logger.warning("뉴스 소스 예외(무시하고 계속): %s", r)
        return merge_headlines(*groups)

    async def get_headlines(self, ticker: Optional[str] = None) -> str:
        """
        종목별 헤드라인. TTL 안이면 캐시를 반환하고, 실패 시에는 마지막 성공
        캐시를 유지한다.
        """
        key = ticker or "_default"
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[1] < self.ttl_sec:
            return cached[0]

        text = await self._fetch_all(ticker)
        if text != _FALLBACK_TEXT or cached is None:
            self._cache[key] = (text, now)
        return self._cache[key][0]
