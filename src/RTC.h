#ifndef RTC_H
#define RTC_H


#include <stdint.h>
#include <zephyr/device.h>

//void RTC_init_ContDown(const struct device *dev);
//void RTC_Start_CountDown(const struct device *dev);

int RTC_set_datetime(const struct device *i2c,
                     uint16_t year, uint8_t month, uint8_t date, uint8_t weekday,
                     uint8_t hour24, uint8_t minute, uint8_t second);
int RTC_alarm_init(const struct device *dev, uint8_t mins);

//void RTC_alarm_set(const struct device *dev);
//void RTC_recover_int(const struct device *dev);

int RTC_clkout_disable(const struct device *dev);

int RTC_enable_porie_interrupt(const struct device *dev);

int RTC_set_to_build_time(const struct device *i2c);

void RTC_read_time(const struct device *dev, uint8_t *seconds, uint8_t *minutes,
                   uint8_t *hours, uint8_t *date, uint8_t *month, uint8_t *year);

int RTC_Check_Alarm(const struct device *dev);

void RTC_recover_int(const struct device *dev);

void RTC_debug_dump(const struct device *dev, const char *label);
#endif
