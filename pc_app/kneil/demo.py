"""Demo mode: a simulated XIAO bridge with a simulated KNEIL device behind it. It produces the protocol's text
lines in real time (L every second, R every 0.5 s while raw streaming is on, I and B lines), so the window can
be tried without hardware. The motions are made up, not recordings."""
from __future__ import annotations

import math
import random
from typing import List

import numpy as np

from . import protocol as P

# (class, seconds): a day in fast motion; it repeats
SCENARIO = ((4, 15), (0, 40), (4, 8), (7, 3), (3, 20), (8, 4), (5, 25), (8, 4), (3, 8), (6, 3),
            (4, 6), (1, 12), (4, 5), (2, 12))

# still posture of each class in the device frame (mg): x anterior, y left, z superior
POSTURE = {0: (0, 0, 1000), 1: (0, 0, 1000), 2: (0, 0, 1000), 3: (250, 0, 968), 4: (0, 0, 1000),
           5: (970, 0, 240), 6: (500, 0, 866), 7: (500, 0, 866), 8: (700, 500, 500)}

DEMO_NAME = 'KNEIL-DEMO'
DEMO_ADDRESS = 'DE:A0:00:00:00:01'


class DemoDevice:
    def __init__(self, seed: int = 1):
        self.rng = random.Random(seed)
        self.reset(0.0)

    def reset(self, now: float) -> None:
        self.state = 'idle'
        self.state_until = 0.0
        self.scan_until = 0.0
        self.found_sent = False
        self.raw_on = False
        self.next_state_line = 0.0
        self.boot = now
        self.n = 0                       # samples made since the device "booted"
        self.posture = np.array(POSTURE[4], dtype=float)
        self.recent: List[tuple] = []    # last 2 s of samples (mg)
        self.raw_buf: List[tuple] = []
        self.raw_t0 = 0
        self.live_seq = self.raw_seq = 0
        self.hist: List[int] = []
        self.out: List[str] = ['B,VERSION,KNEIL-bridge demo (simulated)', self._state_line()]

    # --- bridge -------------------------------------------------------------------------------
    def _state_line(self) -> str:
        on = self.state in ('connecting', 'connected')
        return 'B,STATE,%s,%s,%s,%d,%d' % (self.state, DEMO_NAME if on else '', DEMO_ADDRESS if on else '',
                                           -48 if on else 0, 247 if self.state == 'connected' else 0)

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.out.append(self._state_line())

    def command(self, line: str, now: float) -> None:
        if line == P.CMD_SCAN:
            self.scan_until = now + 5.0
            self.found_sent = False
            if self.state == 'idle':
                self._set_state('scanning')
        elif line.startswith('B,CONNECT,'):
            if self.state != 'connected':
                self._set_state('connecting')
                self.state_until = now + 1.2
        elif line == P.CMD_DISCONNECT:
            self._set_state('idle')
        elif self.state == 'connected' and line in P.CONTROL_BYTES:
            if line == P.CMD_RAW_START:
                self.raw_on, self.raw_buf = True, []
            elif line == P.CMD_RAW_STOP:
                self.raw_on = False
            else:
                self.out.append('I,selftest PASS vectors=27 feature_fail=0 class_mismatch=0 (demo)')

    # --- simulated motion -----------------------------------------------------------------------
    def _true_class(self, t: float) -> int:
        period = sum(s for _, s in SCENARIO)
        t = t % period
        for cls, sec in SCENARIO:
            if t < sec:
                return cls
            t -= sec
        return 4

    def _sample(self, t: float) -> tuple:
        cls = self._true_class(t)
        target = np.array(POSTURE[cls], dtype=float)
        self.posture += (target - self.posture) * (1.0 / (0.6 * P.FS_HZ))     # smooth posture changes
        a = self.posture.copy()
        w = 2 * math.pi * t
        if cls == 0:            # walking, about 0.95 strides per second
            a += (350 * math.sin(0.95 * w), 60 * math.sin(0.95 * w + 1),
                  120 * math.sin(0.95 * w) + 250 * math.sin(1.9 * w + 0.5))
        elif cls == 1:          # stairs up: slower, larger swing of the shank
            a += (450 * math.sin(0.75 * w), 80 * math.sin(0.75 * w + 1), 200 * math.sin(1.5 * w))
        elif cls == 2:          # stairs down: impacts
            a += (300 * math.sin(0.8 * w), 80 * math.sin(0.8 * w + 2), 400 * max(0.0, math.sin(1.6 * w)) ** 3)
        elif cls == 8:          # bed transfer: rolling
            a += (0, 400 * math.sin(0.25 * w), 0)
        a += [self.rng.gauss(0, 8) for _ in range(3)]
        return tuple(int(round(v)) for v in a)

    def _classify(self, t_end: float) -> P.Live:
        true = self._true_class(t_end - 1.0)        # class at the centre of the 2 s window
        cls = true
        r = self.rng.random()
        if true in P.UPRIGHT_AND_STILL and r < 0.25:
            cls = 7 - true                          # sitting <-> standing, the known weakness
        elif r < 0.05:
            cls = self.rng.randrange(P.N_CLASSES)
        self.hist = (self.hist + [cls])[-3:]
        h = self.hist
        smooth = P.CLASS_NONE
        if len(h) == 2:
            smooth = h[0]
        elif len(h) == 3:                           # same majority-of-3 rule as the firmware
            smooth = h[0] if (h[0] == h[2] and h[0] != h[1]) else h[1]
        vote = self.rng.randint(80, 100) if cls == true else self.rng.randint(40, 70)
        mean = tuple(int(round(sum(s[k] for s in self.recent) / len(self.recent))) for k in range(3))
        live = P.Live(self.live_seq & 0xFFFF, int(t_end * 1000) & 0xFFFFFFFF, cls, smooth, vote,
                      P.FLAG_SELFTEST_OK | P.FLAG_LOWPASS, mean, 0)
        self.live_seq += 1
        return live

    # --- time ---------------------------------------------------------------------------------
    def poll(self, now: float) -> List[str]:
        if self.state == 'scanning' or self.scan_until > now:
            if not self.found_sent and now > self.scan_until - 4.0:
                self.out.append('B,FOUND,%s,%s,-48' % (DEMO_NAME, DEMO_ADDRESS))
                self.found_sent = True
        if self.scan_until and now >= self.scan_until:
            self.scan_until = 0.0
            self.out.append('B,SCAN_DONE')
            if self.state == 'scanning':
                self._set_state('idle')
        if self.state == 'connecting' and now >= self.state_until:
            self._set_state('connected')
            self.next_state_line = now + 2.0
            self.out.append('I,KNEIL demo device: simulated data (not a recording)')
            self.out.append('I,model forest; forest 20 trees 18900 nodes; tree 1117 nodes')
        if self.state == 'connected' and now >= self.next_state_line:
            self.out.append(self._state_line())
            self.next_state_line = now + 2.0

        # the device samples all the time; data goes out only while connected
        due = int((now - self.boot) * P.FS_HZ)
        while self.n < due:
            t = self.n / P.FS_HZ
            s = self._sample(t)
            self.n += 1
            self.recent = (self.recent + [s])[-2 * P.FS_HZ:]
            if self.state != 'connected':
                continue
            if self.raw_on:
                if not self.raw_buf:
                    self.raw_t0 = int(t * 1000)
                self.raw_buf.append(s)
                if len(self.raw_buf) == P.RAW_N_NORMAL:
                    self.out.append(P.format_raw(P.Raw(self.raw_seq & 0xFFFF, self.raw_t0, tuple(self.raw_buf))))
                    self.raw_seq += 1
                    self.raw_buf = []
            if self.n >= 2 * P.FS_HZ and self.n % P.FS_HZ == 0:
                self.out.append(P.format_live(self._classify(self.n / P.FS_HZ)))
        out, self.out = self.out, []
        return out
