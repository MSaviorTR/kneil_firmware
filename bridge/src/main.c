/*
 * KNEIL BLE bridge for the Seeed XIAO nRF52840 (board target xiao_ble).
 *
 * The bridge is a BLE central. It connects to a KNEIL-xxxx device, subscribes to LIVE, RAW and INFO (if the
 * device has it), and writes every packet to the USB serial port as the text lines of the KNEIL live
 * protocol (section B: L, R and I lines, made by protocol/kneil_proto.c, shared with the KNEIL firmware). Commands S, P, T
 * from the PC are written to CONTROL as 0x10, 0x11, 0x12. The bridge adds only its own B lines (section C):
 *   PC -> bridge:  B,SCAN   B,CONNECT,<address>   B,CONNECT,AUTO   B,DISCONNECT   S   P   T
 *   bridge -> PC:  B,VERSION,<text>   B,FOUND,<name>,<address>,<rssi_dBm>   B,SCAN_DONE
 *                  B,STATE,<idle|scanning|connecting|connected|lost>,<name>,<address>,<rssi_dBm>,<mtu>
 *                  B,DROPS,<count>
 *
 * Threads (a slow PC never blocks the BLE link):
 *  - BLE callbacks only copy packets and events into message queues.
 *  - writer thread: packets -> text lines -> USB transmit ring buffer. If a line does not fit, it is
 *    dropped and counted (B,DROPS); packets lost because the queue was full are counted too.
 *  - main thread: commands from the PC, connection state machine, B lines, LED.
 * LED: blue blinking = scanning or connecting, green = connected, red = link lost or error.
 *
 * Re-flashing: open the port at 1200 baud with DTR on, then close it (like Arduino); the bridge then
 * restarts in the UF2 bootloader and the XIAO drive appears (bridge/flash_bridge.ps1 does all of it).
 * Double-tapping reset does the same.
 */
#include <string.h>
#include <stdarg.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/ring_buffer.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/util.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/bluetooth/hci.h>
#include <bluetooth/gatt_dm.h>
#include <nrfx.h>

#include "kneil_proto.h"

#define BRIDGE_VERSION  "KNEIL-bridge 0.3.0 xiao_ble NCS-3.1.0"

#define NAME_LEN        31      /* longest device name kept */
#define USER_SCAN_MS    5000    /* B,SCAN duration */
#define AUTO_PICK_MS    3000    /* B,CONNECT,AUTO: listen this long, then take the strongest KNEIL-* */
#define STATE_PERIOD_MS 2000    /* B,STATE while connected */
#define LOST_HOLD_MS    1000    /* show "lost" this long before reconnecting */
#define RETRY_MS        1000    /* wait after a failed connection attempt */
#define ERROR_LED_MS    3000    /* red LED after an error (for example: not a KNEIL device) */
#define DROPS_PERIOD_MS 1000    /* B,DROPS at most once per second */
#define LOOP_MS         20      /* main loop period */
#define MAX_SEEN        16      /* devices remembered during B,SCAN */
#define UF2_MAGIC       0x57    /* GPREGRET value: Adafruit nRF52 bootloader starts the UF2 drive */

/* ------------------------------------------------------------------------------------------------ */
/* USB serial (CDC ACM)                                                                               */
/* ------------------------------------------------------------------------------------------------ */
static const struct device *const uart = DEVICE_DT_GET_ONE(zephyr_cdc_acm_uart);

RING_BUF_DECLARE(tx_ring, 8192);
RING_BUF_DECLARE(rx_ring, 256);
static struct k_spinlock tx_lock;
static struct k_spinlock rx_lock;
static atomic_t drops;            /* lines/packets lost inside the bridge */
static volatile bool host_open;   /* the PC has the port open (DTR set) */

/* UART callback (runs in the USB work queue): only moves bytes between the rings and the driver. */
static void uart_cb(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);

	while (uart_irq_update(dev) && uart_irq_is_pending(dev)) {
		if (uart_irq_rx_ready(dev)) {
			uint8_t buf[64];
			const int n = uart_fifo_read(dev, buf, sizeof(buf));

			if (n > 0) {
				k_spinlock_key_t key = k_spin_lock(&rx_lock);

				ring_buf_put(&rx_ring, buf, n);   /* if full, the bytes are lost */
				k_spin_unlock(&rx_lock, key);
			}
		}
		if (uart_irq_tx_ready(dev)) {
			uint8_t *p;
			k_spinlock_key_t key = k_spin_lock(&tx_lock);
			const uint32_t n = ring_buf_get_claim(&tx_ring, &p, 64);
			const int sent = (n > 0) ? uart_fifo_fill(dev, p, n) : 0;

			ring_buf_get_finish(&tx_ring, sent > 0 ? (uint32_t)sent : 0);
			k_spin_unlock(&tx_lock, key);
			if (sent <= 0) {
				/* nothing to send, or the driver buffer is full: the main loop enables it again */
				uart_irq_tx_disable(dev);
			}
		}
	}
}

