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
        self.assertEqual(P.CONTROL_BYTES, {'S': 0x10, 'P': 0x11, 'T': 0x12})


class BinaryPackets(unittest.TestCase):
    def test_live(self):
        m = P.decode_packet(LIVE_BYTES)
        self.assertEqual(P.format_live(m), LIVE_LINE + '\n')
        self.assertEqual(P.encode_live(m), LIVE_BYTES)
        with self.assertRaises(ValueError):
            P.decode_packet(LIVE_BYTES[:19])
        with self.assertRaises(ValueError):
            P.decode_packet(LIVE_BYTES[:1] + b'\x02' + LIVE_BYTES[2:])          # other protocol version

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
