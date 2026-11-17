/* BMA580 start-up and FIFO streaming (see bma580_fifo.h). Register details: BMA580 datasheet
 * (BST-BMA580-DS000-05): start-up procedure p. 14, FIFO section 4.6, register map. */
#include <errno.h>
#include <zephyr/kernel.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>

#include "bma580_fifo.h"

#define BMA580_ADDR        0x18
#define REG_CHIP_ID        0x00
#define REG_HEALTH_STATUS  0x02
#define REG_CMD_SUSPEND    0x04
#define REG_ACC_CONF_0     0x30
#define REG_ACC_CONF_1     0x31
#define REG_ACC_CONF_2     0x32
#define REG_INT2_CONF      0x35
#define REG_FEAT_ENG_CONF  0x50
#define REG_CMD            0x7E
#define REG_FIFO_LEVEL_0   0x22   /* fill level in bytes (data only, headers are not stored) */
#define REG_FIFO_DATA_OUT  0x24   /* burst read: the address does not increment */
#define REG_FIFO_CTRL      0x40
#define REG_FIFO_CONF_0    0x41
#define REG_FIFO_CONF_1    0x42

#define CHIP_ID_BMA580     0xC4
#define CMD_SOFTRESET      0xB6
#define ACC_CONF_0_OFF     0x00   /* accelerometer off while it is configured */
#define ACC_CONF_0_ON      0x0F   /* accelerometer on (reset value) */
#define ACC_CONF_1_VAL     0xA5   /* HPM, normal filter, ODR 50 Hz (same as BMA580_init in bma580.c) */
#define ACC_CONF_2_VAL     0x0E   /* +/-8 g, IIR roll-off -60 dB */
#define FIFO_CTRL_RST      0x01
#define FIFO_CONF_0_VAL    0x0F   /* fifo_cfg = 1, x, y, z on, no compression */
#define FIFO_CONF_1_VAL    0x03   /* 1024 bytes, no sensor time, stop_on_full = 0 (oldest frame dropped) */

#define FIFO_SIZE          1024
#define PAYLOAD            6      /* x, y, z: 2 bytes each, LSB first */
#define FRAME              (1 + PAYLOAD)
#define FIFO_FULL_LEVEL    (FIFO_SIZE - 2 * PAYLOAD)   /* datasheet 4.6.1.4 */
#define HDR_ACC_XYZ        0xCE   /* const_1, frame_type = acceleration, z/y/x enabled */
#define HDR_EMPTY          0x80   /* FIFO empty */

static int reg_write(const struct device *i2c, uint8_t reg, uint8_t val)
{
	const uint8_t buf[2] = {reg, val};

	return i2c_write(i2c, buf, sizeof(buf), BMA580_ADDR);
}

static int reg_read(const struct device *i2c, uint8_t reg, uint8_t *buf, uint32_t len)
{
	return i2c_burst_read(i2c, BMA580_ADDR, reg, buf, len);
}

/* After power-on or soft reset, the first I2C transaction only selects the interface and is not
 * acknowledged (datasheet p. 14). On this board more than one access can fail, so CHIP_ID is read
 * until it answers. Returns the number of attempts (1..10), or a negative error. */
static int wait_chip_id(const struct device *i2c, uint8_t *id)
{
	int err = -EIO;

	for (int attempt = 1; attempt <= 10; attempt++) {
		err = reg_read(i2c, REG_CHIP_ID, id, 1);
		if (err == 0 && *id == CHIP_ID_BMA580) {
			return attempt;
		}
		k_msleep(2);
	}
	return err ? err : -ENODEV;
}

/* write, then report which register failed */
#define CHECK_WRITE(reg, val)                                                                      \
	do {                                                                                       \
		const int e_ = reg_write(i2c, (reg), (val));                                       \
		if (e_) {                                                                          \
			printk("I,bma580 write reg 0x%02x failed err=%d\n", (reg), e_);           \
			return e_;                                                                 \
		}                                                                                  \
	} while (0)

