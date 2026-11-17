"""Protocol parser tests. The example packets are the same as in ../protocol/test_kneil_proto.c (firmware side)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kneil import protocol as P  # noqa: E402
from kneil.lowpass import LowPass  # noqa: E402

LIVE_BYTES = bytes([0x01, 0x01, 0x01, 0x02, 0x40, 0xE2, 0x01, 0x00, 0x00, 0x04,
                    0x55, 0x03, 0xF4, 0xFF, 0x22, 0x00, 0xDB, 0x03, 0x00, 0x00])
LIVE_LINE = 'L,513,123456,0,4,85,3,-12,34,987,0'
LIVE_V2_BYTES = bytes([0x01, 0x02, 0x01, 0x02, 0x40, 0xE2, 0x01, 0x00, 0x00, 0x03,
                         0x55, 0x03, 0xF4, 0xFF, 0x22, 0x00, 0xDB, 0x03, 0x00, 0x00, 0x01, 0x12])
LIVE_V2_LINE = 'L,513,123456,0,3,85,3,-12,34,987,0,1,18'
SUMMARY_LINE = ('D,2,8,1,2,3,4,5,6,7,8,9,10,11,12,13,2,25,100,1234,1100,-45')
SUMMARY_BYTES = bytes([0x04, 0x02, 0x08, 0x01, 0x00, 0x02, 0x00, 0x03, 0x00, 0x04, 0x00, 0x05, 0x00,
                       0x06, 0x00, 0x07, 0x00, 0x08, 0x00, 0x09, 0x00, 0x0A, 0x00, 0x0B, 0x00,
                       0x0C, 0x00, 0x0D, 0x00, 0x02, 0x00, 0x19, 0x00, 0x64, 0x00, 0xD2, 0x04,
                       0x4C, 0x04, 0xD3, 0xFF])
RAW_BYTES = bytes([0x02, 0x02, 0x07, 0x00, 0xE8, 0x03, 0x00, 0x00,
                   0x01, 0x00, 0xFE, 0xFF, 0xE8, 0x03, 0x00, 0x80, 0xFF, 0x7F, 0x00, 0x00])
RAW_LINE = 'R,7,1000,2,1,-2,1000,-32768,32767,0'


class TextLines(unittest.TestCase):
    def test_live(self):
        m = P.parse_line(LIVE_LINE + '\n')
        self.assertEqual(m, P.Live(513, 123456, 0, 4, 85, 3, (-12, 34, 987), 0))
        self.assertTrue(m.selftest_ok and m.lowpass and not m.small_model)
        self.assertEqual(P.format_live(m), LIVE_LINE + '\n')

    def test_live_no_result_yet(self):
        m = P.parse_line('L,65535,4294967295,255,255,255,0,-32768,32767,0,3700')
        self.assertEqual((m.cls, m.cls_smooth, m.vote_pct, m.battery_mv), (255, 255, 255, 3700))

    def test_live_bad(self):
        for line in ('L,1,2,3', 'L,1,2,9,0,50,3,0,0,0,0', 'L,1,2,0,0,150,3,0,0,0,0', 'L,1,2,0,0,50,3,0,0,99999,0',
                     'L,x,2,0,0,50,3,0,0,0,0'):
            self.assertIsInstance(P.parse_line(line), P.BadLine, line)

    def test_live_v2(self):
        m = P.parse_line(LIVE_V2_LINE + '\n')
        self.assertEqual(m, P.Live(513, 123456, 0, 3, 85, 3, (-12, 34, 987), 0, 1, 0x12, 2))
        self.assertTrue(m.v2)
        self.assertEqual(m.still_posture, 1)                   # posture sitting
        self.assertEqual(P.event_flags(m.events), (False, True, False))
        self.assertEqual(P.format_live(m), LIVE_V2_LINE + '\n')

    def test_live_v1_back_compatible(self):
        m = P.parse_line(LIVE_LINE + '\n')
        self.assertFalse(m.v2)
        self.assertEqual((m.raw_cls, m.events, m.version), (P.CLASS_NONE, 0, 1))
        self.assertEqual(P.format_live(m), LIVE_LINE + '\n')   # a v1 message writes a v1 line

    def test_summary_line(self):
        s = P.parse_line(SUMMARY_LINE + '\n')
        self.assertIsInstance(s, P.Summary)
        self.assertEqual((s.format, s.n_classes), (2, 8))
        self.assertEqual(s.minutes_by_class, (1, 2, 3, 4, 5, 6, 7, 8))
        self.assertEqual((s.minutes_sitting, s.minutes_standing, s.minutes_still_unknown), (9, 10, 11))
        self.assertEqual((s.n_sit_to_stand, s.n_stand_to_sit, s.n_bed_transfer), (12, 13, 2))
        self.assertEqual((s.sts_seconds_x10, s.steps), (25, 100))
        self.assertEqual((s.cadence_x10, s.stairs_cadence_x10, s.sit_tilt_x10), (1234, 1100, -45))
        self.assertEqual(s.minutes_still, 4)
        self.assertEqual(P.format_summary(s), SUMMARY_LINE + '\n')
        self.assertIsInstance(P.parse_line('D,2,8,1,2,3'), P.BadLine)

    def test_raw(self):
        m = P.parse_line(RAW_LINE)
        self.assertEqual(m, P.Raw(7, 1000, ((1, -2, 1000), (-32768, 32767, 0))))
        self.assertEqual(m.n, 2)
        self.assertEqual(P.format_raw(m), RAW_LINE + '\n')
        self.assertIsInstance(P.parse_line('R,7,1000,2,1,-2,1000,5'), P.BadLine)   # n does not match

    def test_info_keeps_commas(self):
        m = P.parse_line('I,dataset a, b, c; V3\r\n')
        self.assertEqual(m, P.Info('dataset a, b, c; V3'))

    def test_bridge_lines(self):
        self.assertEqual(P.parse_line('B,VERSION,KNEIL-bridge 0.2.0 xiao_ble NCS-3.1.0'),
                         P.BridgeVersion('KNEIL-bridge 0.2.0 xiao_ble NCS-3.1.0'))
        self.assertEqual(P.parse_line('B,FOUND,KNEIL-7AD1,FD:39:81:D5:7A:D1,-45'),
                         P.BridgeFound('KNEIL-7AD1', 'FD:39:81:D5:7A:D1', -45))
        self.assertEqual(P.parse_line('B,SCAN_DONE'), P.BridgeScanDone())
        self.assertEqual(P.parse_line('B,STATE,connected,KNEIL-7AD1,FD:39:81:D5:7A:D1,-45,247'),
                         P.BridgeState('connected', 'KNEIL-7AD1', 'FD:39:81:D5:7A:D1', -45, 247))
        self.assertEqual(P.parse_line('B,STATE,idle,,,0,0'), P.BridgeState('idle', '', '', 0, 0))
        self.assertEqual(P.parse_line('B,DROPS,12'), P.BridgeDrops(12))
        self.assertIsInstance(P.parse_line('B,STATE,sleeping,,,0,0'), P.BadLine)
        self.assertIsInstance(P.parse_line('B,HELLO'), P.BadLine)

    def test_empty_and_unknown(self):
        self.assertIsNone(P.parse_line('\n'))
        self.assertIsInstance(P.parse_line('44140,5,5,85,3,-24,990,-178,0'), P.BadLine)   # cut-off line
        self.assertIsInstance(P.parse_line('X,1'), P.BadLine)

    def test_commands(self):
        self.assertEqual(P.cmd_connect(), 'B,CONNECT,AUTO')
        self.assertEqual(P.cmd_connect('FD:39:81:D5:7A:D1'), 'B,CONNECT,FD:39:81:D5:7A:D1')
        self.assertEqual(P.CONTROL_BYTES, {'S': 0x10, 'P': 0x11, 'T': 0x12, 'Q': 0x13, 'Z': 0x14})


class BinaryPackets(unittest.TestCase):
    def test_live(self):
        m = P.decode_packet(LIVE_BYTES)
        self.assertEqual(P.format_live(m), LIVE_LINE + '\n')
        self.assertEqual(P.encode_live(m), LIVE_BYTES)
        with self.assertRaises(ValueError):
            P.decode_packet(LIVE_BYTES[:19])
        with self.assertRaises(ValueError):
            P.decode_packet(LIVE_BYTES[:1] + b'\x02' + LIVE_BYTES[2:])          # other protocol version

    def test_live_v2(self):
        m = P.decode_packet(LIVE_V2_BYTES)
        self.assertEqual(m.version, 2)
        self.assertEqual((m.raw_cls, m.events), (1, 0x12))
        self.assertEqual(P.format_live(m), LIVE_V2_LINE + '\n')
        self.assertEqual(P.encode_live(m), LIVE_V2_BYTES)
        with self.assertRaises(ValueError):
            P.decode_packet(LIVE_V2_BYTES[:21])                                # 21 bytes: neither version
        bad = bytearray(LIVE_V2_BYTES)
        bad[1] = 3
        with self.assertRaises(ValueError):
            P.decode_packet(bytes(bad))                                        # unknown version

    def test_summary_packet(self):
        s = P.decode_packet(SUMMARY_BYTES)
        self.assertEqual(P.format_summary(s), SUMMARY_LINE + '\n')
        self.assertEqual(P.encode_summary(s), SUMMARY_BYTES)
        with self.assertRaises(ValueError):
            P.decode_packet(SUMMARY_BYTES[:-1])
        bad = bytearray(SUMMARY_BYTES)
        bad[1] = 1
        with self.assertRaises(ValueError):
            P.decode_packet(bytes(bad))                                        # only format 2 is accepted

    def test_raw(self):
        m = P.decode_packet(RAW_BYTES)
        self.assertEqual(P.format_raw(m), RAW_LINE + '\n')
        self.assertEqual(P.encode_raw(m), RAW_BYTES)
        with self.assertRaises(ValueError):
            P.decode_packet(RAW_BYTES[:-1])

    def test_info(self):
        self.assertEqual(P.decode_packet(b'\x03selftest PASS'), P.Info('selftest PASS'))
        self.assertEqual(P.encode_info('a\nb'), b'\x03a?b')
        with self.assertRaises(ValueError):
            P.decode_packet(b'\x03')


class LowPassFilter(unittest.TestCase):
    def test_same_as_chip(self):
        # outputs of kneil_lowpass_step() from src/kneil_features.c (C, float32, compiled on the PC) for this input
        chip = [0.2000000, 0.2000000, 0.2000000, 0.2730519, 0.4909224, 0.7526140, 0.9339552, 1.0211247,
                0.9067422, 0.4901215, -0.0163745, -0.3691145, -0.5395572, -0.5843655, -0.5691286, -0.5385813]
        x = [0.2] * 3 + [1.0] * 5 + [-0.5] * 8
        y = LowPass(50.0, 6.0).run(x)
        for a, b in zip(y, chip):
            self.assertAlmostEqual(a, b, places=5)


if __name__ == '__main__':
    unittest.main()
