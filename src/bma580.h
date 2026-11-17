#ifndef BMA580_H
#define BMA580_H

#include <stdint.h>
#include <zephyr/device.h>
void BMA580_init(const struct device *dev);
void readAccel(const struct device *dev, uint8_t *data_xyz);
void readtemp(const struct device *dev, uint8_t * const temp);
void BMA_wakeup(const struct device *dev);
void BMA_sleep(const struct device *dev);
#endif // BMA580_H
