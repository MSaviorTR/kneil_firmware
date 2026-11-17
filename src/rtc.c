#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/sys/printk.h>
#include <string.h>
#include <time.h>

#define RCV_Address                     0x52
#define RCV_Control                     0x0E
#define RCV_Timer_Value_0               0x0A
#define RCV_Timer_Value_1               0x0B
#define RCV_Control_1                   0x0F
#define RCV_Control_2                   0x10
#define RCV_Clock_Int                   0x12
#define RCV_REG_SECONDS                 0x00
#define RCV_REG_MINUTES                 0x01
#define RCV_REG_HOURS                   0x02

#define RCV_REG_MIN_ALM                 0x07    /* AE_M + minute BCD */
#define RCV_REG_HOUR_ALM                0x08    /* AE_H + hour BCD (or AM/PM) */
#define RCV_REG_WD_DATE_ALM             0x09    /* AE_WD + weekday/date BCD */
#define RCV_REG_STATUS                  0x0E    /* ... AF bit (bit2) ... */
#define RCV_REG_CTRL1                   0x0F    /* ... WADA (bit5) ... */
#define RCV_REG_CTRL2                   0x10
#define RCV_REG_WEEKDAY                 0x03
#define RCV_REG_DATE                    0x04
#define RCV_REG_MONTH                   0x05
#define RCV_REG_YEAR                    0x06

#define RCV_REG_CLKOUT                  0x35   /* CLKOUT control register (RAM mirror) */

#define RCV_REG_EE_CMD          0x27    /* EE Command register [cite: 702] */

#define BIT_EERD                3       /* Control1(0x0F) EEPROM Refresh Disable [cite: 497] */
#define BIT_EEBUSY              7       /* Status(0x0E) EEPROM Busy [cite: 486] */
#define BIT_PORIE               3       /* CLKOUT(0x35) Power On Reset IRQ Enable [cite: 781] */

#define EE_CMD_FIRST            0x00    /* First command for EEPROM access [cite: 704, 707] */
#define EE_CMD_UPDATE_ALL       0x11    /* Update: All RAM mirror -> EEPROM [cite: 707] */

#define EEPROM_WRITE_WAIT_MS    10      /* Wait time before polling EEbusy after write [cite: 1334] */
#define EEPROM_POLL_TIMEOUT_MS  100     /* Max time to wait for EEbusy */

#define BIT_CLKF   6          /* Status(0x0E) Clock Flag */
#define BIT_CLKIE  6          /* Control2(0x10) Clock Interrupt Enable */

#define BIT_AF   2                 /* Status.AF (0x0E bit2) */
#define BIT_WADA 5                 /* Control1.WADA (0x0F bit5) */
#define BIT_AIE  3
#define BIT_PORF 0                 /* Status.PORF (0x0E bit0) */

static int write_reg(const struct device *dev, uint8_t reg, uint8_t data)
{
    uint8_t buf[2] = {reg, data};
    int ret = i2c_write(dev, buf, sizeof(buf), RCV_Address);

    if (ret < 0) {
        printk("[RTC] W 0x%02X reg=0x%02X data=0x%02X -> FAIL %d\n",
               RCV_Address, reg, data, ret);
    } else {
        printk("[RTC] W 0x%02X reg=0x%02X data=0x%02X -> ok\n",
               RCV_Address, reg, data);
    }

    return ret;
}

static int read_reg(const struct device *dev, uint8_t reg, uint8_t *data)
{
    int ret = i2c_write_read(dev, RCV_Address, &reg, 1, data, 1);

    if (ret < 0) {
        printk("[RTC] R 0x%02X reg=0x%02X -> FAIL %d\n", RCV_Address, reg, ret);
    } else {
        printk("[RTC] R 0x%02X reg=0x%02X -> 0x%02X\n", RCV_Address, reg, *data);
    }

    return ret;
}

static int update_bit(const struct device *dev, uint8_t reg, uint8_t bit, uint8_t set)
{
    uint8_t val;
    int rc = read_reg(dev, reg, &val);
    if (rc < 0) return rc;

    uint8_t mask = (uint8_t)BIT(bit);
    val = (uint8_t)((val & ~mask) | (set ? mask : 0));

    return write_reg(dev, reg, val);

}