/* Queue one complete line for the PC. Never blocks: if the line does not fit, it is dropped.
 * Nothing is sent while the port is closed (DTR off), so the PC never receives old lines when it opens it. */
static bool usb_send(const char *s, int len)
{
	if (len <= 0 || !host_open) {
		return false;
	}
	k_spinlock_key_t key = k_spin_lock(&tx_lock);
	const bool fits = ring_buf_space_get(&tx_ring) >= (uint32_t)len;

	if (fits) {
		ring_buf_put(&tx_ring, (const uint8_t *)s, (uint32_t)len);
	}
	k_spin_unlock(&tx_lock, key);

	if (fits) {
		uart_irq_tx_enable(uart);
	} else {
		atomic_inc(&drops);
	}
	return fits;
}

/* B lines (main thread only) */
static void usb_printf(const char *fmt, ...)
{
	char line[160];
	va_list ap;

	va_start(ap, fmt);
	int n = vsnprintk(line, sizeof(line), fmt, ap);
	va_end(ap);
	if (n >= (int)sizeof(line)) {
		n = (int)sizeof(line) - 1;
		line[n - 1] = '\n';
	}
	usb_send(line, n);
}

/* ------------------------------------------------------------------------------------------------ */
/* Queues filled by the BLE callbacks                                                                 */
/* ------------------------------------------------------------------------------------------------ */
struct pkt {                      /* one notification (LIVE or RAW) */
	uint16_t len;
	uint8_t data[246];        /* ATT MTU 247 -> at most 244 bytes */
};
K_MSGQ_DEFINE(pkt_q, sizeof(struct pkt), 32, 4);

struct found {                    /* one advertising report of interest */
	bt_addr_le_t addr;
	int8_t rssi;
	char name[NAME_LEN + 1];
};
K_MSGQ_DEFINE(found_q, sizeof(struct found), 16, 4);

enum link_evt_type {
	EV_CONNECTED, EV_CONN_FAILED, EV_DISCONNECTED, EV_MTU_DONE,
	EV_DISC_DONE, EV_DISC_FAILED, EV_SUBSCRIBED, EV_WRITE_DONE,
};
struct link_evt {
	uint8_t type;
	uint8_t err;
};
K_MSGQ_DEFINE(link_q, sizeof(struct link_evt), 16, 1);

static void post_link(uint8_t type, uint8_t err)
{
	const struct link_evt e = {type, err};

	(void)k_msgq_put(&link_q, &e, K_NO_WAIT);
}

/* ------------------------------------------------------------------------------------------------ */
/* INFO lines of the current link, sent again when the PC opens the port: the device sends its summary */
/* when the bridge subscribes, which is often before the PC app is running.                           */
/* ------------------------------------------------------------------------------------------------ */
#define INFO_CACHE_N 16
static char info_cache[INFO_CACHE_N][KNEIL_INFO_TEXT_MAX + 4];   /* "I,<text>\n" */
static uint8_t info_cache_len[INFO_CACHE_N];
static int info_cache_head, info_cache_count;
static struct k_spinlock info_lock;

static void info_cache_add(const char *line, int len)       /* writer thread */
{
	if (len <= 0 || len > (int)sizeof(info_cache[0])) {
		return;
	}
	k_spinlock_key_t key = k_spin_lock(&info_lock);
	const int slot = (info_cache_head + info_cache_count) % INFO_CACHE_N;

	memcpy(info_cache[slot], line, len);
	info_cache_len[slot] = (uint8_t)len;
	if (info_cache_count < INFO_CACHE_N) {
		info_cache_count++;
	} else {
		info_cache_head = (info_cache_head + 1) % INFO_CACHE_N;   /* full: forget the oldest */
	}
	k_spin_unlock(&info_lock, key);
}

static void info_cache_clear(void)                           /* main thread: new link */
{
	k_spinlock_key_t key = k_spin_lock(&info_lock);

	info_cache_head = 0;
	info_cache_count = 0;
	k_spin_unlock(&info_lock, key);
}

