/* Host unit test for kneil_proto.c (gcc on the PC):
 *   gcc -std=c99 -Wall -Wextra -o test_kneil_proto protocol/test_kneil_proto.c protocol/kneil_proto.c
 * The example packets and lines below can also be used by the PC app's tests (same bytes, same lines). */
#include <stdio.h>
#include <string.h>
#include "kneil_proto.h"

static int fails;

#define CHECK(cond) do { if (!(cond)) { printf("FAIL line %d: %s\n", __LINE__, #cond); fails++; } } while (0)

int main(void)
{
	uint8_t buf[256];
	char line[KNEIL_LINE_MAX];
	kneil_live_t live;
	kneil_live_t l2;

	/* ---- LIVE v2: seq 513, t 123456 ms, walking (0) after the stairs rule, still (3) smoothed, 85 % of the
	 * raw class (stairs_up = 1), flags = self-test ok + low-pass, mean (-12, 34, 987) mg, no battery,
	 * events = stand-to-sit counted (bit1) + posture sitting (bits 4-5 = 1) */
	memset(&live, 0, sizeof(live));
	live.version = 2;
	live.seq = 513;
	live.t_ms = 123456;
	live.cls = 0;
	live.cls_smooth = 3;
	live.vote_pct = 85;
	live.flags = 0x03;
	live.mean_mg[0] = -12; live.mean_mg[1] = 34; live.mean_mg[2] = 987;
	live.battery_mv = 0;
	live.raw_cls = 1;
	live.events = KNEIL_EV_STAND_TO_SIT | (KNEIL_POSTURE_SITTING << KNEIL_EV_POSTURE_SHIFT);
	const uint8_t live2_bytes[KNEIL_LIVE_V2_LEN] = {0x01, 0x02, 0x01, 0x02, 0x40, 0xE2, 0x01, 0x00, 0x00, 0x03,
							0x55, 0x03, 0xF4, 0xFF, 0x22, 0x00, 0xDB, 0x03, 0x00, 0x00, 0x01, 0x12};
	CHECK(kneil_live_encode(&live, buf, sizeof(buf)) == KNEIL_LIVE_V2_LEN);
	CHECK(memcmp(buf, live2_bytes, KNEIL_LIVE_V2_LEN) == 0);
	CHECK(kneil_live_decode(live2_bytes, KNEIL_LIVE_V2_LEN, &l2) == 0);
	CHECK(l2.version == 2 && l2.raw_cls == 1 && l2.events == 0x12);
	CHECK(kneil_live_to_line(&l2, line, sizeof(line)) > 0);
	CHECK(strcmp(line, "L,513,123456,0,3,85,3,-12,34,987,0,1,18\n") == 0);

	/* ---- LIVE v1 (20 bytes, 9 classes) still decodes; the line stays v1 (no raw/event values) */
	const uint8_t live1_bytes[KNEIL_LIVE_V1_LEN] = {0x01, 0x01, 0x01, 0x02, 0x40, 0xE2, 0x01, 0x00, 0x00, 0x03,
							0x55, 0x03, 0xF4, 0xFF, 0x22, 0x00, 0xDB, 0x03, 0x00, 0x00};
	CHECK(kneil_live_decode(live1_bytes, KNEIL_LIVE_V1_LEN, &l2) == 0);
	CHECK(l2.version == 1 && l2.raw_cls == KNEIL_CLASS_NONE && l2.events == 0);
	CHECK(kneil_live_to_line(&l2, line, sizeof(line)) > 0);
	CHECK(strcmp(line, "L,513,123456,0,3,85,3,-12,34,987,0\n") == 0);

	/* ---- lengths and versions */
	CHECK(kneil_live_decode(live2_bytes, 19, &l2) == -1);          /* wrong length */
	CHECK(kneil_live_decode(live2_bytes, 21, &l2) == -1);
	uint8_t bad[KNEIL_LIVE_V2_LEN];
	memcpy(bad, live2_bytes, sizeof(bad));
	bad[1] = 3;
	CHECK(kneil_live_decode(bad, sizeof(bad), &l2) == -1);         /* unknown protocol version */
	memcpy(bad, live2_bytes, sizeof(bad));
	bad[0] = 0x02;
	CHECK(kneil_live_decode(bad, sizeof(bad), &l2) == -1);         /* not a LIVE packet */
	CHECK(kneil_live_encode(&live, buf, KNEIL_LIVE_V1_LEN) == -1); /* buffer too small */

	/* ---- LIVE with no result yet */
	memset(&live, 0, sizeof(live));
	live.version = 2;
	live.seq = 65535; live.t_ms = 4294967295u;
	live.cls = KNEIL_CLASS_NONE; live.cls_smooth = KNEIL_CLASS_NONE; live.vote_pct = KNEIL_VOTE_NONE;
	live.mean_mg[0] = -32768; live.mean_mg[1] = 32767; live.mean_mg[2] = 0;
	live.battery_mv = 3700; live.raw_cls = KNEIL_CLASS_NONE; live.events = 0;
	CHECK(kneil_live_encode(&live, buf, sizeof(buf)) == KNEIL_LIVE_V2_LEN);
	CHECK(kneil_live_decode(buf, KNEIL_LIVE_V2_LEN, &l2) == 0);
	CHECK(kneil_live_to_line(&l2, line, sizeof(line)) > 0);
	CHECK(strcmp(line, "L,65535,4294967295,255,255,255,0,-32768,32767,0,3700,255,0\n") == 0);

	/* ---- RAW: seq 7, first sample at 1000 ms, 2 samples */
	kneil_raw_t raw = {7, 1000, 2, {{1, -2, 1000}, {-32768, 32767, 0}}};
	const uint8_t raw_bytes[] = {0x02, 0x02, 0x07, 0x00, 0xE8, 0x03, 0x00, 0x00,
				     0x01, 0x00, 0xFE, 0xFF, 0xE8, 0x03, 0x00, 0x80, 0xFF, 0x7F, 0x00, 0x00};
	CHECK(kneil_raw_encode(&raw, buf, sizeof(buf)) == (int)sizeof(raw_bytes));
	CHECK(memcmp(buf, raw_bytes, sizeof(raw_bytes)) == 0);
	kneil_raw_t r2;
	CHECK(kneil_raw_decode(raw_bytes, sizeof(raw_bytes), &r2) == 0);
	CHECK(kneil_raw_to_line(&r2, line, sizeof(line)) > 0);
	CHECK(strcmp(line, "R,7,1000,2,1,-2,1000,-32768,32767,0\n") == 0);
	CHECK(kneil_raw_decode(raw_bytes, sizeof(raw_bytes) - 1, &r2) == -1);  /* length does not match n */

	/* RAW normal (25 samples = 158 bytes) and the largest one (39 samples) */
	raw.n = KNEIL_RAW_N_NORMAL;
	CHECK(kneil_raw_encode(&raw, buf, sizeof(buf)) == 158);
	raw.n = KNEIL_RAW_N_MAX;
	for (int i = 0; i < KNEIL_RAW_N_MAX; i++) {
		raw.xyz_mg[i][0] = -32768; raw.xyz_mg[i][1] = -32768; raw.xyz_mg[i][2] = -32768;
	}
	CHECK(kneil_raw_encode(&raw, buf, sizeof(buf)) == 8 + 6 * KNEIL_RAW_N_MAX);
	CHECK(kneil_raw_decode(buf, 8 + 6 * KNEIL_RAW_N_MAX, &r2) == 0);
	const int nraw = kneil_raw_to_line(&r2, line, sizeof(line));
	CHECK(nraw > 0 && nraw < KNEIL_LINE_MAX);
	CHECK(kneil_raw_to_line(&r2, line, 100) == -1);                  /* output too small */
	CHECK(KNEIL_RAW_N_FOR_MTU(247) == 39 && KNEIL_RAW_N_FOR_MTU(23) == 2);

	/* ---- SUMMARY: format 2, 8 classes, minutes 1..8, sitting 9, standing 10, unknown 11, events 12/13/2,
	 * mean sit-to-stand 2.5 s, 100 steps, cadence 123.4, stairs cadence 110.0, seated tilt -4.5 deg */
	kneil_summary_t sum;
	memset(&sum, 0, sizeof(sum));
	sum.format = KNEIL_SUMMARY_FORMAT;
	sum.n_classes = KNEIL_SUMMARY_CLASSES;
	for (int k = 0; k < KNEIL_SUMMARY_CLASSES; k++) {
		sum.minutes_by_class[k] = (uint16_t)(k + 1);
	}
	sum.minutes_sitting = 9; sum.minutes_standing = 10; sum.minutes_still_unknown = 11;
	sum.n_sit_to_stand = 12; sum.n_stand_to_sit = 13; sum.n_bed_transfer = 2;
	sum.sts_seconds_x10 = 25; sum.steps = 100; sum.cadence_x10 = 1234; sum.stairs_cadence_x10 = 1100;
	sum.sit_tilt_x10 = -45;
	CHECK(kneil_summary_encode(&sum, buf, sizeof(buf)) == KNEIL_SUMMARY_LEN);
	CHECK(buf[0] == KNEIL_PKT_SUMMARY && buf[1] == 2 && buf[2] == 8);
	const uint8_t sum_bytes[KNEIL_SUMMARY_LEN] = {0x04, 0x02, 0x08, 0x01, 0x00, 0x02, 0x00, 0x03, 0x00,
						      0x04, 0x00, 0x05, 0x00, 0x06, 0x00, 0x07, 0x00, 0x08, 0x00,
						      0x09, 0x00, 0x0A, 0x00, 0x0B, 0x00, 0x0C, 0x00, 0x0D, 0x00,
						      0x02, 0x00, 0x19, 0x00, 0x64, 0x00, 0xD2, 0x04, 0x4C, 0x04, 0xD3, 0xFF};
	CHECK(memcmp(buf, sum_bytes, KNEIL_SUMMARY_LEN) == 0);
	kneil_summary_t s2;
	CHECK(kneil_summary_decode(sum_bytes, KNEIL_SUMMARY_LEN, &s2) == 0);
	CHECK(s2.minutes_by_class[7] == 8 && s2.minutes_sitting == 9 && s2.n_sit_to_stand == 12 &&
	      s2.sit_tilt_x10 == -45);
	CHECK(kneil_summary_to_line(&s2, line, sizeof(line)) > 0);
	CHECK(strcmp(line, "D,2,8,1,2,3,4,5,6,7,8,9,10,11,12,13,2,25,100,1234,1100,-45\n") == 0);
	CHECK(kneil_summary_decode(sum_bytes, KNEIL_SUMMARY_LEN - 1, &s2) == -1);
	CHECK(kneil_summary_encode(&sum, buf, 40) == -1);

	/* ---- INFO: "selftest PASS" -> 03 73 65 ... -> "I,selftest PASS" */
	const uint8_t info_bytes[] = {0x03, 's', 'e', 'l', 'f', 't', 'e', 's', 't', ' ', 'P', 'A', 'S', 'S'};
	CHECK(kneil_info_encode("selftest PASS", buf, sizeof(buf)) == (int)sizeof(info_bytes));
	CHECK(memcmp(buf, info_bytes, sizeof(info_bytes)) == 0);
	CHECK(kneil_info_to_line(info_bytes, sizeof(info_bytes), line, sizeof(line)) > 0);
	CHECK(strcmp(line, "I,selftest PASS\n") == 0);
	CHECK(kneil_info_encode("a\nb", buf, sizeof(buf)) == 4 && buf[2] == '?');   /* no line end in the text */
	CHECK(kneil_info_encode("", buf, sizeof(buf)) == -1);
	char longtext[300];
	memset(longtext, 'x', sizeof(longtext) - 1);
	longtext[sizeof(longtext) - 1] = '\0';
	CHECK(kneil_info_encode(longtext, buf, sizeof(buf)) == 1 + KNEIL_INFO_TEXT_MAX);
	CHECK(kneil_info_to_line(buf, 1 + KNEIL_INFO_TEXT_MAX, line, sizeof(line)) == 2 + KNEIL_INFO_TEXT_MAX + 1);
	CHECK(kneil_info_to_line(info_bytes, 1, line, sizeof(line)) == -1);            /* no text */
	CHECK(kneil_info_to_line(live2_bytes, KNEIL_LIVE_V2_LEN, line, sizeof(line)) == -1); /* not INFO */

	/* Commands */
	CHECK(kneil_cmd_to_ctrl("S") == 0x10 && kneil_cmd_to_ctrl("P") == 0x11 && kneil_cmd_to_ctrl("T") == 0x12);
	CHECK(kneil_cmd_to_ctrl("Q") == 0x13 && kneil_cmd_to_ctrl("Z") == 0x14);
	CHECK(kneil_cmd_to_ctrl("X") == -1 && kneil_cmd_to_ctrl("SS") == -1 && kneil_cmd_to_ctrl("") == -1);

	printf("longest R line: %d characters\n", nraw);
	printf("PROTOCOL TEST: %s\n", fails ? "FAILED" : "PASS");
	return fails ? 1 : 0;
}
