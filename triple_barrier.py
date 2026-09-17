import pandas as pd
import numpy as np
import lightgbm as lgb
from typing import Tuple, List

class MetaLabeling:
    def __init__(self, pt_sl: List[float] = [1.0, 1.0], min_ret: float = 0.001, num_threads: int = 4):
        self.pt_sl = pt_sl
        self.min_ret = min_ret
        self.model = lgb.LGBMClassifier(n_estimators=100, num_leaves=31, n_jobs=num_threads)
        
    def get_volatility(self, prices: pd.Series, span: int = 100) -> pd.Series:
        """EWMA 변동성 계산"""
        df0 = prices.pct_change()
        vol = df0.ewm(span=span).std()
        return vol

    def apply_pt_sl_on_t1(self, close: pd.Series, events: pd.DataFrame, pt_sl: List[float]) -> pd.DataFrame:
        """동적 상한/하한 장벽 도달 시간 및 수익률 계산"""
        out = events[['t1']].copy(deep=True)
        if pt_sl[0] > 0:
            pt = pt_sl[0] * events['trgt']
        else:
            pt = pd.Series(index=events.index)
            
        if pt_sl[1] > 0:
            sl = -pt_sl[1] * events['trgt']
        else:
            sl = pd.Series(index=events.index)

        for loc, t1 in events['t1'].fillna(close.index[-1]).items():
            df0 = close[loc:t1]
            df0 = (df0 / close[loc]) - 1
            out.loc[loc, 'sl'] = df0[df0 < sl[loc]].index.min()
            out.loc[loc, 'pt'] = df0[df0 > pt[loc]].index.min()
        return out

    def get_events(self, close: pd.Series, t_events: pd.DatetimeIndex, pt_sl: List[float], trgt: pd.Series, min_ret: float, num_threads: int = 1, t1: pd.Series = None, side: pd.Series = None) -> pd.DataFrame:
        """Triple Barrier 이벤트 생성"""
        trgt = trgt.loc[t_events]
        trgt = trgt[trgt > min_ret]
        if t1 is None:
            t1 = pd.Series(pd.NaT, index=t_events)
            
        if side is None:
            side_ = pd.Series(1., index=trgt.index)
            pt_sl_ = [pt_sl[0], pt_sl[0]]
        else:
            side_ = side.loc[trgt.index]
            pt_sl_ = pt_sl[:2]

        events = pd.concat({'t1': t1, 'trgt': trgt, 'side': side_}, axis=1).dropna(subset=['trgt'])
        df0 = self.apply_pt_sl_on_t1(close, events, pt_sl_)
        
        events['t1'] = df0.dropna(how='all').min(axis=1)
        if side is None:
            events = events.drop('side', axis=1)
            
        return events

    def get_bins(self, events: pd.DataFrame, close: pd.Series) -> pd.DataFrame:
        """1, 0, -1 라벨 생성 (Primary) 또는 Meta 라벨링 (Secondary)"""
        if events.empty:
            return pd.DataFrame()
            
        events_ = events.dropna(subset=['t1'])
        px = events_.index
        px = close.reindex(px, method='bfill')
        
        out = pd.DataFrame(index=events_.index)
        out['ret'] = close.reindex(events_['t1'].values, method='bfill').values / px.values - 1
        
        if 'side' in events_:
            out['ret'] *= events_['side']
            
        out['bin'] = np.sign(out['ret'])
        
        if 'side' in events_:
            out.loc[out['ret'] <= 0, 'bin'] = 0 # Meta-labeling (Success/Fail)
        
        return out

    def train_meta_model(self, X_train: pd.DataFrame, y_train: pd.Series):
        """AdaBoost / LightGBM 메타 레이블링 모델 훈련"""
        self.model.fit(X_train, y_train)

    def predict_meta_prob(self, X_test: pd.DataFrame) -> np.ndarray:
        """확률 반환 (베팅 규모 조절용)"""
        return self.model.predict_proba(X_test)[:, 1]

class GeneticOptimizer:
    def __init__(self, pop_size=20, generations=10):
        self.pop_size = pop_size
        self.generations = generations

    def optimize_barriers(self, fitness_func) -> List[float]:
        """고수익고위험/저수익저위험 파라미터 최적화(단순화된 GA)"""
        # [pt, sl, window]
        population = np.random.uniform(low=[0.5, 0.5, 10], high=[3.0, 3.0, 100], size=(self.pop_size, 3))
        
        for _ in range(self.generations):
            fitnesses = np.array([fitness_func(ind) for ind in population])
            parents = population[np.argsort(fitnesses)[-self.pop_size//2:]]
            
            # Crossover & Mutation
            offspring = []
            for i in range(self.pop_size // 2):
                p1, p2 = parents[np.random.randint(0, len(parents), 2)]
                child = (p1 + p2) / 2.0
                child += np.random.normal(0, 0.1, size=3)
                child = np.clip(child, [0.1, 0.1, 5], [5.0, 5.0, 200])
                offspring.append(child)
            
            population = np.vstack((parents, offspring))
            
        best_idx = np.argmax([fitness_func(ind) for ind in population])
        return population[best_idx].tolist()

if __name__ == "__main__":
    # Test Block
    np.random.seed(42)
    dates = pd.date_range(start='2023-01-01', periods=1000, freq='h')
    prices = pd.Series(np.exp(np.random.randn(1000).cumsum() * 0.01), index=dates)
    
    ml = MetaLabeling()
    vol = ml.get_volatility(prices)
    
    events = ml.get_events(
        close=prices,
        t_events=dates[::10],
        pt_sl=[1.5, 1.5],
        trgt=vol,
        min_ret=0.005,
        t1=pd.Series(dates[10::10], index=dates[:-10:10])
    )
    
    bins = ml.get_bins(events, prices)
    print("Bins generated:\n", bins.head())