static void info_cache_replay(void)                          /* main thread: the PC opened the port */
{
	char line[KNEIL_INFO_TEXT_MAX + 4];

	for (int i = 0;; i++) {
		int len = 0;
		k_spinlock_key_t key = k_spin_lock(&info_lock);

		if (i < info_cache_count) {
			const int slot = (info_cache_head + i) % INFO_CACHE_N;

			len = info_cache_len[slot];
			memcpy(line, info_cache[slot], len);
		}
		k_spin_unlock(&info_lock, key);
		if (len == 0) {
			break;
		}
		usb_send(line, len);
	}
}

/* ------------------------------------------------------------------------------------------------ */
/* Writer thread: packets -> L/R/I lines                                                              */
/* ------------------------------------------------------------------------------------------------ */
static void writer_thread(void *a, void *b, void *c)
{
	static struct pkt pk;
	static kneil_live_t live;
	static kneil_raw_t raw;
	static char line[KNEIL_LINE_MAX];

	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);
	for (;;) {
		k_msgq_get(&pkt_q, &pk, K_FOREVER);
		int n = -1;

		if (kneil_live_decode(pk.data, pk.len, &live) == 0) {
			n = kneil_live_to_line(&live, line, sizeof(line));
		} else if (kneil_raw_decode(pk.data, pk.len, &raw) == 0) {
			n = kneil_raw_to_line(&raw, line, sizeof(line));
		} else {
			n = kneil_info_to_line(pk.data, pk.len, line, sizeof(line));
			if (n > 0) {
				info_cache_add(line, n);
			}
		}
		/* packets that are not valid LIVE/RAW/INFO packets are ignored */
		if (n > 0) {
			usb_send(line, n);
		}
	}
}
K_THREAD_DEFINE(writer_tid, 2048, writer_thread, NULL, NULL, NULL, 7, 0, 0);

/* ------------------------------------------------------------------------------------------------ */
/* BLE: UUIDs, scanning, connection, discovery, subscriptions, CONTROL writes                        */
/* ------------------------------------------------------------------------------------------------ */
static struct bt_uuid_128 uuid_svc = BT_UUID_INIT_128(KNEIL_UUID_ENCODE(KNEIL_UUID_SERVICE));
static struct bt_uuid_128 uuid_live = BT_UUID_INIT_128(KNEIL_UUID_ENCODE(KNEIL_UUID_LIVE));
static struct bt_uuid_128 uuid_raw = BT_UUID_INIT_128(KNEIL_UUID_ENCODE(KNEIL_UUID_RAW));
static struct bt_uuid_128 uuid_ctrl = BT_UUID_INIT_128(KNEIL_UUID_ENCODE(KNEIL_UUID_CONTROL));
static struct bt_uuid_128 uuid_info = BT_UUID_INIT_128(KNEIL_UUID_ENCODE(KNEIL_UUID_INFO));

/* address we are looking for (also reported when the device has no KNEIL name) */
static volatile bool scan_target_set;
static bt_addr_t scan_target;

static bool is_kneil_name(const char *name)
{
	return strncmp(name, KNEIL_NAME_PREFIX, sizeof(KNEIL_NAME_PREFIX) - 1) == 0;
}

static bool ad_name(struct bt_data *data, void *user_data)
{
	char *name = user_data;

	if (data->type == BT_DATA_NAME_COMPLETE || data->type == BT_DATA_NAME_SHORTENED) {
		const size_t n = MIN(data->data_len, NAME_LEN);

		for (size_t i = 0; i < n; i++) {
			const char ch = (char)data->data[i];
			/* keep the CSV lines valid: no commas or control characters in names */
			name[i] = (ch == ',' || ch < 0x20 || ch > 0x7e) ? '_' : ch;
		}
		name[n] = '\0';
		return false;
	}
	return true;
}

static void scan_cb(const bt_addr_le_t *addr, int8_t rssi, uint8_t adv_type, struct net_buf_simple *ad)
{
	struct found f = {.rssi = rssi};

	ARG_UNUSED(adv_type);
	bt_data_parse(ad, ad_name, f.name);
	const bool target = scan_target_set &&
			    memcmp(addr->a.val, scan_target.val, sizeof(scan_target.val)) == 0;

	if (!is_kneil_name(f.name) && !target) {
		return;
	}
	bt_addr_le_copy(&f.addr, addr);
	(void)k_msgq_put(&found_q, &f, K_NO_WAIT);   /* full: this report is skipped */
}

