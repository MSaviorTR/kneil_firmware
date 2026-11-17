"""Ground truth, time alignment, confusion table, recording and replay files."""
import csv
import os
import queue
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kneil import protocol as P  # noqa: E402
from kneil.recording import Recorder, read_session  # noqa: E402
from kneil.session import TRUTH_OTHER, ClockMap, Confusion, Session, TruthTrack  # noqa: E402
from kneil.transports import ReplayTransport  # noqa: E402


def live(seq, t_ms, cls, smooth):
    return P.Live(seq, t_ms, cls, smooth, 90, 3, (0, 0, 1000), 0)


class Alignment(unittest.TestCase):
    def test_truth_track(self):
        tr = TruthTrack()
        self.assertIsNone(tr.at(5.0))
        tr.set(0, 10.0)
        tr.set(3, 20.0)
        self.assertEqual((tr.at(9.9), tr.at(10.0), tr.at(19.9), tr.at(25.0)), (None, 0, 0, 3))
        self.assertEqual(tr.current, 3)

    def test_clock_uses_least_delayed_packet(self):
        c = ClockMap()
        c.update(5000, 105.30)                  # 0.30 s late
        c.update(6000, 106.10)                  # 0.10 s late: the best estimate
        c.update(7000, 107.25)
        self.assertAlmostEqual(c.to_session(7000), 107.10)

    def test_window_centre(self):
        # truth walking until session time 100, then standing. Arrival = device time + 50 (no delay).
        s = Session()
        s.truth.set(0, 0.0)
        s.truth.set(4, 100.0)
        # window ending at device 50.5 s (session 100.5): centre at 99.5 -> still walking
        s.handle(live(0, 50500, 4, 0), now=100.5)
        self.assertEqual(s.last_truth_raw, 0)
        # window ending at 51.5 s: centre 100.5 -> standing; the smoothed class is about the window before
        s.handle(live(1, 51500, 4, 4), now=101.5)
        self.assertEqual((s.last_truth_raw, s.last_truth_smooth), (4, 0))
        self.assertEqual(int(s.conf_raw.m[0, 4]), 1)        # truth walking, device standing (too early)
        self.assertEqual(int(s.conf_smooth.m[0, 4]), 1)


class ConfusionTable(unittest.TestCase):
    def test_accuracy(self):
        c = Confusion()
        for truth, cls in [(0, 0)] * 8 + [(0, 1)] * 2 + [(3, 4)] * 5 + [(3, 3)] * 5 + [(4, 4)] * 10 + \
                          [(TRUTH_OTHER, 5)] * 7 + [(None, 0)] * 3:
            c.add(truth, cls)
        self.assertEqual(c.walking(), (80.0, 10))
        self.assertEqual(c.upright_and_still(), (100.0, 20))
        pct, n = c.overall()
        self.assertEqual(n, 30)                              # 'other' and 'no truth' are not counted
        self.assertAlmostEqual(pct, 100 * 23 / 30)
        self.assertAlmostEqual(c.overall(merge_sit_stand=True)[0], 100 * 28 / 30)
        self.assertEqual(Confusion().walking(), (None, 0))


class RecordAndReplay(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            s = Session()
            rec = Recorder(folder, t_start=100.0)
            rec.truth(100.5, 0)
            s.truth.set(0, 100.5)
            lines = ['B,STATE,connected,KNEIL-7AD1,FD:39:81:D5:7A:D1,-40,247', 'I,selftest PASS',
                     P.format_live(live(0, 3000, 0, 255)).strip(),
                     P.format_raw(P.Raw(0, 2500, ((1, 2, 3), (4, 5, 6)))).strip()]
            for i, line in enumerate(lines):
                t = 101.0 + 0.1 * i
                msg = P.parse_line(line)
                s.handle(msg, t)
                rec.line(t, line)
                if isinstance(msg, P.Live):
                    rec.live(msg, t, s.truth.at(t), s.last_truth_raw, s.last_truth_smooth)
                elif isinstance(msg, P.Raw):
                    rec.raw(msg, t, lambda t_ms: s.sample_truth(t_ms, t))
            rec.close()

            with open(rec.base + '_live.csv', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual((rows[0]['class_name'], rows[0]['class_smoothed_name'], rows[0]['truth_now_name']),
                             ('walking', '', 'walking'))
            with open(rec.base + '_raw.csv', newline='') as f:
                raw = list(csv.DictReader(f))
            self.assertEqual([r['t_ms'] for r in raw], ['2500', '2520'])
            self.assertEqual(raw[1]['z_mg'], '6')

            entries = read_session(rec.base + '_session.txt')
            self.assertEqual(entries[0], (0.5, '#TRUTH,0'))
            self.assertEqual(len(entries), 5)

            out: queue.Queue = queue.Queue()
            rp = ReplayTransport(rec.base + '_session.txt', out, speed=30.0)
            rp.open()
            items = []
            deadline = time.time() + 5
            while time.time() < deadline:
                item = out.get(timeout=5)
                items.append(item)
                if item[0] == 'closed':
                    break
            rp.close()
            kinds = [i[0] for i in items]
            self.assertEqual(kinds, ['truth', 'msg', 'msg', 'msg', 'msg', 'info', 'closed'])
            self.assertEqual(items[0][1], 0)
            self.assertIsInstance(items[3][1], P.Live)

    def test_not_a_session_file(self):
        with tempfile.NamedTemporaryFile('w', suffix='_session.txt', delete=False) as f:
            f.write('hello\n')
        try:
            with self.assertRaises(ValueError):
                read_session(f.name)
        finally:
            os.remove(f.name)


if __name__ == '__main__':
    unittest.main()