static inline uint8_t dec_to_bcd(uint8_t d) {
    return (uint8_t)(((d / 10) << 4) | (d % 10));
}

static inline uint8_t bcd_to_dec(uint8_t b) {
    return (uint8_t)(((b >> 4) * 10) + (b & 0x0F));
}

/**
 * @brief Waits for the EEPROM busy flag to clear.
 * * Follows the procedure from the manual[cite: 1298, 1317].
 */
static int wait_for_eebusy(const struct device *dev)
{
    uint8_t status;
    int ret;
    int timeout = EEPROM_POLL_TIMEOUT_MS;

    while (timeout > 0) {
        ret = read_reg(dev, RCV_REG_STATUS, &status);
        if (ret < 0) {
            return ret;
        }
        /* Check if EEbusy bit (bit 7) is 0 */
        if (!(status & BIT(BIT_EEBUSY))) {
            return 0; /* Success, not busy */
        }
        k_msleep(1);
        timeout--;
    }
    printk("Timeout waiting for EEbusy\n");
    return -ETIMEDOUT;
}

int RTC_enable_porie_interrupt(const struct device *dev){
    int ret;
    printk("Enabling PORIE interrupt in EEPROM...\n");

    /* * To write to EEPROM, we must follow the "Update" procedure [cite: 1253-1258].
     * 1. Disable automatic refresh (EERD = 1)
     * 2. Wait for EEbusy = 0
     * 3. Write new value to RAM mirror (set PORIE=1 in reg 0x35)
     * 4. Send EEPROM Update command (0x00, then 0x11)
     * 5. Wait for EEbusy = 0
     * 6. Re-enable automatic refresh (EERD = 0)
     */

    /* 1. Disable automatic refresh (EERD = 1 in reg 0x0F) */
    ret = update_bit(dev, RCV_REG_CTRL1, BIT_EERD, 1);
    if (ret) return ret;

    /* 2. Wait until EEPROM is not busy */
    ret = wait_for_eebusy(dev);
    if (ret) return ret;

    /* 3. Set PORIE bit in the RAM mirror (Bit 3 in reg 0x35) */
    ret = update_bit(dev, RCV_REG_CLKOUT, BIT_PORIE, 1);
    if (ret) return ret;

    /* 4. Send Update command (0x11) to save RAM mirror to EEPROM */
    ret = write_reg(dev, RCV_REG_EE_CMD, EE_CMD_FIRST);
    if (ret) return ret;
    ret = write_reg(dev, RCV_REG_EE_CMD, EE_CMD_UPDATE_ALL);
    if (ret) return ret;

    /* 5. Wait for the EEPROM write to complete.
     * The manual's flowchart shows a 10ms wait *before* polling. [cite: 1334]
     */
    k_msleep(EEPROM_WRITE_WAIT_MS);
    ret = wait_for_eebusy(dev);
    if (ret) return ret;

    /* 6. Enable automatic refresh (EERD = 0) [cite: 1258] */
    ret = update_bit(dev, RCV_REG_CTRL1, BIT_EERD, 0);

    printk("RV-3028 PORIE interrupt enabled in EEPROM\n");
    return ret;
}