static void on_connected(struct bt_conn *c, uint8_t err)
{
	ARG_UNUSED(c);
	post_link(err ? EV_CONN_FAILED : EV_CONNECTED, err);
}

static void on_disconnected(struct bt_conn *c, uint8_t reason)
{
	ARG_UNUSED(c);
	post_link(EV_DISCONNECTED, reason);
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected = on_connected,
	.disconnected = on_disconnected,
};

static struct bt_gatt_exchange_params mtu_params;

static void mtu_cb(struct bt_conn *c, uint8_t err, struct bt_gatt_exchange_params *p)
{
	ARG_UNUSED(c);
	ARG_UNUSED(p);
	post_link(EV_MTU_DONE, err);
}

static uint16_t h_live, h_live_ccc, h_raw, h_raw_ccc, h_ctrl;
static uint16_t h_info, h_info_ccc;   /* optional: 0 if the device has no INFO characteristic */

/* value handle of a characteristic (and its CCC descriptor), 0 if missing */
static uint16_t dm_handle(struct bt_gatt_dm *dm, const struct bt_uuid *uuid, uint16_t *ccc)
{
	const struct bt_gatt_dm_attr *chrc = bt_gatt_dm_char_by_uuid(dm, uuid);

	if (chrc == NULL) {
		return 0;
	}
	const struct bt_gatt_dm_attr *val = bt_gatt_dm_desc_by_uuid(dm, chrc, uuid);

	if (ccc != NULL) {
		const struct bt_gatt_dm_attr *d = bt_gatt_dm_desc_by_uuid(dm, chrc, BT_UUID_GATT_CCC);
		*ccc = (d != NULL) ? d->handle : 0;
	}
	return (val != NULL) ? val->handle : 0;
}

static void dm_completed(struct bt_gatt_dm *dm, void *ctx)
{
	ARG_UNUSED(ctx);
	h_live = dm_handle(dm, &uuid_live.uuid, &h_live_ccc);
	h_raw = dm_handle(dm, &uuid_raw.uuid, &h_raw_ccc);
	h_ctrl = dm_handle(dm, &uuid_ctrl.uuid, NULL);
	h_info = dm_handle(dm, &uuid_info.uuid, &h_info_ccc);
	bt_gatt_dm_data_release(dm);
	const bool ok = h_live && h_live_ccc && h_raw && h_raw_ccc && h_ctrl;

	post_link(ok ? EV_DISC_DONE : EV_DISC_FAILED, 0);
}

static void dm_not_found(struct bt_conn *c, void *ctx)
{
	ARG_UNUSED(c);
	ARG_UNUSED(ctx);
	post_link(EV_DISC_FAILED, 0);
}

static void dm_error(struct bt_conn *c, int err, void *ctx)
{
	ARG_UNUSED(c);
	ARG_UNUSED(ctx);
	post_link(EV_DISC_FAILED, (uint8_t)(-err));
}

static const struct bt_gatt_dm_cb dm_cb = {
	.completed = dm_completed,
	.service_not_found = dm_not_found,
	.error_found = dm_error,
};

/* Notifications: copy the packet into the queue, nothing else */
static uint8_t on_notify(struct bt_conn *c, struct bt_gatt_subscribe_params *p, const void *data, uint16_t len)
{
	ARG_UNUSED(c);
	if (data == NULL) {          /* unsubscribed (for example after a disconnection) */
		p->value_handle = 0;
		return BT_GATT_ITER_STOP;
	}
	struct pkt pk;

	pk.len = MIN(len, sizeof(pk.data));
	memcpy(pk.data, data, pk.len);
	if (k_msgq_put(&pkt_q, &pk, K_NO_WAIT) != 0) {
		atomic_inc(&drops);
	}
	return BT_GATT_ITER_CONTINUE;
}

static void on_subscribed(struct bt_conn *c, uint8_t err, struct bt_gatt_subscribe_params *p)
{
	ARG_UNUSED(c);
	ARG_UNUSED(p);
	post_link(EV_SUBSCRIBED, err);
}

static struct bt_gatt_subscribe_params sub_live, sub_raw, sub_info;
static struct bt_gatt_write_params wr_params;
static uint8_t wr_byte;

static void on_write(struct bt_conn *c, uint8_t err, struct bt_gatt_write_params *p)
{
	ARG_UNUSED(c);
	ARG_UNUSED(p);
	post_link(EV_WRITE_DONE, err);
}

