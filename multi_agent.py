import asyncio
import json
import logging
import re
from typing import Dict, Any, Optional

from config import Config

try:
    from google import genai
except ImportError:  # 패키지 미설치 환경에서도 임포트는 통과시킨다
    genai = None

logger = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class MultiAgentSystem:
    """
    Gemini 기반 다중 에이전트 의사결정.
      - Crypto Agent  (기술적 분석)
      - News Agent    (감성 분석)
      - Trading Agent (상위 수퍼바이저)

    변경점:
      - 모델명을 Config 로 뺐다 (README 와 코드가 서로 다른 모델을 가리키던 문제).
      - 실패를 'score 0.0' 으로 뭉개지 않고 ok=False 로 구분한다. 기존에는
        LLM 장애와 '중립 판단'이 같은 값으로 내려와 상위 로직이 구분할 수 없었다.
      - 타임아웃과 재시도를 넣었다. 응답이 늦으면 매매 틱 전체가 밀린다.
      - 프롬프트에서 <reasoning> 태그 지시를 뺐다. JSON 만 내놓으라고 하면서
        동시에 태그를 쓰라고 해 파싱 실패를 자초하던 부분이다.
    """

    def __init__(self):
        self.api_key = Config.GEMINI_API_KEY
        self.model_name = Config.GEMINI_MODEL
        self.timeout = 20.0
        self.max_retries = 2

        if genai and self.api_key:
            self.client = genai.Client(api_key=self.api_key)
        else:
            self.client = None
            logger.warning("Gemini 미설정 - LLM 에이전트는 중립(ok=False)으로 동작합니다.")

    # ------------------------------------------------------------------
    def _sync_call_llm(self, system_prompt: str, user_prompt: str) -> Optional[str]:
        if not self.client:
            return None

        interaction = self.client.interactions.create(
            model=self.model_name,
            system_instruction=system_prompt,
            input=user_prompt,
            generation_config={"response_mime_type": "application/json"},
        )
        text = (interaction.output_text or "").strip()
        return _JSON_FENCE.sub("", text).strip()

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> Optional[str]:
        for attempt in range(self.max_retries + 1):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._sync_call_llm, system_prompt, user_prompt),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("Gemini 응답 타임아웃 (%d/%d)", attempt + 1, self.max_retries + 1)
            except Exception as e:
                logger.warning("Gemini 호출 실패 (%d/%d): %s", attempt + 1, self.max_retries + 1, e)

            if attempt < self.max_retries:
                await asyncio.sleep(1.5 * (attempt + 1))
        return None

    @staticmethod
    def _parse(raw: Optional[str], fallback: Dict[str, Any]) -> Dict[str, Any]:
        if raw is None:
            return {**fallback, "ok": False, "reasoning": "LLM 호출 실패"}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("JSON 객체가 아님")
            data["ok"] = True
            return data
        except Exception as e:
            logger.warning("LLM JSON 파싱 실패: %s | 원문: %.200s", e, raw)
            return {**fallback, "ok": False, "reasoning": "JSON 파싱 실패"}

    @staticmethod
    def _clamp_score(value: Any) -> float:
        try:
            return max(-1.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    async def run_crypto_agent(self, market_summary: str) -> Dict[str, Any]:
        sys_prompt = (
            "You are a Crypto Analyst Agent. Analyze the given market data and score it. "
            "Respond with ONLY a JSON object: "
            '{"score": <float between -1 and 1>, "reasoning": "<short explanation>"}. '
            "score > 0 means bullish, score < 0 means bearish. No markdown, no extra text."
        )
        usr_prompt = f"Market data:\n{market_summary}\n\nProvide your market score."

        res = self._parse(await self._call_llm(sys_prompt, usr_prompt), {"score": 0.0})
        res["score"] = self._clamp_score(res.get("score"))
        return res

    async def run_news_agent(self, news_headlines: str) -> Dict[str, Any]:
        sys_prompt = (
            "You are a News Sentiment Agent for crypto markets. "
            "Respond with ONLY a JSON object: "
            '{"score": <float between -1 and 1>, "reasoning": "<short explanation>"}. '
            "score > 0 means positive sentiment. No markdown, no extra text."
        )
        usr_prompt = f"News:\n{news_headlines}\n\nProvide your sentiment score."

        res = self._parse(await self._call_llm(sys_prompt, usr_prompt), {"score": 0.0})
        res["score"] = self._clamp_score(res.get("score"))
        return res

    async def run_trading_agent(
        self, crypto_score: float, news_score: float, portfolio_state: str
    ) -> Dict[str, Any]:
        sys_prompt = (
            "You are the Lead Trading Agent. You receive scores from a Crypto Agent and a "
            "News Agent plus the current portfolio state, and make the final decision. "
            "Be conservative: prefer HOLD unless the signals clearly agree. "
            "Respond with ONLY a JSON object: "
            '{"action": "BUY"|"SELL"|"HOLD", "target_weight": <float 0.0-1.0>, '
            '"confidence": <float 0.0-1.0>, "reasoning": "<short explanation>"}. '
            "No markdown, no extra text."
        )
        usr_prompt = (
            f"Crypto Score: {crypto_score}\n"
            f"News Score: {news_score}\n"
            f"Portfolio State: {portfolio_state}\n"
            "Make your final decision."
        )

        res = self._parse(
            await self._call_llm(sys_prompt, usr_prompt),
            {"action": "HOLD", "target_weight": 0.0, "confidence": 0.0},
        )

        action = str(res.get("action", "HOLD")).upper().strip()
        res["action"] = action if action in ("BUY", "SELL", "HOLD") else "HOLD"
        try:
            res["target_weight"] = max(0.0, min(1.0, float(res.get("target_weight", 0.0))))
        except (TypeError, ValueError):
            res["target_weight"] = 0.0
        try:
            res["confidence"] = max(0.0, min(1.0, float(res.get("confidence", 0.0))))
        except (TypeError, ValueError):
            res["confidence"] = 0.0
        return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def test():
        mas = MultiAgentSystem()
        print("모델:", mas.model_name)
        c = await mas.run_crypto_agent("BTC 1h: 상승추세, 거래량 증가, RSI 62, 스프레드 0.02%")
        print("Crypto Agent:", c)
        n = await mas.run_news_agent("현물 ETF 순유입 지속. 규제 리스크 특이사항 없음.")
        print("News  Agent:", n)
        t = await mas.run_trading_agent(c.get("score", 0.0), n.get("score", 0.0),
                                        "KRW 1,000,000 / BTC 0")
        print("Trading Agent:", t)

    asyncio.run(test())
