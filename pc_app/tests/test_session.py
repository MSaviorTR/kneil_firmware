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


class V2Session(unittest.TestCase):
    """Protocol v2: 8 classes, 4 s window, event flags, posture, daily summary."""

    @staticmethod
    def live2(seq, t_ms, cls, smooth=None, events=0, raw=None):
        return P.Live(seq, t_ms, cls, cls if smooth is None else smooth, 80, 3, (0, 0, 1000), 0,
                      cls if raw is None else raw, events, 2)

    def test_device_column_mapping(self):
        self.assertEqual(P.device_column(3, 1 << P.EV_POSTURE_SHIFT, 2), 3)     # still + sitting
        self.assertEqual(P.device_column(3, 2 << P.EV_POSTURE_SHIFT, 2), 4)     # still + standing
        self.assertEqual(P.device_column(3, 0, 2), P.DEVICE_STILL_UNKNOWN)      # still + unknown posture
        self.assertEqual([P.device_column(c, 0, 2) for c in (0, 1, 2, 4, 5, 6, 7)], [0, 1, 2, 5, 6, 7, 8])
        self.assertEqual(P.device_column(3, 0, 2, unknown_col=3), 3)            # the totals table keeps its mapping
        self.assertEqual(P.device_column(4, 0, 1), 4)                           # v1 unchanged

    def test_confusion_counts_still_by_posture(self):
        s = Session()
        s.truth.set(3, 0.0)                                     # the user sits from t = 0
        s.handle(self.live2(0, 12000, 3, events=1 << P.EV_POSTURE_SHIFT), now=12.0)
        self.assertEqual(s.version, 2)
        self.assertEqual(s.conf_raw.m[3, 3], 1)                 # truth sitting, device still + sitting
        s2 = Session()
        s2.truth.set(4, 0.0)                                    # the user stands
        s2.handle(self.live2(0, 12000, 3, events=2 << P.EV_POSTURE_SHIFT), now=12.0)
        self.assertEqual(s2.conf_raw.m[4, 4], 1)                # truth standing, device still + standing
        s3 = Session()
        s3.truth.set(3, 0.0)
        s3.handle(self.live2(0, 12000, 3, events=0), now=12.0)  # still, posture unknown
        self.assertEqual(s3.conf_raw.m[3, P.DEVICE_STILL_UNKNOWN], 1)
        self.assertEqual(s3.conf_raw.m[3, 3], 0)                # never counted as sitting
        self.assertEqual(s3.conf_raw.sitting(), (None, 0))      # no window with a known posture
        self.assertEqual(s3.conf_raw.posture_unknown(), (100.0, 1))
        self.assertEqual(s3.conf_raw.upright_and_still(), (100.0, 1))
        s4 = Session()
        s4.truth.set(3, 0.0)
        s4.handle(self.live2(0, 12000, 3, events=1 << P.EV_POSTURE_SHIFT), now=12.0)
        s4.handle(self.live2(1, 13000, 3, events=0), now=13.0)
        sit, n = s4.conf_raw.sitting()
        self.assertEqual((round(sit, 1), n), (100.0, 1))        # the unknown-posture window is not in the denominator
        self.assertEqual(s4.conf_raw.posture_unknown(), (50.0, 2))

    def test_events_come_from_the_flags(self):
        t = Totals()
        t.add(3, P.EV_SIT_TO_STAND, 2)
        t.add(3, P.EV_STAND_TO_SIT | (1 << P.EV_POSTURE_SHIFT), 2)
        t.add(3, P.EV_BED_TRANSFER, 2)
        t.add(3, 0, 2)
        self.assertEqual((t.n_sit_to_stand, t.n_stand_to_sit, t.n_bed_transfer), (1, 1, 1))
        self.assertEqual(t.transitions, 2)     # sit-to-stand + stand-to-sit, not the bed transfer
        self.assertEqual((t.seconds_sitting, t.seconds_standing, t.seconds_still_unknown), (1, 0, 3))

    def test_v2_window_centre_is_two_seconds(self):
        s = Session()
        s.truth.set(0, 0.0)                     # walking from t = 0
        s.truth.set(3, 10.0)                    # sitting from t = 10 s
        s.handle(self.live2(0, 12000, 3), now=12.0)
        # the 4 s window that ended at 12 s has its centre at 10 s -> truth sitting (3);
        # the smoothed class belongs to the window that ended at 11 s (centre 9 s) -> walking (0)
        self.assertEqual(s.last_truth_raw, 3)
        self.assertEqual(s.last_truth_smooth, 0)

    def test_summary_message(self):
        s = Session()
        s.handle(P.Summary(2, 8, (1, 2, 3, 4, 5, 6, 7, 8), 9, 10, 11, 12, 13, 2, 25, 100, 1234, 1100, -45),
                 now=1.0)
        self.assertEqual(s.summary.minutes_still, 4)
        self.assertIn('daily summary', s.handle(P.Summary(2, 8, (0,) * 8, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0),
                                                now=2.0)[0])


    def test_still_with_unknown_posture_is_its_own_column(self):
        s = Session()
        s.truth.set(3, 0.0)
        s.handle(self.live2(0, 12000, 3, events=0), now=12.0)
        self.assertEqual(s.conf_raw.m[3, P.DEVICE_STILL_UNKNOWN], 1)
        self.assertEqual(int(s.conf_raw.m[3, 3]), 0)
        self.assertEqual(s.conf_raw.sitting(), (None, 0))
        self.assertEqual(s.conf_raw.standing(), (None, 0))
        self.assertEqual(s.conf_raw.posture_unknown(), (100.0, 1))
        self.assertEqual(s.conf_raw.upright_and_still(), (100.0, 1))

    def test_known_posture_sitting_and_standing(self):
        s = Session()
        s.truth.set(3, 0.0)
        s.handle(self.live2(0, 12000, 3, events=1 << P.EV_POSTURE_SHIFT), now=12.0)
        s.handle(self.live2(1, 13000, 3, events=0), now=13.0)          # same truth, posture unknown
        sit, n = s.conf_raw.sitting()
        self.assertEqual((round(sit, 1), n), (100.0, 1))              # denominator: known postures only
        self.assertEqual(s.conf_raw.posture_unknown(), (50.0, 2))
        s2 = Session()
        s2.truth.set(4, 0.0)
        s2.handle(self.live2(0, 12000, 3, events=2 << P.EV_POSTURE_SHIFT), now=12.0)
        self.assertEqual(s2.conf_raw.standing(), (100.0, 1))
        self.assertEqual(s2.conf_raw.sitting(), (None, 0))

    def test_totals_keep_the_old_still_split(self):
        t = Totals()
        t.add(3, 1 << P.EV_POSTURE_SHIFT, 2)
        t.add(3, 2 << P.EV_POSTURE_SHIFT, 2)
        t.add(3, 0, 2)                                                # unknown posture still counts as still time
        self.assertEqual((t.seconds_sitting, t.seconds_standing, t.seconds_still_unknown), (1, 1, 1))
        self.assertEqual(sum(t.seconds[:2]), 0)
        self.assertEqual(t.seconds[3] + t.seconds[4] + 0, 3)           # 1 sitting + 1 standing + 1 unknown (as sitting)


if __name__ == '__main__':
    unittest.main()
