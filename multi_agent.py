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

# ---------------------------------------------------------------------------
# 최종 판정 규칙
#
# 원래는 Trading Agent(LLM)가 BUY/SELL/HOLD 를 직접 골랐다. 프롬프트가
# "prefer HOLD unless the signals clearly agree" 뿐이고 'clearly agree' 를
# 숫자로 정의하지 않아, 실측 59건이 전부 HOLD 로 나왔다. 그중 부호가 일치한
# 경우가 24건(41%)이었는데도 그렇다. 근거 문장도 자기 점수 체계를 어겼다
# (news=+0.25 를 "bearish", crypto=0.40/news=0.30 을 "weak/bearish" 로 서술).
#
# 그래서 LLM 은 점수 산출까지만 맡고, 조합과 판정은 여기서 숫자로 한다.
# 테스트·재현이 되고 임계값을 데이터로 조정할 수 있다.
# ---------------------------------------------------------------------------
DEFAULT_MIN_SCORE = 0.4       # |가중합| 이 값 미만이면 진입하지 않는다
CRYPTO_WEIGHT = 0.7           # 가중합 = 0.7*crypto + 0.3*news
NEWS_WEIGHT = 0.3


def decide_action(crypto_score: float, news_score: float,
                  min_score: float = DEFAULT_MIN_SCORE) -> Dict[str, Any]:
    """
    두 점수의 가중합으로 최종 행동을 정한다.

        score = 0.7*crypto + 0.3*news        (부호 있는 합)
        |score| >= min_score 이면 진입, 부호가 방향

    처음에는 '두 점수의 부호 일치 + |crypto| >= 0.3' 이었는데, 이 규칙은
    news 에 거부권을 줬다. 실측 95건(7.9시간)에서 두 신호의 성질이 전혀
    달랐다:

        crypto : 범위 -0.78~+0.85, 표준편차 0.512, 직전값과 동일 7%
        news   : 범위 -0.25~+0.45, 표준편차 0.216, 직전값과 동일 31%

    news 는 변동폭이 절반도 안 되고 3번 중 1번은 직전 값 그대로다. 헤드라인을
    NEWS_CACHE_TTL_SEC(기본 15분) 캐시하는 데다 뉴스 자체가 일간 사이클이라
    분 단위 가격 움직임을 따라갈 수 없다. 그 느린 신호가 빠른 신호를 거부하면서
    |crypto|>=0.3 인 68건 중 39건(57%)이 막혔고, 그중 11건이 매수 기회였다
    (+0.85, +0.75, +0.75, +0.72, ... 실제 급등 구간 포함).

    가중합으로 바꾸면 news 는 문턱을 올리고 내리기만 한다:
        news= 0.00 -> crypto >= 0.571 이어야 진입
        news=+0.45 -> crypto >= 0.379   (관측 최대 호재)
        news=-0.25 -> crypto >= 0.679   (관측 최소 악재)
    거부권은 없지만, 진짜 악재(news=-0.9)면 crypto 가 0.96 은 돼야 하므로
    필요한 상황에서는 사실상 거부권처럼 작동한다.

    News Agent 조회/판단 실패 시에는 score 0.0 이 들어온다. 예전 규칙은 이걸
    '합의 실패'로 보고 무조건 매매를 막았지만, 지금은 crypto 단독 기준
    (|crypto| >= min_score/0.7)으로 올라갈 뿐 완전히 막지는 않는다.
    호출부(main.py)가 이 경우를 로그로 남긴다.

    strength 는 |score|. 메타 모델의 입력이자, 메타 모델이 아직 없을 때의
    대체 게이트 기준이 된다.
    """
    c = float(crypto_score)
    n = float(news_score)
    score = round(CRYPTO_WEIGHT * c + NEWS_WEIGHT * n, 4)
    strength = round(abs(score), 4)

    if strength < min_score:
        return {"action": "HOLD", "strength": strength,
                "reason": (f"가중합 약함 ({score:+.2f}, |{score:+.2f}| < {min_score}) "
                          f"· crypto={c:+.2f} news={n:+.2f}")}

    return {
        "action": "BUY" if score > 0 else "SELL",
        "strength": strength,
        "reason": f"가중합 {score:+.2f} · crypto={c:+.2f} news={n:+.2f}",
    }


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
        """
        ※ 2026-09-18 부터 매매 판정 경로에서 빠졌다. main.py 는 decide_action()
        을 쓴다 (사유는 이 파일 상단 주석 참고). 되돌릴 수 있도록 남겨두며,
        프롬프트 기반 판정을 다시 실험할 때 쓸 수 있다.
        """
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
