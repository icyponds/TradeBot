"""
3-state Gaussian HMM regime allocator.

Purpose:
- Estimate market regimes from a proxy symbol (default: BTC) using OHLCV-derived features.
- Provide a per-strategy multiplier (regime-aware allocation) that adjusts existing strategy weights.

Design constraints:
- Must be stable and lightweight for live trading.
- Uses only numpy/scipy (already in requirements).
- Diagonal covariance Gaussian emissions; small K (3) keeps compute modest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np


def logsumexp(a: np.ndarray, axis=None, keepdims: bool = False) -> np.ndarray:
    """
    Minimal NumPy-only logsumexp to avoid SciPy runtime dependency.
    """
    a = np.asarray(a, dtype=float)
    a_max = np.max(a, axis=axis, keepdims=True)
    # subtract max for numerical stability; handle -inf max
    stable = np.where(np.isfinite(a_max), a - a_max, a)
    s = np.sum(np.exp(stable), axis=axis, keepdims=True)
    out = np.log(s + 1e-12) + a_max
    if not keepdims and axis is not None:
        out = np.squeeze(out, axis=axis)
    return out


def _safe_normalize(p: np.ndarray, axis: int = -1, eps: float = 1e-12) -> np.ndarray:
    s = np.sum(p, axis=axis, keepdims=True)
    return p / (s + eps)


def _log_gaussian_diag(x: np.ndarray, mean: np.ndarray, var: np.ndarray) -> np.ndarray:
    """
    Log N(x | mean, diag(var)) for each row in x.
    x: (T, D)
    mean: (D,)
    var: (D,)
    returns: (T,)
    """
    var = np.maximum(var, 1e-8)
    diff = x - mean
    return -0.5 * (np.sum(np.log(2.0 * np.pi * var)) + np.sum((diff * diff) / var, axis=1))


@dataclass
class RegimeResult:
    regime: str  # "range" | "trend" | "high_vol"
    probs: Dict[str, float]


class GaussianHMM3:
    """
    Minimal Gaussian HMM with diagonal covariance, for small K=3.
    """

    def __init__(self, n_states: int = 3, n_iter: int = 12, seed: int = 7):
        self.n_states = n_states
        self.n_iter = n_iter
        self.rng = np.random.default_rng(seed)

        self.pi: Optional[np.ndarray] = None      # (K,)
        self.A: Optional[np.ndarray] = None       # (K, K)
        self.means: Optional[np.ndarray] = None   # (K, D)
        self.vars: Optional[np.ndarray] = None    # (K, D)

    def fit(self, X: np.ndarray) -> None:
        """
        Fit HMM parameters with EM (Baum-Welch).
        X: (T, D)
        """
        T, D = X.shape
        K = self.n_states
        if T < 30:
            raise ValueError("Not enough observations to fit HMM")

        # Initialize
        self.pi = np.full(K, 1.0 / K, dtype=float)
        self.A = _safe_normalize(self.rng.random((K, K)), axis=1)

        # Simple initialization via quantiles on first feature (trend proxy)
        q = np.quantile(X[:, 0], [0.2, 0.6])
        labels = np.zeros(T, dtype=int)
        labels[X[:, 0] > q[0]] = 1
        labels[X[:, 0] > q[1]] = 2

        self.means = np.zeros((K, D), dtype=float)
        self.vars = np.zeros((K, D), dtype=float)
        for k in range(K):
            mask = labels == k
            if mask.sum() < 5:
                # fallback random mean
                self.means[k] = X[self.rng.integers(0, T)]
                self.vars[k] = np.var(X, axis=0) + 1e-6
            else:
                self.means[k] = np.mean(X[mask], axis=0)
                self.vars[k] = np.var(X[mask], axis=0) + 1e-6

        # EM
        for _ in range(self.n_iter):
            logB = self._log_emissions(X)  # (T, K)
            log_alpha, log_beta, log_likelihood = self._forward_backward(logB)

            # gamma: (T, K)
            log_gamma = log_alpha + log_beta
            log_gamma = log_gamma - logsumexp(log_gamma, axis=1, keepdims=True)
            gamma = np.exp(log_gamma)

            # xi: (T-1, K, K)
            xi = np.zeros((T - 1, K, K), dtype=float)
            for t in range(T - 1):
                m = (
                    log_alpha[t, :, None]
                    + np.log(self.A + 1e-12)[None, :, :]
                    + logB[t + 1, None, :]
                    + log_beta[t + 1, None, :]
                )
                m = m - logsumexp(m)
                xi[t] = np.exp(m)

            # M-step
            self.pi = _safe_normalize(gamma[0], axis=0)
            A_num = np.sum(xi, axis=0)
            self.A = _safe_normalize(A_num, axis=1)

            for k in range(K):
                w = gamma[:, k][:, None]  # (T,1)
                w_sum = np.sum(w)
                if w_sum < 1e-8:
                    continue
                mu = np.sum(w * X, axis=0) / w_sum
                var = np.sum(w * (X - mu) ** 2, axis=0) / w_sum
                self.means[k] = mu
                self.vars[k] = np.maximum(var, 1e-8)

            # optional: could early-stop on log_likelihood improvement
            _ = log_likelihood

    def predict_proba_last(self, X: np.ndarray) -> np.ndarray:
        """
        Return filtered state probabilities for the last observation.
        X: (T, D)
        """
        if self.pi is None or self.A is None or self.means is None or self.vars is None:
            raise ValueError("Model not fit")
        logB = self._log_emissions(X)
        log_alpha = self._forward(logB)
        probs_last = np.exp(log_alpha[-1] - logsumexp(log_alpha[-1]))
        return probs_last

    def _log_emissions(self, X: np.ndarray) -> np.ndarray:
        T, _ = X.shape
        K = self.n_states
        logB = np.zeros((T, K), dtype=float)
        for k in range(K):
            logB[:, k] = _log_gaussian_diag(X, self.means[k], self.vars[k])
        return logB

    def _forward(self, logB: np.ndarray) -> np.ndarray:
        T, K = logB.shape
        logA = np.log(self.A + 1e-12)
        log_pi = np.log(self.pi + 1e-12)
        log_alpha = np.zeros((T, K), dtype=float)
        log_alpha[0] = log_pi + logB[0]
        for t in range(1, T):
            log_alpha[t] = logB[t] + logsumexp(log_alpha[t - 1][:, None] + logA, axis=0)
        return log_alpha

    def _forward_backward(self, logB: np.ndarray) -> Tuple[np.ndarray, np.ndarray, float]:
        T, K = logB.shape
        logA = np.log(self.A + 1e-12)
        log_pi = np.log(self.pi + 1e-12)

        log_alpha = np.zeros((T, K), dtype=float)
        log_alpha[0] = log_pi + logB[0]
        for t in range(1, T):
            log_alpha[t] = logB[t] + logsumexp(log_alpha[t - 1][:, None] + logA, axis=0)

        log_beta = np.zeros((T, K), dtype=float)
        log_beta[-1] = 0.0
        for t in range(T - 2, -1, -1):
            log_beta[t] = logsumexp(logA + logB[t + 1][None, :] + log_beta[t + 1][None, :], axis=1)

        ll = float(logsumexp(log_alpha[-1]))
        return log_alpha, log_beta, ll


class RegimeAllocator:
    """
    Wraps the HMM and maps states to human-readable regimes + strategy multipliers.
    """

    REGIME_NAMES = ("range", "trend", "high_vol")

    def __init__(
        self,
        lookback: int = 220,
        retrain_minutes: int = 30,
        hysteresis_threshold: float = 0.60,
        min_switch_minutes: int = 15,
        strategy_multipliers: Optional[Dict[str, Dict[str, float]]] = None,
        seed: int = 7,
    ):
        self.lookback = int(lookback)
        self.retrain_minutes = int(retrain_minutes)
        self.hysteresis_threshold = float(hysteresis_threshold)
        self.min_switch_minutes = int(min_switch_minutes)
        self.model = GaussianHMM3(n_states=3, n_iter=12, seed=seed)

        self._last_fit_ts: float = 0.0
        self._last_regime: str = "range"
        self._last_regime_ts: float = 0.0

        # default multipliers (conservative)
        self.strategy_multipliers = strategy_multipliers or {
            "range": {"ou_mean_reversion": 1.20, "stat_arb": 1.10, "momentum_factor": 0.85},
            "trend": {"momentum_factor": 1.25, "ou_mean_reversion": 0.75, "stat_arb": 0.85},
            "high_vol": {"momentum_factor": 0.80, "ou_mean_reversion": 0.80, "stat_arb": 0.80},
        }

    @staticmethod
    def build_features_from_ohlcv(df) -> np.ndarray:
        """
        Build observation matrix X from OHLCV dataframe.
        Features:
          - trend_strength: abs(rolling_mean(returns)) / (rolling_std(returns) + eps)
          - realized_vol: rolling_std(returns)
        """
        close = np.asarray(df["close"], dtype=float)
        r = np.diff(close) / np.maximum(close[:-1], 1e-12)
        if r.size < 40:
            raise ValueError("Not enough returns for features")

        w = 24  # ~6h at 15m bars; stable enough, responsive enough
        # rolling mean/std
        # pad with nan then drop
        rm = np.convolve(r, np.ones(w) / w, mode="valid")
        # rolling std via windowed var (approx): compute with stride windows
        # for simplicity and stability, use explicit windows (small T only)
        rv = np.array([np.std(r[i : i + w]) for i in range(0, len(r) - w + 1)], dtype=float)

        trend = np.abs(rm) / (rv + 1e-8)
        X = np.column_stack([trend, rv])

        # standardize features (z-score) for numerical stability
        mu = np.mean(X, axis=0)
        sd = np.std(X, axis=0) + 1e-8
        X = (X - mu) / sd
        return X

    def update(self, X: np.ndarray, now_ts: float) -> RegimeResult:
        """
        Update regime from observation matrix X. Returns current regime + probs.
        """
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[0] < 40:
            raise ValueError("Invalid observation matrix")

        X = X[-self.lookback :]

        # Refit periodically
        if (now_ts - self._last_fit_ts) >= (self.retrain_minutes * 60) or self._last_fit_ts == 0.0:
            self.model.fit(X)
            self._last_fit_ts = now_ts

        p = self.model.predict_proba_last(X)

        # Map latent states -> regimes by interpreting emission means:
        # We assume state with highest mean vol feature corresponds to "high_vol".
        # Among remaining, higher trend feature corresponds to "trend", lower to "range".
        means = self.model.means  # (3,2) in standardized space, still comparable
        vol_rank = np.argsort(means[:, 1])
        high_vol_state = int(vol_rank[-1])
        remaining = [s for s in range(3) if s != high_vol_state]
        trend_state = int(remaining[np.argmax(means[remaining, 0])])
        range_state = int([s for s in remaining if s != trend_state][0])

        state_to_regime = {range_state: "range", trend_state: "trend", high_vol_state: "high_vol"}
        probs = {
            state_to_regime[0]: float(p[0]),
            state_to_regime[1]: float(p[1]),
            state_to_regime[2]: float(p[2]),
        }

        # Choose regime with hysteresis + minimum dwell time
        best_state = int(np.argmax(p))
        best_regime = state_to_regime[best_state]
        best_prob = float(p[best_state])

        can_switch = (now_ts - self._last_regime_ts) >= (self.min_switch_minutes * 60)
        if best_regime != self._last_regime and best_prob >= self.hysteresis_threshold and can_switch:
            self._last_regime = best_regime
            self._last_regime_ts = now_ts
        elif self._last_regime_ts == 0.0:
            self._last_regime = best_regime
            self._last_regime_ts = now_ts

        # Normalize probs into fixed keys for output readability
        out_probs = {
            "range": probs.get("range", 0.0),
            "trend": probs.get("trend", 0.0),
            "high_vol": probs.get("high_vol", 0.0),
        }
        return RegimeResult(regime=self._last_regime, probs=out_probs)

    def get_multiplier(self, strategy_name: str, regime: str) -> float:
        return float(self.strategy_multipliers.get(regime, {}).get(strategy_name, 1.0))


