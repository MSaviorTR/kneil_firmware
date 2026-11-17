#ifndef TEMP118_H
#define TEMP118_H


#include <stdint.h>
#include <zephyr/device.h>


void readTemp118(const struct device *dev, uint8_t *temp);
void readtempAS(uint8_t *data_xyz);
void TMP_sleep(const struct device *dev);
void TMP_wakeup(const struct device *dev);








#endif
