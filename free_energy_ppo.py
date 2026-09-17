import asyncio
import logging
from collections import deque
from typing import Optional, Tuple

import numpy as np
import torch
import gymnasium as gym
from gymnasium import spaces
from stable_baselines3 import PPO

logger = logging.getLogger(__name__)

OBS_DIM = 20
ACTION_DIM = 3


class MarketReplayBuffer:
    """
    라이브 피드에서 수집한 (관측치, 전방수익률) 쌍을 담는 순환 버퍼.

    기존 환경은 step() 마다 np.random 으로 관측치와 보상을 만들어냈다.
    그렇게 학습한 가중치는 시장과 아무 관계가 없으므로, 실제 마켓에서
    캡처한 표본을 리플레이하도록 바꾼다.
    """

    def __init__(self, maxlen: int = 5000):
        self.obs = deque(maxlen=maxlen)
        self.fwd_ret = deque(maxlen=maxlen)
        self._cursor = 0

    def push(self, obs: np.ndarray, forward_return: float):
        self.obs.append(np.asarray(obs, dtype=np.float32))
        self.fwd_ret.append(float(forward_return))

    def __len__(self) -> int:
        return len(self.obs)

    def ready(self, min_samples: int = 128) -> bool:
        return len(self.obs) >= min_samples

    def next(self) -> Optional[Tuple[np.ndarray, float]]:
        if not self.obs:
            return None
        i = self._cursor % len(self.obs)
        self._cursor += 1
        return self.obs[i], self.fwd_ret[i]


class FreeEnergyEnv(gym.Env):
    """
    자유 에너지 벨만 보상을 계산하는 환경.
        V(o_t) = max E[r] - S* - W_t * I(regime) + tau * H(pi)

    관측치와 기저 수익률은 MarketReplayBuffer 에서 가져온다.
    버퍼가 비어 있으면 학습을 진행하지 않고 0 보상을 돌려준다
    (난수로 채워 넣어 '학습한 척' 하지 않기 위함).
    """

    metadata = {"render_modes": ["human"]}

    def __init__(self, buffer: Optional[MarketReplayBuffer] = None,
                 slippage_coef: float = 0.001, dissipation_coef: float = 0.005):
        super().__init__()
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(OBS_DIM,), dtype=np.float32)
        self.action_space = spaces.Box(low=-1, high=1, shape=(ACTION_DIM,), dtype=np.float32)

        self.buffer = buffer if buffer is not None else MarketReplayBuffer()
        self.slippage_coef = slippage_coef
        self.dissipation_coef = dissipation_coef

        self.current_step = 0
        self.max_steps = 1000
        self.current_obs = np.zeros(OBS_DIM, dtype=np.float32)
        self.last_reward_info = {}
        self._starved_logged = False

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.current_step = 0
        sample = self.buffer.next()
        if sample is not None:
            self.current_obs = sample[0]
        return self.current_obs, {}

    def step(self, action):
        self.current_step += 1
        action = np.asarray(action, dtype=np.float32)

        sample = self.buffer.next()
        if sample is None:
            if not self._starved_logged:
                logger.warning("리플레이 버퍼가 비어 학습 표본이 없습니다 (보상 0 처리).")
                self._starved_logged = True
            info = {"r_base": 0.0, "s_star": 0.0, "w_t_penalty": 0.0, "starved": True}
            self.last_reward_info = info
            return self.current_obs, 0.0, False, True, info

        next_obs, fwd_ret = sample

        # 1. 기저 기대수익: 포지션 방향 x 실현된 전방수익률
        exposure = float(np.clip(action.mean(), -1.0, 1.0))
        r_base = exposure * fwd_ret

        # 2. 측지선 슬리피지: 포지션 변경 크기에 대한 패널티
        s_star = self.slippage_coef * float(np.linalg.norm(action))

        # 3. Wasserstein 소산: 고변동 국면에서만 부과
        #    관측치의 마지막 성분이 점성(viscosity) 지표다.
        viscosity = float(next_obs[-1]) if next_obs.size else 0.0
        regime_indicator = 1.0 if viscosity > 0.6 else 0.0
        w_penalty = self.dissipation_coef * regime_indicator

        reward = r_base - s_star - w_penalty

        self.current_obs = next_obs
        truncated = self.current_step >= self.max_steps
        info = {
            "r_base": r_base,
            "s_star": s_star,
            "w_t_penalty": w_penalty,
            "viscosity": viscosity,
        }
        self.last_reward_info = info
        return next_obs, float(reward), False, truncated, info

    def render(self):
        pass