/* ------------------------------------------------------------------------------------------------ */
/* State machine (main thread only)                                                                   */
/* ------------------------------------------------------------------------------------------------ */
enum state { ST_IDLE, ST_SCANNING, ST_CONNECTING, ST_CONNECTED, ST_LOST };
static const char *const state_name[] = {"idle", "scanning", "connecting", "connected", "lost"};

static enum state state = ST_IDLE;
static struct bt_conn *conn;      /* our reference while connecting or connected */
static bool want_link;            /* B,CONNECT given: keep the link up, reconnect after a loss */
static bool switching;            /* B,CONNECT to another device while connected */
static bool auto_pick;            /* B,CONNECT,AUTO: still choosing the strongest KNEIL-* */
static int64_t auto_deadline;
static struct found best;
static bool best_valid;
static bt_addr_le_t target;       /* the device we connect to */
static bool have_target;
static char target_name[NAME_LEN + 1];
static int8_t link_rssi;
static uint16_t link_mtu;
static int subs_done, subs_needed;
static bool scanning;
static int64_t retry_at, lost_until, error_until, next_state_at, next_drops_at;
static atomic_val_t drops_reported;
static bool bt_ok;

static bool user_scan;            /* B,SCAN running */
static int64_t user_scan_end;
static bt_addr_t seen[MAX_SEEN];
static int n_seen;

static uint8_t ctrl_q[8];         /* CONTROL bytes waiting to be written */
static int ctrl_head, ctrl_count;
static bool wr_busy;

static const struct gpio_dt_spec led_red = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static const struct gpio_dt_spec led_green = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);
static const struct gpio_dt_spec led_blue = GPIO_DT_SPEC_GET(DT_ALIAS(led2), gpios);

static void emit_state(void)
{
	char a[BT_ADDR_STR_LEN] = "";
	const bool show = have_target && (want_link || conn != NULL);

	if (show) {
		bt_addr_to_str(&target.a, a, sizeof(a));
	}
	usb_printf("B,STATE,%s,%s,%s,%d,%u\n", state_name[state], show ? target_name : "", a,
		   show ? (int)link_rssi : 0, (state == ST_CONNECTED) ? (unsigned int)link_mtu : 0U);
}

static void set_state(enum state s)
{
	if (s != state) {
		state = s;
		emit_state();
	}
}

static void scanner_update(int64_t now)
{
	const bool need = bt_ok && (user_scan ||
		(want_link && state == ST_CONNECTING && conn == NULL && now >= retry_at));

	if (need == scanning) {
		return;
	}
	if (need) {
		/* active scan (names may be in the scan response), no duplicate filter (RSSI updates) */
		const struct bt_le_scan_param p = {
			.type = BT_LE_SCAN_TYPE_ACTIVE,
			.options = BT_LE_SCAN_OPT_NONE,
			.interval = BT_GAP_SCAN_FAST_INTERVAL,
			.window = BT_GAP_SCAN_FAST_WINDOW,
		};
		const int err = bt_le_scan_start(&p, scan_cb);

		if (err == 0 || err == -EALREADY) {
			scanning = true;
		} else {
			retry_at = now + RETRY_MS;
		}
	} else {
		(void)bt_le_scan_stop();
		scanning = false;
	}
}

static void connect_now(int64_t now)
{
	if (scanning) {
		(void)bt_le_scan_stop();
		scanning = false;
	}
	/* connection interval 30-50 ms, no latency, 4 s supervision timeout */
	const int err = bt_conn_le_create(&target, BT_CONN_LE_CREATE_CONN,
					  BT_LE_CONN_PARAM(24, 40, 0, 400), &conn);
	if (err) {
		conn = NULL;
		retry_at = now + RETRY_MS;
	}
}

static void subscribe(struct bt_gatt_subscribe_params *p, uint16_t value_handle, uint16_t ccc_handle)
{
	p->notify = on_notify;
	p->subscribe = on_subscribed;
	p->value = BT_GATT_CCC_NOTIFY;
	p->value_handle = value_handle;
	p->ccc_handle = ccc_handle;
	if (bt_gatt_subscribe(conn, p) != 0) {
		bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
	}
}

