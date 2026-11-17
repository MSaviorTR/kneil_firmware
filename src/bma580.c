#include "bma580.h"
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/sys/printk.h>
#include <string.h>
#include <errno.h>

#define BMA580_Addr                     0x18
#define BMA580_CHIP_ID                  0x00
#define BMA580_CHIP_ID_VAL              0xC4
#define BMA580_INT2_CONF                0x35
#define BMA580_ACC_CONF_0				0x30  //default 0x0F
#define BMA580_ACC_CONF_1				0x31 	// 0xA8 high power mode, normal mode , 400 hz
#define BMA580_ACC_CONF_2				0x32  // defalt 0x0E
#define BMA580_ACC_DATA_0				0x18
#define BMA580_ACC_DATA_1				0x19
#define BMA580_ACC_DATA_2				0x1A
#define BMA580_ACC_DATA_3				0x1B
#define BMA580_ACC_DATA_4				0x1C
#define BMA580_ACC_DATA_5				0x1D
#define BMA580_Tem_DATA					0x1E
// #define BMA580_cmd_SUSPEND				0x1E // <-- This was incorrect, 0x1E is TEMP_DATA
#define BMA580_CMD_SUSPEND				0x04 // <-- Correct register (CMD_SUSPEND) from datasheet p. 68
#define BMA580_CMD_RESET				0x7E
#define BMA580_acc_STATUS				0x11


static int write_reg(const struct device *dev, uint8_t reg, uint8_t data)
{
    uint8_t buf[2] = {reg, data};
    int ret = i2c_write(dev, buf, sizeof(buf), BMA580_Addr);

    if (ret < 0) {
        printk("[BMA] W 0x%02X reg=0x%02X data=0x%02X -> FAIL %d\n",
               BMA580_Addr, reg, data, ret);
    } else {
        printk("[BMA] W 0x%02X reg=0x%02X data=0x%02X -> ok\n",
               BMA580_Addr, reg, data);
    }
    return ret;
}

/**
 * @brief Waits until the BMA580 answers on I2C after power-on or a soft reset.
 * @note Datasheet p. 14: after power-on or soft reset, the first I2C access only selects the
 *       interface and is not acknowledged (NACK). On this board the next access can fail too, so
 *       CHIP_ID is read until it returns 0xC4 (at most 10 tries, 2 ms apart).
 * @return 0 when the sensor answers, -EIO otherwise.
 */
static int BMA_wait_chip_id(const struct device *dev)
{
    uint8_t reg = BMA580_CHIP_ID;
    uint8_t id = 0;

    for (int attempt = 1; attempt <= 10; attempt++) {
        if (i2c_write_read(dev, BMA580_Addr, &reg, 1, &id, 1) == 0 && id == BMA580_CHIP_ID_VAL) {
            printk("[BMA] chip id 0x%02X after %d attempt(s)\n", id, attempt);
            return 0;
        }
        k_sleep(K_MSEC(2));
    }
    printk("[BMA] no answer to chip id read (last 0x%02X)\n", id);
    return -EIO;
}

/**
 * @brief Performs a soft-reset on the BMA580.
 * @note Per datasheet section 4.1, the device enters SUSPEND mode after a reset.
 */
void BMA_soft_reset(const struct device *dev){
    k_sleep(K_MSEC(20));
    // Write 0xB6 to CMD register 0x7E to trigger soft-reset
    write_reg(dev, BMA580_CMD_RESET, 0xB6);
    // Wait for device to reboot (datasheet p. 22, power on time is ~1.8ms)
    k_sleep(K_MSEC(50));
    // Select I2C again (the first access after a reset is not acknowledged), see BMA_wait_chip_id()
    BMA_wait_chip_id(dev);
}

/**
 * @brief Wakes the BMA580 from SUSPEND mode to ACTIVE mode.
 * @note This is done by writing 0x00 to the CMD_SUSPEND register (0x04).
 */
void BMA_wakeup(const struct device *dev){
    // Write 0x00 to CMD_SUSPEND register (0x04) to disable suspend mode
    int ret = write_reg(dev, BMA580_CMD_SUSPEND, 0x00);
    if (ret < 0) {
        printk("BMA580 failed to enter ACTIVE mode (wakeup)\n");
    }
    // Give the sensor time to start up (datasheet p. 26, ~3.15ms)
    k_sleep(K_MSEC(5));
}


