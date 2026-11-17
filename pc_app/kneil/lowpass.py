"""The 6 Hz input low-pass of the chip (kneil_features.c): 2nd-order Butterworth, bilinear transform with
pre-warping, transposed direct form II, started in steady state for the first sample (no start-up jump)."""
from __future__ import annotations

import math

import numpy as np


class LowPass:
    def __init__(self, fs: float = 50.0, fc: float = 6.0):
        k = math.tan(math.pi * fc / fs)
        r2 = math.sqrt(2.0)
        norm = 1.0 / (1.0 + r2 * k + k * k)
        self.b0 = k * k * norm
        self.b1 = 2.0 * self.b0
        self.b2 = self.b0
        self.a1 = 2.0 * (k * k - 1.0) * norm
        self.a2 = (1.0 - r2 * k + k * k) * norm
        self.reset()

    def reset(self) -> None:
        self.z1 = self.z2 = 0.0
        self.primed = False

    def step(self, x: float) -> float:
        if not self.primed:
            self.z1 = x * (self.b1 + self.b2 - self.a1 - self.a2)
            self.z2 = x * (self.b2 - self.a2)
            self.primed = True
        y = self.b0 * x + self.z1
        self.z1 = self.b1 * x - self.a1 * y + self.z2
        self.z2 = self.b2 * x - self.a2 * y
        return y

    def run(self, xs) -> np.ndarray:
        return np.array([self.step(float(x)) for x in xs])
