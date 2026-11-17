"""Session model tests: lost packets, device restart, timeline, totals, raw buffer, demo device."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kneil import protocol as P  # noqa: E402
from kneil.demo import DemoDevice  # noqa: E402
from kneil.session import Session, SeqTracker, Totals  # noqa: E402


def live(seq, t_ms, cls=0, smooth=0):
    return P.Live(seq, t_ms, cls, smooth, 90, 3, (0, 0, 1000), 0)


class Sequences(unittest.TestCase):
    def test_lost_and_wrap(self):
        s = SeqTracker()
        self.assertEqual([s.update(q) for q in (65533, 65534, 65535, 0, 3)], [0, 0, 0, 0, 2])
        self.assertEqual(s.lost, 2)

    def test_restart_not_counted(self):
        s = SeqTracker()
        s.update(500)
        self.assertEqual(s.update(0), 0)          # gap of 65036: the device restarted
        self.assertEqual(s.lost, 0)


class SessionModel(unittest.TestCase):
    def test_timeline_and_totals(self):
        s = Session()
        for i, c in enumerate([255, 0, 0, 4, 7, 7, 3, 3, 6, 4]):
            s.handle(live(i, 1000 * (i + 2), c, c), now=float(i))
        s.handle(live(12, 14000, 5, 5), now=12.0)         # two LIVE packets lost before this one
        self.assertEqual(s.live_seq.lost, 2)
        tl = s.timeline.array()
        self.assertEqual(list(tl[-4:]), [4, 255, 255, 5])
        self.assertEqual(s.totals.seconds, [2, 0, 0, 2, 2, 1, 1, 2, 0])
        self.assertEqual(s.totals.transitions, 2)          # 7,7 is one run; 6 is another
        self.assertEqual(s.totals.group_seconds((3, 4)), 4)

    def test_restart(self):
        s = Session()
        s.handle(live(100, 900000), now=0.0)
        log = s.handle(live(0, 3000), now=1.0)
        self.assertIn('device restarted (its clock went back)', log)
        self.assertEqual(s.live_seq.lost, 0)

    def test_transitions(self):
        t = Totals()
        for c in (4, 6, 7, 6, 3, 3, 7, 4, 255, 6):
            t.add(c)
        self.assertEqual(t.transitions, 3)

    def test_raw_buffer_gap_and_lowpass(self):
        s = Session()
        s.handle(P.Raw(0, 1000, tuple((0, 0, 1000) for _ in range(25))), now=0.0)
        s.handle(P.Raw(1, 1500, tuple((0, 0, 1000) for _ in range(25))), now=0.5)
        t, xyz, mag, lp, lp_mag = s.raw.view()
        self.assertEqual(len(t), 50)
        self.assertAlmostEqual(t[-1], 0.0)
        self.assertTrue(np.allclose(mag, 1.0) and np.allclose(lp_mag, 1.0))   # steady state: no start-up jump
        s.handle(P.Raw(3, 2500, tuple((0, 0, 1000) for _ in range(25))), now=1.5)   # packet 2 lost: gap
        t, xyz, mag, lp, lp_mag = s.raw.view()
        self.assertEqual(int(np.isnan(xyz[:, 0]).sum()), 1)
        self.assertEqual(s.raw_seq.lost, 1)


class Demo(unittest.TestCase):
    def test_demo_lines_follow_the_protocol(self):
        d = DemoDevice()
        d.reset(0.0)
        d.command(P.cmd_connect(), 0.0)
        lines = d.poll(0.1) + d.poll(2.0)
        d.command(P.CMD_RAW_START, 2.0)
        lines += d.poll(20.0)
        msgs = [P.parse_line(x) for x in lines]
        self.assertFalse([m for m in msgs if isinstance(m, P.BadLine)])
        lives = [m for m in msgs if isinstance(m, P.Live)]
        raws = [m for m in msgs if isinstance(m, P.Raw)]
        self.assertGreaterEqual(len(lives), 17)
        self.assertGreaterEqual(len(raws), 30)
        self.assertEqual([m.seq for m in lives], list(range(len(lives))))
        self.assertIn(P.BridgeState('connected', 'KNEIL-DEMO', 'DE:A0:00:00:00:01', -48, 247), msgs)


if __name__ == '__main__':
    unittest.main()
