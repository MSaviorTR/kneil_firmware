/*
 * KNEIL live protocol, version 1: binary BLE packets (section A) and USB text lines (section B).
 * Shared by the KNEIL firmware (src/kneil_live.c) and the XIAO bridge (bridge/), so both sides
 * produce exactly the same lines. The PC app parses these lines: do not change one side alone.
 *
 * All multi-byte values are little-endian. Acceleration in mg (int16), device frame
 * x anterior, y subject's left, z superior. t_ms = milliseconds since boot.
 * Plain C99, no Zephyr dependency (except KNEIL_UUID_ENCODE, which needs <zephyr/bluetooth/uuid.h>).
 */
#ifndef KNEIL_PROTO_H
#define KNEIL_PROTO_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define KNEIL_PROTO_VERSION 1

/* BLE: 128-bit UUIDs a8f5000X-3e2c-4c6e-9b1a-6b4e45494c00 */
#define KNEIL_UUID_SERVICE 0x0001   /* KNEIL live service */
#define KNEIL_UUID_LIVE    0x0002   /* notify */
#define KNEIL_UUID_RAW     0x0003   /* notify */
#define KNEIL_UUID_CONTROL 0x0004   /* write */
#define KNEIL_UUID_INFO    0x0005   /* notify: text information (I lines); optional, added 16 Nov 2026 */
#define KNEIL_UUID_ENCODE(id) BT_UUID_128_ENCODE(0xa8f50000U | (id), 0x3e2c, 0x4c6e, 0x9b1a, 0x6b4e45494c00ULL)

#define KNEIL_NAME_PREFIX "KNEIL-"  /* advertised name KNEIL-xxxx (last 4 hex digits of the address) */

/* Packet types and sizes */
#define KNEIL_PKT_LIVE     0x01
#define KNEIL_PKT_RAW      0x02
#define KNEIL_PKT_INFO     0x03
/* INFO packet: uint8 type = 0x03, then 1..KNEIL_INFO_TEXT_MAX characters of printable ASCII text
 * (no line end; needs ATT MTU >= 104 for the full length). The bridge writes it as "I,<text>". */
#define KNEIL_INFO_TEXT_MAX 100
#define KNEIL_LIVE_LEN     20
#define KNEIL_RAW_HDR_LEN  8
#define KNEIL_RAW_N_NORMAL 25       /* 0.5 s at 50 Hz: 158-byte packet (needs ATT MTU >= 161) */
#define KNEIL_RAW_N_MAX    39       /* floor((247 - 3 - 8) / 6) */
#define KNEIL_RAW_N_FOR_MTU(mtu) (((mtu) - 3 - KNEIL_RAW_HDR_LEN) / 6)

/* CONTROL bytes (BLE) and command letters (USB) */
#define KNEIL_CTRL_RAW_START 0x10   /* 'S' */
#define KNEIL_CTRL_RAW_STOP  0x11   /* 'P' */
#define KNEIL_CTRL_SELFTEST  0x12   /* 'T' */

#define KNEIL_CLASS_NONE 255        /* no result yet */
#define KNEIL_VOTE_NONE  255        /* vote share not available */

/* LIVE flags */
#define KNEIL_FLAG_SELFTEST_OK 0x01
#define KNEIL_FLAG_LOWPASS     0x02
#define KNEIL_FLAG_SMALL_MODEL 0x04

/* Longest text line: R with KNEIL_RAW_N_MAX samples (about 850 characters) */
#define KNEIL_LINE_MAX 1024

typedef struct {
	uint16_t seq;          /* wraps */
	uint32_t t_ms;         /* end of the classified window */
	uint8_t cls;           /* raw class 0-8 or KNEIL_CLASS_NONE */
	uint8_t cls_smooth;    /* after the majority-of-3 smoothing */
	uint8_t vote_pct;      /* vote share of the winning class (%) or KNEIL_VOTE_NONE */
	uint8_t flags;         /* KNEIL_FLAG_* */
	int16_t mean_mg[3];    /* mean x, y, z of the window */
	uint16_t battery_mv;   /* 0 = not measured */
} kneil_live_t;

typedef struct {
	uint16_t seq;          /* wraps */
	uint32_t t_ms_first;   /* time of the first sample */
	uint8_t n;             /* samples in this packet (1..KNEIL_RAW_N_MAX) */
	int16_t xyz_mg[KNEIL_RAW_N_MAX][3];
} kneil_raw_t;

/* Binary packets (BLE). encode: returns the packet length, or -1 if buf is too small (or n is invalid).
 * decode: returns 0, or -1 if the packet is not a valid packet of that type. */
int kneil_live_encode(const kneil_live_t *p, uint8_t *buf, size_t size);
int kneil_live_decode(const uint8_t *buf, size_t len, kneil_live_t *p);
int kneil_raw_encode(const kneil_raw_t *p, uint8_t *buf, size_t size);
int kneil_raw_decode(const uint8_t *buf, size_t len, kneil_raw_t *p);

/* Text lines (USB): "L,..." and "R,..." ending with '\n' (and a terminating NUL).
 * Return the line length without the NUL, or -1 if out is too small. */
int kneil_live_to_line(const kneil_live_t *p, char *out, size_t size);
int kneil_raw_to_line(const kneil_raw_t *p, char *out, size_t size);

/* INFO: encode text (characters outside 0x20-0x7E become '?', text longer than KNEIL_INFO_TEXT_MAX or
 * than the buffer is cut). Returns the packet length (>= 2), or -1 if the text is empty or buf too small. */
int kneil_info_encode(const char *text, uint8_t *buf, size_t size);
/* INFO packet -> "I,<text>\n". Returns the line length, or -1 if it is not a valid INFO packet or out is too small. */
int kneil_info_to_line(const uint8_t *buf, size_t len, char *out, size_t size);

/* Command line from the PC ("S", "P", "T") -> CONTROL byte, or -1 if it is not one of them */
int kneil_cmd_to_ctrl(const char *line);

#ifdef __cplusplus
}
#endif
#endif /* KNEIL_PROTO_H */
