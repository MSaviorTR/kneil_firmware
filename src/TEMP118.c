
#include "TEMP118.h"
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/sys/printk.h>
#include <string.h>

#define Temp_Data            0x00
#define TEMP118_Addr         0x49
#define TEMP118_config       0x01

#define ShutDown_Mode_MSB        0x61
#define ShutDown_Mode_LSB        0xB0
#define Continue_Mode_MSB        0x60
#define Continue_Mode_LSB        0xB0



void readTemp118(const struct device *dev, uint8_t *  temp){

uint8_t reg = Temp_Data ;
uint8_t data[2];
   int ret = i2c_write_read(dev,TEMP118_Addr,&reg,1,data,sizeof(data));
 if (ret < 0) {
        printk("[TMP] R 0x%02X reg=0x%02X len=2 -> FAIL %d\n",
               TEMP118_Addr, reg, ret);
        return;
    }
    printk("[TMP] R 0x%02X reg=0x%02X len=2 -> 0x%02X 0x%02X\n",
           TEMP118_Addr, reg, data[0], data[1]);
   memcpy(temp,data,sizeof(data));
     printk("Temp data: %02X %02X \n",
       data[0], data[1]);
}
static int write_reg(const struct device *dev, uint8_t reg, uint8_t data_MSB,uint8_t data_LSB)
{
    uint8_t buf[3] = {reg, data_MSB,data_LSB};
    int ret = i2c_write(dev, buf, sizeof(buf), TEMP118_Addr);

    if (ret < 0) {
        printk("[TMP] W 0x%02X reg=0x%02X data=0x%02X%02X -> FAIL %d\n",
               TEMP118_Addr, reg, data_MSB, data_LSB, ret);
    } else {
        printk("[TMP] W 0x%02X reg=0x%02X data=0x%02X%02X -> ok\n",
               TEMP118_Addr, reg, data_MSB, data_LSB);
    }

    return ret;
}
void TMP_sleep(const struct device *dev){

int ret = write_reg(dev,TEMP118_config,ShutDown_Mode_MSB,ShutDown_Mode_LSB);
if (ret < 0) {
        printk("sleep failed\n");

}
}
void TMP_wakeup(const struct device *dev){

int ret = write_reg(dev,TEMP118_config,Continue_Mode_MSB,Continue_Mode_LSB);

if (ret < 0) {
        printk("sleep wake up\n");

}
}