int RTC_set_datetime(const struct device *i2c,
                     uint16_t year, uint8_t month, uint8_t date, uint8_t weekday,
                     uint8_t hour24, uint8_t minute, uint8_t second){
    int rc ;
    if (!device_is_ready(i2c)) return -ENODEV;
    if (month < 1 || month > 12)   return -EINVAL;
    if (date  < 1 || date  > 31)   return -EINVAL;
    if (weekday > 6)               return -EINVAL;   /* user-defined 0..6 */
    if (hour24 > 23)               return -EINVAL;
    if (minute > 59 || second > 59)return -EINVAL;
    if (year < 2000 || year > 2099)return -EINVAL;

    uint8_t y = (uint8_t)(year % 100);


    /* Write all time/calendar fields (any order) */
    rc = write_reg(i2c, RCV_REG_MINUTES, dec_to_bcd(minute));     if (rc) return rc;
    rc = write_reg(i2c, RCV_REG_HOURS,   dec_to_bcd(hour24));     if (rc) return rc;
    rc = write_reg(i2c, RCV_REG_WEEKDAY, weekday & 0x07);         if (rc) return rc;
    rc = write_reg(i2c, RCV_REG_DATE,    dec_to_bcd(date));       if (rc) return rc;
    rc = write_reg(i2c, RCV_REG_MONTH,   dec_to_bcd(month));      if (rc) return rc;
    rc = write_reg(i2c, RCV_REG_YEAR,    dec_to_bcd(y));          if (rc) return rc;

    /* ...then write SECONDS LAST to synchronize the 1 Hz prescaler. */
    rc = write_reg(i2c, RCV_REG_SECONDS, dec_to_bcd(second));     if (rc) return rc;
    rc =  write_reg(i2c, RCV_Clock_Int, 0x00);


    //printk("RV-3028 time set to %04u-%02u-%02u (wday=%u) %02u:%02u:%02u\n",
    //       year, month, date, weekday, hour24, minute, second);
    return 0;
}

int RTC_alarm_init(const struct device *dev, uint8_t mins){

    int ret;
    /* 1) Disable AIE while we configure*/
    ret = update_bit(dev, RCV_REG_CTRL2, BIT_AIE, 0);
    if (ret) return ret;

    /* 2) Clear AF (Alarm Flag) */
    ret = update_bit(dev, RCV_Control, BIT_AF, 0);
    if (ret) return ret;

    /* 3) Set seconds alarm with AE_S=0 to enable seconds compare */
    uint8_t min_alm = dec_to_bcd(mins) & 0x7F;   /* bit7=0 => enable minute compare */
    ret = write_reg(dev, RCV_REG_MIN_ALM, min_alm);
    if (ret) return ret;

    /* 4) Disable hour and weekday/date comparisons */
    ret = write_reg(dev, RCV_REG_HOUR_ALM, 0x80);      /* bit7=1 => disable hour */
    if (ret) return ret;
    ret = write_reg(dev, RCV_REG_WD_DATE_ALM, 0x80);   /* bit7=1 => disable weekday/date */
    if (ret) return ret;

    /* 5) Enable AIE (Alarm Interrupt Enable) */
    ret = update_bit(dev, RCV_REG_CTRL2, BIT_AIE, 1);
    if (ret) return ret;

    /* 6) Clear PORF (Power On Reset Flag) */
    ret = update_bit(dev, RCV_Control, BIT_PORF, 0);
    if (ret) return ret;

    //printk("RV-3028 seconds alarm set to trigger at %u seconds every minute\n", secs);
    return 0;
}

/**
 * @brief Parse compile-time date/time and set RTC
 * Uses __DATE__ and __TIME__ macros
 */
/*int RTC_set_to_build_time(const struct device *i2c)
{
    // __DATE__ format: "Jan 01 2025"
    // __TIME__ format: "12:34:56"

    const char *date = __DATE__;
    const char *time = __TIME__;

    // Month lookup table
    const char *months[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};

    // Parse date
    char month_str[4];
    int day, year;
    sscanf(date, "%s %d %d", month_str, &day, &year);

    // Find month number
    int month = 0;
    for (int i = 0; i < 12; i++) {
        if (strcmp(month_str, months[i]) == 0) {
            month = i + 1;
            break;
        }
    }

    // Parse time
    int hour, minute, second;
    sscanf(time, "%d:%d:%d", &hour, &minute, &second);

    // Calculate weekday (simplified - just use day % 7)
    uint8_t weekday = (uint8_t)(day % 7);

    printk("Setting RTC to build time: %04d-%02d-%02d %02d:%02d:%02d\n",
           year, month, day, hour, minute, second);

    return RTC_set_datetime(i2c, year, month, day, weekday,
                           hour, minute, second);
}*/

