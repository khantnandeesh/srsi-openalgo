"""Append completed closes once; preview each live close without advancing RSI state."""
from collections import deque
import math
import numpy as np


class IncrementalSRSI:
    def __init__(self, rsi_period, stoch_period, k_period, d_period):
        self.r, self.s, self.k, self.d = map(int, (rsi_period, stoch_period, k_period, d_period))
        self.count = 0
        self.previous = None
        self.gains, self.losses = [], []
        self.avg_gain = self.avg_loss = None
        self.rsi = deque(maxlen=self.s)
        self.raw = deque(maxlen=self.k)
        self.ks = deque(maxlen=self.d)
        self.last_range = float('nan')

    def _next_rsi(self, close):
        if self.previous is None:
            return float('nan'), None, None
        delta = close - self.previous
        gain, loss = max(delta, 0.0), max(-delta, 0.0)
        if self.avg_gain is None:
            if len(self.gains) + 1 < self.r:
                return float('nan'), None, None
            avg_gain = float(np.mean(self.gains + [gain]))
            avg_loss = float(np.mean(self.losses + [loss]))
        else:
            avg_gain = (self.avg_gain * (self.r - 1) + gain) / self.r
            avg_loss = (self.avg_loss * (self.r - 1) + loss) / self.r
        value = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        return value, avg_gain, avg_loss

    @staticmethod
    def _mean_valid(values):
        clean = [value for value in values if math.isfinite(value)]
        return float(np.mean(clean)) if clean else float('nan')

    def _outputs(self, rsi):
        # Match SDK warm-up and its NaN-filtered rolling-window means.
        index = self.count
        rsi_window = (list(self.rsi) + [rsi])[-self.s:]
        clean = [value for value in rsi_window if math.isfinite(value)]
        raw = float('nan')
        spread = float('nan')
        if index >= self.s - 1 and clean:
            low, high = min(clean), max(clean)
            spread = high - low
            raw = (rsi - low) / spread * 100 if spread != 0 else 50.0
        k = self._mean_valid((list(self.raw) + [raw])[-self.k:]) if index >= self.k - 1 else float('nan')
        d = self._mean_valid((list(self.ks) + [k])[-self.d:]) if index >= self.d - 1 else float('nan')
        return raw, k, d, spread

    def append(self, close):
        close = float(close)
        rsi, gain, loss = self._next_rsi(close)
        raw, k, d, spread = self._outputs(rsi)
        if self.previous is not None and self.avg_gain is None:
            delta = close - self.previous
            self.gains.append(max(delta, 0.0)); self.losses.append(max(-delta, 0.0))
        if gain is not None:
            self.avg_gain, self.avg_loss = gain, loss
            self.gains.clear(); self.losses.clear()
        self.previous = close
        self.count += 1
        self.rsi.append(rsi); self.raw.append(raw); self.ks.append(k)
        self.last_range = spread

    def preview(self, close):
        if self.count + 1 < self.r + self.s:
            return float('nan'), float('nan'), float('nan')
        value, _, _ = self._next_rsi(float(close))
        _, k, d, spread = self._outputs(value)
        return k, d, spread
