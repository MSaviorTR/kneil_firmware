/*
 * BMA580 start-up and FIFO streaming for the KNEIL live app (50 Hz, +/-8 g, same settings as
 * BMA580_init() in bma580.c). It follows the datasheet start-up procedure, including the dummy read
 * that selects I2C after a reset. bma580.c itself is not changed (and not used by the KNEIL build).
 */
#ifndef BMA580_FIFO_H
#define BMA580_FIFO_H

#include <stdbool.h>
#include <stdint.h>
#include <zephyr/device.h>

#define BMA580_LSB_PER_G       4096   /* +/-8 g range, 16 bit */
#define BMA580_FIFO_MAX_FRAMES 146    /* 1024-byte FIFO / 7 bytes per frame (header + x, y, z) */

/* Reset and configure the sensor and its FIFO (1024 bytes, x/y/z, 16 bit, oldest frame dropped
 * when full). Returns 0, or a negative error if the FIFO configuration does not read back. */
int bma580_fifo_start(const struct device *i2c);

/* Read all complete frames (at most max) into out[i] = raw x, y, z (sensor axes, LSB).
 * Returns the number of frames, or a negative error (I2C error or unexpected frame: restart the FIFO).
 * *overflow is set if the FIFO was full, i.e. samples were lost before this read. */
int bma580_fifo_read(const struct device *i2c, int16_t (*out)[3], int max, bool *overflow);

#endif /* BMA580_FIFO_H */