static void ctrl_kick(void)
{
	if (wr_busy || ctrl_count == 0 || state != ST_CONNECTED || conn == NULL) {
		return;
	}
	wr_byte = ctrl_q[ctrl_head];
	ctrl_head = (ctrl_head + 1) % (int)sizeof(ctrl_q);
	ctrl_count--;
	wr_params.func = on_write;
	wr_params.handle = h_ctrl;
	wr_params.offset = 0;
	wr_params.data = &wr_byte;
	wr_params.length = 1;
	if (bt_gatt_write(conn, &wr_params) == 0) {
		wr_busy = true;
	}
}

/* RSSI of the current link (standard HCI Read RSSI command) */
static void read_link_rssi(void)
{
	uint16_t handle;

	if (conn == NULL || bt_hci_get_conn_handle(conn, &handle) != 0) {
		return;
	}
	struct net_buf *buf = bt_hci_cmd_alloc(K_MSEC(100));

	if (buf == NULL) {
		return;
	}
	struct bt_hci_cp_read_rssi *cp = net_buf_add(buf, sizeof(*cp));

	cp->handle = sys_cpu_to_le16(handle);
	struct net_buf *rsp = NULL;

	if (bt_hci_cmd_send_sync(BT_HCI_OP_READ_RSSI, buf, &rsp) == 0 && rsp != NULL) {
		const struct bt_hci_rp_read_rssi *rp = (const void *)rsp->data;

		if (rp->status == 0) {
			link_rssi = rp->rssi;
		}
	}
	if (rsp != NULL) {
		net_buf_unref(rsp);
	}
}

static void handle_link(const struct link_evt *e, int64_t now)
{
	switch (e->type) {
	case EV_CONNECTED:
		if (!want_link) {     /* B,DISCONNECT arrived while connecting */
			bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
			break;
		}
		info_cache_clear();   /* the device sends its summary again on this link */
		mtu_params.func = mtu_cb;
		if (bt_gatt_exchange_mtu(conn, &mtu_params) != 0) {
			post_link(EV_MTU_DONE, 0);   /* already exchanged: go on with discovery */
		}
		break;

	case EV_CONN_FAILED:      /* timeout or cancelled: our reference is still held */
		if (conn != NULL) {
			bt_conn_unref(conn);
			conn = NULL;
		}
		retry_at = now + RETRY_MS;
		if (switching) {      /* attempt cancelled by B,CONNECT to another device */
			switching = false;
			retry_at = now;
			emit_state();
		}
		if (!want_link) {
			set_state(ST_IDLE);
		}
		break;

	case EV_MTU_DONE:
		if (conn == NULL) {
			break;
		}
		link_mtu = bt_gatt_get_mtu(conn);
		if (bt_gatt_dm_start(conn, &uuid_svc.uuid, &dm_cb, NULL) != 0) {
			bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
		}
		break;

	case EV_DISC_DONE:
		subs_done = 0;
		subs_needed = 2;
		subscribe(&sub_live, h_live, h_live_ccc);
		subscribe(&sub_raw, h_raw, h_raw_ccc);
		if (h_info != 0 && h_info_ccc != 0) {
			subs_needed = 3;
			subscribe(&sub_info, h_info, h_info_ccc);
		}
		break;

	case EV_DISC_FAILED:      /* no KNEIL service: wrong device, give up */
		want_link = false;
		error_until = now + ERROR_LED_MS;
		if (conn != NULL) {
			bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
		}
		break;

	case EV_SUBSCRIBED:
		if (e->err != 0) {
			if (conn != NULL) {
				bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
			}
		} else if (++subs_done == subs_needed) {
			link_mtu = bt_gatt_get_mtu(conn);
			set_state(ST_CONNECTED);
			next_state_at = now + STATE_PERIOD_MS;
			ctrl_kick();
		}
		break;

	case EV_WRITE_DONE:
		wr_busy = false;
		ctrl_kick();
		break;

	case EV_DISCONNECTED:
		if (conn != NULL) {
			bt_conn_unref(conn);
			conn = NULL;
		}
		link_mtu = 0;
		subs_done = 0;
		wr_busy = false;
		ctrl_count = 0;
		if (want_link && switching) {          /* B,CONNECT to another device */
			switching = false;
			retry_at = now;
			set_state(ST_CONNECTING);
		} else if (want_link) {                /* link lost: reconnect by itself */
			lost_until = now + LOST_HOLD_MS;
			set_state(ST_LOST);
		} else {
			set_state(ST_IDLE);
		}
		break;

	default:
		break;
	}
}