int bma580_fifo_start(const struct device *i2c)
{
	uint8_t id = 0, health = 0, acc[3], fifo[2];
	int err;

	/* datasheet start-up: soft reset, wait, select I2C, CHIP_ID, leave suspend, health */
	err = wait_chip_id(i2c, &id);                        /* in case the sensor was just powered */
	if (err < 0) {
		printk("I,bma580 no answer before reset: chip id 0x%02x err=%d\n", id, err);
	}
	(void)reg_write(i2c, REG_CMD, CMD_SOFTRESET);
	k_msleep(5);                                         /* power-on time 1.8 ms, 3 ms recommended */
	err = wait_chip_id(i2c, &id);
	if (err < 0) {
		printk("I,bma580 chip id 0x%02x err=%d (expected 0xc4)\n", id, err);
		return err;
	}
	printk("I,bma580 chip id 0xc4 after reset: %d attempt(s)\n", err);
	CHECK_WRITE(REG_CMD_SUSPEND, 0x00);                 /* SUSPEND -> ACTIVE */
	k_msleep(5);
	err = reg_read(i2c, REG_HEALTH_STATUS, &health, 1);
	if (err || (health & 0x0F) != 0x0F) {
		printk("I,bma580 health 0x%02x err=%d (expected 0x0f)\n", health, err);
		return err ? err : -EIO;
	}
	CHECK_WRITE(REG_INT2_CONF, 0x01);                   /* datasheet start-up step when I2C is used */

	/* accelerometer: 50 Hz, +/-8 g */
	CHECK_WRITE(REG_ACC_CONF_0, ACC_CONF_0_OFF);
	CHECK_WRITE(REG_ACC_CONF_1, ACC_CONF_1_VAL);
	CHECK_WRITE(REG_ACC_CONF_2, ACC_CONF_2_VAL);
	CHECK_WRITE(REG_ACC_CONF_0, ACC_CONF_0_ON);

	/* FIFO: the feature engine (on after reset) locks the FIFO at 512 bytes; it is not used here.
	 * Size first: fifo_cfg cannot be set while fifo_size is 0. These writes also flush the FIFO. */
	CHECK_WRITE(REG_FEAT_ENG_CONF, 0x00);
	CHECK_WRITE(REG_FIFO_CONF_1, FIFO_CONF_1_VAL);
	CHECK_WRITE(REG_FIFO_CONF_0, FIFO_CONF_0_VAL);
	CHECK_WRITE(REG_FIFO_CTRL, FIFO_CTRL_RST);

	err = reg_read(i2c, REG_ACC_CONF_0, acc, sizeof(acc));
	err = err ? err : reg_read(i2c, REG_FIFO_CONF_0, fifo, sizeof(fifo));
	if (err || acc[1] != ACC_CONF_1_VAL || (acc[2] & 0x1F) != ACC_CONF_2_VAL ||
	    (fifo[0] & 0x1F) != FIFO_CONF_0_VAL || (fifo[1] & 0x1F) != FIFO_CONF_1_VAL) {
		printk("I,bma580 read back acc_conf %02x %02x %02x fifo_conf %02x %02x err=%d\n",
		       acc[0], acc[1], acc[2], fifo[0], fifo[1], err);
		return err ? err : -EIO;
	}
	return 0;
}

int bma580_fifo_read(const struct device *i2c, int16_t (*out)[3], int max, bool *overflow)
{
	static uint8_t buf[BMA580_FIFO_MAX_FRAMES * FRAME];
	uint8_t lv[2];

	*overflow = false;
	int err = reg_read(i2c, REG_FIFO_LEVEL_0, lv, sizeof(lv));

	if (err) {
		return err;
	}
	const int level = lv[0] | ((lv[1] & 0x07) << 8);
	const int frames = MIN(MIN(level / PAYLOAD, max), BMA580_FIFO_MAX_FRAMES);

	*overflow = (level >= FIFO_FULL_LEVEL);
	if (frames == 0) {
		return 0;
	}
	/* read whole frames only: a partly read frame would corrupt the next read */
	err = reg_read(i2c, REG_FIFO_DATA_OUT, buf, (uint32_t)(frames * FRAME));
	if (err) {
		return err;
	}
	int n = 0;

	for (int i = 0; i < frames * FRAME; i += FRAME) {
		if (buf[i] == HDR_EMPTY) {
			break;
		}
		if (buf[i] != HDR_ACC_XYZ) {
			return -EBADMSG;          /* out of step with the frames */
		}
		const uint8_t *p = &buf[i + 1];

		for (int k = 0; k < 3; k++) {
			out[n][k] = (int16_t)(p[2 * k] | (p[2 * k + 1] << 8));
		}
		n++;
	}
	return n;
}