int RTC_clkout_disable(const struct device *dev){
    int rc;

    if (!device_is_ready(dev)) return -ENODEV;

    /* 1) Stop interrupt-gated clocking: CTRL2.CLKIE = 0 */
    rc = update_bit(dev, RCV_REG_CTRL2, BIT_CLKIE, 0);     if (rc) return rc;

    /* 2) If a clock gate is currently active, drop it: STATUS.CLKF = 0 */
    rc = update_bit(dev, RCV_REG_STATUS, BIT_CLKF, 0);     if (rc) return rc;

    /* 3) Force pin LOW via CLKOUT register:
          - clear CLKOE (bit7) and set FD=111 (bits2..0) - output stays LOW
     */
    uint8_t v;
    rc = read_reg(dev, RCV_REG_CLKOUT, &v);                if (rc) return rc;
    v &= (uint8_t)~(BIT(7) | 0x07);   /* clear CLKOE and FD */
    v |= 0x07;                        /* FD = 111 -> LOW */
    rc = write_reg(dev, RCV_REG_CLKOUT, v);                if (rc) return rc;

    printk("RV-3028 CLKOUT disabled (forced LOW)\n");
    return 0;
}

void RTC_read_time(const struct device *dev, uint8_t *seconds, uint8_t *minutes,
                   uint8_t *hours, uint8_t *date, uint8_t *month, uint8_t *year){
    read_reg(dev, RCV_REG_SECONDS, seconds);
    read_reg(dev, RCV_REG_MINUTES, minutes);
    read_reg(dev, RCV_REG_HOURS, hours);
    read_reg(dev, RCV_REG_DATE, date);
    read_reg(dev, RCV_REG_MONTH, month);
    read_reg(dev, RCV_REG_YEAR, year);
}

int RTC_Check_Alarm(const struct device *dev){
    int rc;
    uint8_t yt;

    rc = read_reg(dev,RCV_REG_YEAR , &yt);

    printk("RV-3028 check alarm\n");
    printk("Alarm status: 0x%02X\n", yt);
    if (yt == 0x00){
        return 1;
    }
    else{
        return 0;
    }
}

void RTC_recover_int(const struct device *dev){
    int ret = update_bit(dev, RCV_Control, BIT_AF, 0);
}

/**
 * @brief Prints the RTC's actual live register contents (not just whether
 * the last I2C transaction errored), so date/time/alarm state can be
 * confirmed directly instead of inferred from write return codes.
 */
void RTC_debug_dump(const struct device *dev, const char *label){
    uint8_t sec = 0, min = 0, hr = 0, date = 0, month = 0, year = 0;
    RTC_read_time(dev, &sec, &min, &hr, &date, &month, &year);

    uint8_t status = 0, ctrl2 = 0, min_alm = 0, hour_alm = 0, wd_alm = 0;
    read_reg(dev, RCV_REG_STATUS, &status);
    read_reg(dev, RCV_REG_CTRL2, &ctrl2);
    read_reg(dev, RCV_REG_MIN_ALM, &min_alm);
    read_reg(dev, RCV_REG_HOUR_ALM, &hour_alm);
    read_reg(dev, RCV_REG_WD_DATE_ALM, &wd_alm);

    printk("--- RTC dump [%s] ---\n", label);
    printk("Time: 20%02u-%02u-%02u %02u:%02u:%02u (raw BCD: %02X-%02X-%02X %02X:%02X:%02X)\n",
           bcd_to_dec(year), bcd_to_dec(month), bcd_to_dec(date),
           bcd_to_dec(hr), bcd_to_dec(min), bcd_to_dec(sec),
           year, month, date, hr, min, sec);
    printk("STATUS=0x%02X (AF=%u PORF=%u)  CTRL2=0x%02X (AIE=%u)\n",
           status, (status >> BIT_AF) & 1, (status >> BIT_PORF) & 1,
           ctrl2, (ctrl2 >> BIT_AIE) & 1);
    printk("MIN_ALM=0x%02X HOUR_ALM=0x%02X WD_DATE_ALM=0x%02X\n",
           min_alm, hour_alm, wd_alm);
    printk("------------------------\n");
}