static void handle_found(const struct found *f, int64_t now)
{
	/* B,SCAN: each KNEIL device once */
	if (user_scan && is_kneil_name(f->name)) {
		bool known = false;

		for (int i = 0; i < n_seen; i++) {
			known |= memcmp(seen[i].val, f->addr.a.val, sizeof(seen[i].val)) == 0;
		}
		if (!known && n_seen < MAX_SEEN) {
			char a[BT_ADDR_STR_LEN];

			seen[n_seen++] = f->addr.a;
			bt_addr_to_str(&f->addr.a, a, sizeof(a));
			usb_printf("B,FOUND,%s,%s,%d\n", f->name, a, f->rssi);
		}
	}

	if (!(want_link && state == ST_CONNECTING && conn == NULL)) {
		return;
	}
	if (auto_pick) {
		if (is_kneil_name(f->name) && (!best_valid || f->rssi > best.rssi)) {
			best = *f;
			best_valid = true;
		}
		return;               /* the choice is made in periodic() after AUTO_PICK_MS */
	}
	if (have_target && memcmp(f->addr.a.val, target.a.val, sizeof(target.a.val)) == 0 && now >= retry_at) {
		bt_addr_le_copy(&target, &f->addr);   /* now with the right address type */
		if (f->name[0] != '\0') {
			strcpy(target_name, f->name);
		}
		link_rssi = f->rssi;
		connect_now(now);
	}
}

static void handle_command(const char *line, int64_t now)
{
	const int ctrl = kneil_cmd_to_ctrl(line);

	if (ctrl >= 0) {                                   /* S, P, T -> CONTROL */
		if (state == ST_CONNECTED && ctrl_count < (int)sizeof(ctrl_q)) {
			ctrl_q[(ctrl_head + ctrl_count) % (int)sizeof(ctrl_q)] = (uint8_t)ctrl;
			ctrl_count++;
			ctrl_kick();
		}
		return;
	}
	if (strcmp(line, "B,SCAN") == 0) {
		user_scan = true;
		user_scan_end = now + USER_SCAN_MS;
		n_seen = 0;
		if (state == ST_IDLE) {
			set_state(ST_SCANNING);
		}
		return;
	}
	if (strncmp(line, "B,CONNECT,", 10) == 0) {
		const char *arg = line + 10;

		if (strcmp(arg, "AUTO") == 0) {
			auto_pick = true;
			auto_deadline = now + AUTO_PICK_MS;
			best_valid = false;
			have_target = false;
			target_name[0] = '\0';
			scan_target_set = false;
		} else {
			bt_addr_t a;

			if (bt_addr_from_str(arg, &a) != 0) {
				return;                            /* not an address: ignored */
			}
			if (conn != NULL && memcmp(a.val, target.a.val, sizeof(a.val)) == 0) {
				emit_state();                      /* already connected/connecting to it */
				return;
			}
			auto_pick = false;
			target.type = BT_ADDR_LE_RANDOM;           /* corrected when the device is seen */
			bt_addr_copy(&target.a, &a);
			have_target = true;
			target_name[0] = '\0';
			scan_target = a;
			scan_target_set = true;
		}
		want_link = true;
		retry_at = now;
		if (conn != NULL) {                        /* connected to another device: switch */
			switching = true;
			bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
		} else {
			if (state == ST_CONNECTING) {
				emit_state();
			}
			set_state(ST_CONNECTING);
		}
		return;
	}
	if (strcmp(line, "B,DISCONNECT") == 0) {
		want_link = false;
		switching = false;
		auto_pick = false;
		scan_target_set = false;
		if (conn != NULL) {
			bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
		} else {
			set_state(user_scan ? ST_SCANNING : ST_IDLE);
		}
		return;
	}
	/* anything else is ignored */
}

static void handle_rx(int64_t now)
{
	static char line[64];
	static int len;
	static bool overflow;
	uint8_t buf[64];
	uint32_t n;

	do {
		k_spinlock_key_t key = k_spin_lock(&rx_lock);

		n = ring_buf_get(&rx_ring, buf, sizeof(buf));
		k_spin_unlock(&rx_lock, key);
		for (uint32_t i = 0; i < n; i++) {
			const char ch = (char)buf[i];

			if (ch == '\r') {
				continue;
			}
			if (ch == '\n') {
				line[len] = '\0';
				if (!overflow) {
					handle_command(line, now);
				}
				len = 0;
				overflow = false;
			} else if (len < (int)sizeof(line) - 1) {
				line[len++] = ch;
			} else {
				overflow = true;                    /* too long: not a command */
			}
		}
	} while (n > 0);
}