class OnlineFreeEnergyAgent:
    """
    실시간 온라인 학습용 PPO 래퍼.

    변경점:
      - 환경이 난수가 아니라 라이브 마켓 리플레이 버퍼를 소비한다.
      - train_online 을 await 가능하게 만들었다. 기존에는 동기 블로킹
        호출을 async 틱 안에서 그대로 실행해 이벤트 루프(웹소켓 수신 포함)를
        통째로 멈춰 세웠다.
    """

    def __init__(self, buffer: Optional[MarketReplayBuffer] = None):
        self.buffer = buffer if buffer is not None else MarketReplayBuffer()
        self.env = FreeEnergyEnv(self.buffer)
        self.model = PPO(
            "MlpPolicy",
            self.env,
            verbose=0,
            ent_coef=0.01,
            learning_rate=3e-4,
            n_steps=64,
            batch_size=16,
            device="cuda" if torch.cuda.is_available() else "cpu",
        )
        self.training_steps_accumulated = 0
        self._training = False

    def get_action(self, obs: np.ndarray) -> np.ndarray:
        obs = np.asarray(obs, dtype=np.float32).reshape(OBS_DIM)
        action, _ = self.model.predict(obs, deterministic=False)
        return action

    def _train_sync(self, timesteps: int):
        self.model.learn(total_timesteps=timesteps, reset_num_timesteps=False)
        self.training_steps_accumulated += timesteps

    async def train_online(self, timesteps: int = 64, min_samples: int = 128) -> bool:
        """
        별도 스레드에서 학습해 이벤트 루프를 막지 않는다.
        표본이 부족하면 학습을 건너뛴다.
        """
        if self._training:
            logger.debug("이전 학습이 아직 진행 중 - 이번 주기는 건너뜁니다.")
            return False
        if not self.buffer.ready(min_samples):
            logger.debug("표본 부족 (%d/%d) - 학습 보류.", len(self.buffer), min_samples)
            return False

        self._training = True
        try:
            await asyncio.to_thread(self._train_sync, timesteps)
            return True
        except Exception as e:
            logger.exception("온라인 학습 실패: %s", e)
            return False
        finally:
            self._training = False

    def save_model(self, path: str = "state/free_energy_ppo_online"):
        try:
            self.model.save(path)
            logger.info("모델 저장: %s", path)
        except Exception as e:
            logger.error("모델 저장 실패: %s", e)

    def load_model(self, path: str = "state/free_energy_ppo_online"):
        import os
        if not os.path.exists(path + ".zip"):
            logger.info("저장된 모델 없음 - 새로 시작합니다.")
            return False
        try:
            self.model = PPO.load(path, env=self.env)
            logger.info("모델 복원: %s", path)
            return True
        except Exception as e:
            logger.error("모델 복원 실패: %s", e)
            return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def main():
        buf = MarketReplayBuffer()
        rng = np.random.default_rng(0)
        # 실제 피드 대신 형태만 같은 표본을 넣어 파이프라인을 점검한다.
        for _ in range(300):
            buf.push(rng.standard_normal(OBS_DIM).astype(np.float32), rng.normal(0, 0.002))

        agent = OnlineFreeEnergyAgent(buf)
        obs, _ = agent.env.reset()
        print("Predicted Action:", agent.get_action(obs))

        print("온라인 학습 1회 수행...")
        ok = await agent.train_online(timesteps=64)
        print("학습 완료:", ok, "| 누적 스텝:", agent.training_steps_accumulated)

        empty = OnlineFreeEnergyAgent(MarketReplayBuffer())
        print("표본 없을 때 학습 시도:", await empty.train_online(timesteps=64), "(False 가 정상)")

    asyncio.run(main())
