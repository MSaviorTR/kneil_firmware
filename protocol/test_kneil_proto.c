/* Host unit test for kneil_proto.c (gcc on the PC):
 *   gcc -std=c99 -Wall -Wextra -o test_kneil_proto protocol/test_kneil_proto.c protocol/kneil_proto.c
 * The example packets and lines below can also be used by the PC app's tests. */
#include <stdio.h>
#include <string.h>
#include "kneil_proto.h"

static int fails;

#define CHECK(cond) do { if (!(cond)) { printf("FAIL line %d: %s\n", __LINE__, #cond); fails++; } } while (0)

int main(void)
{
	uint8_t buf[256];
	char line[KNEIL_LINE_MAX];

	/* LIVE: seq 513, t 123456 ms, walking (0) raw, standing (4) smoothed, 85 %, self-test ok + low-pass */
	const kneil_live_t live = {513, 123456, 0, 4, 85, 0x03, {-12, 34, 987}, 0};
	const uint8_t live_bytes[KNEIL_LIVE_LEN] = {0x01, 0x01, 0x01, 0x02, 0x40, 0xE2, 0x01, 0x00, 0x00, 0x04,
						    0x55, 0x03, 0xF4, 0xFF, 0x22, 0x00, 0xDB, 0x03, 0x00, 0x00};
	CHECK(kneil_live_encode(&live, buf, sizeof(buf)) == KNEIL_LIVE_LEN);
	CHECK(memcmp(buf, live_bytes, KNEIL_LIVE_LEN) == 0);
	kneil_live_t l2;
	CHECK(kneil_live_decode(live_bytes, KNEIL_LIVE_LEN, &l2) == 0);
	CHECK(kneil_live_to_line(&l2, line, sizeof(line)) > 0);
	CHECK(strcmp(line, "L,513,123456,0,4,85,3,-12,34,987,0\n") == 0);
	CHECK(kneil_live_decode(live_bytes, 19, &l2) == -1);            /* wrong length */
	uint8_t bad[KNEIL_LIVE_LEN];
	memcpy(bad, live_bytes, sizeof(bad));
	bad[1] = 2;
	CHECK(kneil_live_decode(bad, sizeof(bad), &l2) == -1);          /* other protocol version */

	/* LIVE with no result yet */
	const kneil_live_t none = {65535, 4294967295u, 255, 255, 255, 0, {-32768, 32767, 0}, 3700};
	CHECK(kneil_live_encode(&none, buf, sizeof(buf)) == KNEIL_LIVE_LEN);
	CHECK(kneil_live_decode(buf, KNEIL_LIVE_LEN, &l2) == 0);
	CHECK(kneil_live_to_line(&l2, line, sizeof(line)) > 0);
	CHECK(strcmp(line, "L,65535,4294967295,255,255,255,0,-32768,32767,0,3700\n") == 0);

	/* RAW: seq 7, first sample at 1000 ms, 2 samples */
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
	const int n = kneil_raw_to_line(&r2, line, sizeof(line));
	CHECK(n > 0 && n < KNEIL_LINE_MAX);
	CHECK(kneil_raw_to_line(&r2, line, 100) == -1);                  /* output too small */
	CHECK(KNEIL_RAW_N_FOR_MTU(247) == 39 && KNEIL_RAW_N_FOR_MTU(23) == 2);

	/* INFO: "selftest PASS" -> 03 73 65 ... -> "I,selftest PASS" */
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
	CHECK(kneil_info_to_line(live_bytes, KNEIL_LIVE_LEN, line, sizeof(line)) == -1); /* not INFO */

	/* Commands */
	CHECK(kneil_cmd_to_ctrl("S") == 0x10 && kneil_cmd_to_ctrl("P") == 0x11 && kneil_cmd_to_ctrl("T") == 0x12);
	CHECK(kneil_cmd_to_ctrl("X") == -1 && kneil_cmd_to_ctrl("SS") == -1 && kneil_cmd_to_ctrl("") == -1);

	printf("longest R line: %d characters\n", n);
	printf("PROTOCOL TEST: %s\n", fails ? "FAILED" : "PASS");
	return fails ? 1 : 0;
}