static void enter_uf2_bootloader(void)
{
	NRF_POWER->GPREGRET = UF2_MAGIC;
	NVIC_SystemReset();
}

static void check_host(void)
{
	uint32_t dtr = 0, baud = 0;

	(void)uart_line_ctrl_get(uart, UART_LINE_CTRL_DTR, &dtr);
	(void)uart_line_ctrl_get(uart, UART_LINE_CTRL_BAUD_RATE, &baud);
	if (dtr && !host_open) {                           /* the PC opened the port */
		k_spinlock_key_t key = k_spin_lock(&tx_lock);

		ring_buf_reset(&tx_ring);                  /* forget lines nobody was reading */
		k_spin_unlock(&tx_lock, key);
		host_open = true;
		/* a line cut off when the port was last closed may still be in the USB driver: end it */
		usb_send("\n", 1);
		usb_printf("B,VERSION,%s\n", BRIDGE_VERSION);
		emit_state();
		info_cache_replay();                       /* device information the PC has not seen */
	} else if (!dtr && host_open) {                    /* the PC closed the port */
		host_open = false;
		if (baud == 1200) {
			enter_uf2_bootloader();            /* "1200 baud touch": opened at 1200 baud, then closed */
		}
	}
}

static void led_update(int64_t now)
{
	const bool blink = ((now / 250) & 1) != 0;
	bool r = false, g = false, b = false;

	if (!bt_ok || now < error_until || state == ST_LOST) {
		r = true;
	} else if (user_scan || state == ST_SCANNING || state == ST_CONNECTING) {
		b = blink;
	} else if (state == ST_CONNECTED) {
		g = true;
	}
	gpio_pin_set_dt(&led_red, r);
	gpio_pin_set_dt(&led_green, g);
	gpio_pin_set_dt(&led_blue, b);
}

static void periodic(int64_t now)
{
	if (user_scan && now >= user_scan_end) {
		user_scan = false;
		usb_printf("B,SCAN_DONE\n");
		if (state == ST_SCANNING) {
			set_state(ST_IDLE);
		}
	}
	if (state == ST_LOST && now >= lost_until) {
		retry_at = now;
		set_state(ST_CONNECTING);
	}
	if (auto_pick && want_link && state == ST_CONNECTING && conn == NULL && now >= auto_deadline && best_valid) {
		auto_pick = false;                             /* later reconnects keep this device */
		bt_addr_le_copy(&target, &best.addr);
		have_target = true;
		strcpy(target_name, best.name);
		link_rssi = best.rssi;
		scan_target = target.a;
		scan_target_set = true;
		emit_state();
		connect_now(now);
	}
	scanner_update(now);

	if (state == ST_CONNECTED && now >= next_state_at) {
		read_link_rssi();
		emit_state();
		next_state_at = now + STATE_PERIOD_MS;
	}

	const atomic_val_t d = atomic_get(&drops);

	if (d != drops_reported && now >= next_drops_at) {
		usb_printf("B,DROPS,%ld\n", (long)d);
		drops_reported = d;
		next_drops_at = now + DROPS_PERIOD_MS;
	}

	check_host();
	led_update(now);
	if (!ring_buf_is_empty(&tx_ring)) {
		uart_irq_tx_enable(uart);                  /* restart a stalled transmission */
	}
}

int main(void)
{
	gpio_pin_configure_dt(&led_red, GPIO_OUTPUT_INACTIVE);
	gpio_pin_configure_dt(&led_green, GPIO_OUTPUT_INACTIVE);
	gpio_pin_configure_dt(&led_blue, GPIO_OUTPUT_INACTIVE);

	if (!device_is_ready(uart)) {
		gpio_pin_set_dt(&led_red, 1);
		return 0;
	}
	uart_irq_callback_set(uart, uart_cb);
	uart_irq_rx_enable(uart);

	bt_ok = (bt_enable(NULL) == 0);
	/* B,VERSION and B,STATE are sent when the PC opens the port (check_host) */

	for (;;) {
		struct link_evt le;
		struct found f;

		if (k_msgq_get(&link_q, &le, K_MSEC(LOOP_MS)) == 0) {
			handle_link(&le, k_uptime_get());
		}
		while (k_msgq_get(&found_q, &f, K_NO_WAIT) == 0) {
			handle_found(&f, k_uptime_get());
		}
		handle_rx(k_uptime_get());
		periodic(k_uptime_get());
	}
	return 0;
}