void BMA580_init(const struct device *dev)
{
    if (!device_is_ready(dev)) {
        printk("BMA580 I2C device not ready\n");
        return;
    }

    // 1. Reset the device. This leaves it in SUSPEND mode.
    BMA_soft_reset(dev);

    // 2. Wake the device up to ACTIVE mode.
    BMA_wakeup(dev);

    // 2b. I2C is used: configure INT2 (pin 3, CSB in SPI mode) as an output, as the datasheet
    //     recommends (section 5.2.1.1), so the pin cannot trigger an erroneous SPI detection.
    write_reg(dev, BMA580_INT2_CONF, 0x01);

    // 3. Configure the sensor (as per datasheet Figure 6)
    write_reg(dev, BMA580_ACC_CONF_0, 0x00);           // Disable accelerometer to change config
    write_reg(dev, BMA580_ACC_CONF_1, 0xA5);           // ODR 50Hz (acc_odr=0x5), Normal filter, HPM
    write_reg(dev, BMA580_ACC_CONF_2, 0x0E);           // Range +/- 8g (acc_range=0b10, 4096 LSB/g), Roll-off -60dB
    write_reg(dev, BMA580_ACC_CONF_0, 0x0F);           // Enable accelerometer

    k_sleep(K_MSEC(10));  // Allow configuration to settle
}



void readAccel(const struct device *dev, uint8_t *data_xyz)
{
    uint8_t data[6];
    uint8_t reg = BMA580_ACC_DATA_0;
    uint8_t status_reg = BMA580_acc_STATUS;
    uint8_t flag = 0;
    int retries = 2;

while (retries-- > 0) {
    int err = i2c_write_read(dev, BMA580_Addr, &status_reg, 1, &flag, 1);
    if (err < 0) {
        printk("[BMA] R 0x%02X reg=0x%02X (status) -> FAIL %d\n",
               BMA580_Addr, status_reg, err);
        return;
    }
    printk("[BMA] R 0x%02X reg=0x%02X (status) -> 0x%02X\n",
           BMA580_Addr, status_reg, flag);
    // Check acc_data_rdy bit (bit 0 of SENSOR_STATUS 0x11)
    if (flag & 0x01) {
        break;
    } else {
        printk("data not ready\n");
        k_sleep(K_MSEC(5));
    }
}

// If still not ready after retries
if (!(flag & 0x01)) {
    printk("Timeout, forcing reset\n");
    BMA_soft_reset(dev);      // Soft reset
    k_sleep(K_MSEC(20));
    BMA580_init(dev);         // Reapply config
    return;
}

    int ret = i2c_write_read(dev, BMA580_Addr, &reg, 1, data, sizeof(data));
    if (ret < 0) {
        printk("[BMA] R 0x%02X reg=0x%02X len=6 (accel) -> FAIL %d\n",
               BMA580_Addr, reg, ret);
        return;
    }
    printk("[BMA] R 0x%02X reg=0x%02X len=6 (accel) -> ok\n", BMA580_Addr, reg);

    memcpy(data_xyz, data, sizeof(data));
    printk("Accel data: %02X %02X %02X %02X %02X %02X\n",
           data_xyz[0], data_xyz[1], data_xyz[2],
           data_xyz[3], data_xyz[4], data_xyz[5]);
}


void readtemp(const struct device *dev, uint8_t * const temp)
{
    uint8_t reg = BMA580_Tem_DATA;
    uint8_t buf[2];

    int ret = i2c_write_read(dev, BMA580_Addr, &reg, 1, buf, 2);
    if (ret < 0) {
        printk("[BMA] R 0x%02X reg=0x%02X len=2 (temp) -> FAIL %d\n",
               BMA580_Addr, reg, ret);
        return;
    }
    printk("[BMA] R 0x%02X reg=0x%02X len=2 (temp) -> ok\n", BMA580_Addr, reg);
       printk("Temp data: %02X %02X \n", // Corrected printk message
           buf[0], buf[1]);
   memcpy(temp, buf, sizeof(buf));
}

/**
 * @brief Puts the BMA580 into SUSPEND mode.
 * @note This is done by writing 0x01 to the CMD_SUSPEND register (0x04).
 */
void BMA_sleep(const struct device *dev)
{
    // Write 0x01 to CMD_SUSPEND register (0x04) to enable suspend mode
    int ret = write_reg(dev, BMA580_CMD_SUSPEND, 0x01);
    if (ret < 0) {
        printk("BMA580 failed to enter suspend mode\n");
    }
    k_sleep(K_MSEC(2)); // Allow time for the mode to change
}
