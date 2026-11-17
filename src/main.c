#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/sys/printk.h>
#include "bma580.h"
#include "TEMP118.h"
#include "RTC.h"
#include <zephyr/types.h>
#include <stddef.h>
#include <string.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/gap.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/bluetooth/gatt.h>

#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/drivers/comparator.h>
#include <zephyr/pm/device.h>
#include <zephyr/sys/poweroff.h>
#include <zephyr/sys/util.h>

// Make sure the node label matches your device tree, e.g., "interrupt" or "button0"
//tatic const struct gpio_dt_spec sw0 = GPIO_DT_SPEC_GET(DT_NODELABEL(interrupt), gpios);
// If your device tree uses a different label (e.g., "button0"), use that instead:
// static const struct gpio_dt_spec sw0 = GPIO_DT_SPEC_GET(DT_NODELABEL(button0), gpios);

#define DEVICE_NAME     CONFIG_BT_DEVICE_NAME
#define DEVICE_NAME_LEN (sizeof(DEVICE_NAME) - 1)
#define NOTIFY_INTERVAL 100
// ES4L15BA1 (nRF54L15): I2C SCL/SDA are on P1.06/P1.07, wired to the i2c21
// TWIM/pinctrl instance in boards/KAGAFEI/es4l15ba1/es4l15ba1_nrf54l15_cpuapp.dts
// (the previous nRF52832 board used the "i2c0" node label instead).
#define I2C_DEV_NODE DT_NODELABEL(i2c21)
#define STACK_SIZE 1024
#define PRIORITY 5
#define BT_UUID_SENSOR_SERVICE_VAL \
	BT_UUID_128_ENCODE(0x00001000, 0x0000, 0x1000, 0x8000, 0x5f9b34fb)

// define DEF_SVC1_ADC_VAL_1_UUID_128      {0xFB, 0x34, 0x9B, 0x5F, 0x80, 0x00, 0x00, 0x80, 0x00, 0x10, 0x00, 0x00, 0x01, 0x10, 0x00, 0x00}
// 0000 1001-0000-1000-8000-00805F9B34FB
#define BT_UUID_SENSOR_CHAR_VAL \
	BT_UUID_128_ENCODE(0x00001001, 0x0000, 0x1000, 0x8000, 0x00805F9B34FB)

static bool notify_enabled = false;
static struct bt_uuid_128 sensor_service_uuid = BT_UUID_INIT_128(BT_UUID_SENSOR_SERVICE_VAL);
static struct bt_uuid_128 sensor_char_uuid = BT_UUID_INIT_128(BT_UUID_SENSOR_CHAR_VAL);
#define I2C_ADDR 0x18  // Replace with your target device address
static const uint8_t url_data[] = {
    0x17,'/' ,'/',
    'w','w','w','.','n','e','i','l','.','t','e','c','h','/'
};
static struct k_work adv_work;
static bool is_connected = false;
static uint8_t sensor_data[20];
static const struct bt_data ad[]={
	BT_DATA_BYTES(BT_DATA_UUID128_ALL, BT_UUID_SENSOR_SERVICE_VAL),
	BT_DATA_BYTES(BT_DATA_FLAGS, BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR),
	BT_DATA(BT_DATA_NAME_COMPLETE, DEVICE_NAME, DEVICE_NAME_LEN),
};

const struct device *i2c_dev = DEVICE_DT_GET(I2C_DEV_NODE);


static const struct bt_data sd[]={

	//BT_DATA_BYTES(BT_DATA_UUID128_ALL, BT_UUID_SENSOR_SERVICE_VAL),
	BT_DATA(BT_DATA_URI,url_data,sizeof(url_data)),

};
static const struct bt_le_adv_param *adv_param = BT_LE_ADV_PARAM(
	(BT_LE_ADV_OPT_CONN |
	 BT_LE_ADV_OPT_USE_IDENTITY),
	640,
	800,
	NULL);

/* Wakes send_data_thread when it may have work to do (connected AND
 * notifications enabled), instead of it polling is_connected/notify_enabled
 * on a 20ms timer. */
static K_SEM_DEFINE(notify_state_sem, 0, 1);

static void sensor_ccc_cfg_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
	notify_enabled = (value & BT_GATT_CCC_NOTIFY);
	printk("Notify %s\n", notify_enabled ? "enabled" : "disabled");
	k_sem_give(&notify_state_sem);
}

BT_GATT_SERVICE_DEFINE(sensor_svc,
	BT_GATT_PRIMARY_SERVICE(&sensor_service_uuid),
	BT_GATT_CHARACTERISTIC(&sensor_char_uuid.uuid,
			           BT_GATT_CHRC_NOTIFY | BT_GATT_CHRC_READ,
                       BT_GATT_PERM_READ,
			       NULL, NULL, sensor_data),
	BT_GATT_CCC(sensor_ccc_cfg_changed,
		    BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
);


static void adv_work_handler(struct k_work *work){
	int err = bt_le_adv_start(adv_param, ad, ARRAY_SIZE(ad), sd, ARRAY_SIZE(sd));

	if (err) {
		printk("Advertising failed to start (err %d)\n", err);
		return;
	}

	printk("Advertising successfully started\n");
}

static void advertising_start(void){
	k_work_submit(&adv_work);
}
static void recycled_cb(void){
	printk("Connection object available from previous conn. Disconnect is complete!\n");
	advertising_start();
}

static void on_connected(struct bt_conn *conn, uint8_t err){
	if (err) {
		printk("Connection failed (err %u)\n", err);
		return;
	}
	/* Powering the sensors up costs ~100ms of blocking k_sleep()s, which is
	 * far too long to spend inside a Bluetooth callback - it stalls the BT
	 * RX thread. Just record the state and let send_data_thread do the work. */
	is_connected = true;
	k_sem_give(&notify_state_sem);
	printk("Connected\n");
}

static void on_disconnected(struct bt_conn *conn, uint8_t reason){
	is_connected = false;
	notify_enabled = false;
	/* Wake send_data_thread so it powers both sensors down. Doing it there
	 * rather than here also avoids racing with an in-flight sensor read. */
	k_sem_give(&notify_state_sem);
	printk("Disconnected (reason %u)\n", reason);
}

struct bt_conn_cb connection_callbacks = {
	.connected = on_connected,
	.disconnected = on_disconnected,
	.recycled = recycled_cb,
};

/* Tracks whether the two sensors are currently powered. Owned exclusively by
 * send_data_thread so that they are woken exactly once when a central
 * connects and shut down exactly once when it goes away. */
static bool sensors_on;

static void sensors_power_up(void)
{
	TMP_wakeup(i2c_dev);
	k_msleep(20);
	BMA580_init(i2c_dev);
	k_msleep(20);
	sensors_on = true;
	printk("Sensors powered up\n");
}

static void sensors_power_down(void)
{
	TMP_sleep(i2c_dev);
	BMA_sleep(i2c_dev);
	sensors_on = false;
	printk("Sensors powered down\n");
}

void send_data_thread(void)
{

	if (!device_is_ready(i2c_dev)) {
    printk("I2C device not ready!\n");
    return;
	}

	k_msleep(20);

	/* Nothing is connected at boot, so put both sensors into their idle
	 * state rather than leaving them in whatever mode they came up in -
	 * the TMP118 in particular powers up converting continuously. */
	sensors_power_down();

	while (1) {
		if (is_connected) {
			if (!sensors_on) {
				sensors_power_up();
			}

			if (notify_enabled) {
				uint8_t data_temp[2];
				uint8_t data_xyz[6];
				uint8_t packet[20] = {0};

				readAccel(i2c_dev, data_xyz);
				k_msleep(50);
				readTemp118(i2c_dev, data_temp);

				packet[3] = data_temp[0];
				packet[4] = data_temp[1];
				//uint8_t zero = 0;
				for (int i = 0; i < 6; i++) {
					packet[14 + i] = data_xyz[i];
				}
				memcpy(sensor_data, packet, sizeof(packet));
				bt_gatt_notify(NULL, &sensor_svc.attrs[2], sensor_data, sizeof(packet));
				k_sleep(K_MSEC(NOTIFY_INTERVAL));
				continue;
			}
		} else if (sensors_on) {
			sensors_power_down();
		}

		/* Either disconnected, or connected but not subscribed yet: there is
		 * nothing to sample until the connection/notify state changes. */
		k_sem_take(&notify_state_sem, K_FOREVER);
	}
}

K_THREAD_DEFINE(sensor_thread_id, STACK_SIZE,
		send_data_thread, NULL, NULL, NULL,
		PRIORITY, 0, 0);

/*void print_time_thread(void)
{
    if (!device_is_ready(i2c_dev)) {
        printk("I2C device not ready!\n");
        return;
    }

    while (1) {
        uint8_t seconds, minutes, hours, date, month, year;

        // Use the new RTC function
        RTC_read_time(i2c_dev, &seconds, &minutes, &hours, &date, &month, &year);

        // Convert BCD to decimal
        uint8_t sec = ((seconds >> 4) * 10) + (seconds & 0x0F);
        uint8_t min = ((minutes >> 4) * 10) + (minutes & 0x0F);
        uint8_t hr = ((hours >> 4) * 10) + (hours & 0x0F);
        uint8_t d = ((date >> 4) * 10) + (date & 0x0F);
        uint8_t mon = ((month >> 4) * 10) + (month & 0x0F);
        uint8_t yr = ((year >> 4) * 10) + (year & 0x0F);

        // Print time
        printk("RTC Time: 20%02d-%02d-%02d %02d:%02d:%02d\n",
               yr, mon, d, hr, min, sec);

        k_sleep(K_SECONDS(1));
    }
}

// Add thread definition before main()
K_THREAD_DEFINE(print_time_thread_id, STACK_SIZE,
        print_time_thread, NULL, NULL, NULL,
        PRIORITY, 0, 0);*/

int main(void)
{
	//printk("\n%s system off demo\n", CONFIG_BOARD);

	//*(volatile uint32_t *)0x40000578 = 1;

	//RTC_clkout_disable(i2c_dev);
	//RTC_enable_porie_interrupt(i2c_dev);


	int alarm_status;

	if (!device_is_ready(i2c_dev)) {
        printk("I2C device not ready!\n");
        return 0;
    }

	alarm_status = RTC_Check_Alarm(i2c_dev);
	RTC_debug_dump(i2c_dev, "boot, before set_datetime");
	if (alarm_status == 1){
		printk("Alarm Date not set\n");
		RTC_set_datetime(i2c_dev,2025,1,26,1,12,0,0);
		printk("\n Date set \n");
		RTC_debug_dump(i2c_dev, "after set_datetime");
	}

	int err = bt_enable(NULL);

	if (err){
		printk("BLE Intialise Fail\n");
	}
	printk("Bluetooth initialized\n");
	bt_conn_cb_register(&connection_callbacks);
	k_work_init(&adv_work, adv_work_handler);
	advertising_start();

	k_sleep(K_SECONDS(20)); // Run for 1200 seconds 20 minutes
	// RTC_Start_CountDown(i2c_dev);
	k_thread_abort(sensor_thread_id);
	/* The thread owned the sensors; now that it is gone, make sure they are
	 * idle in case the load switch does not actually cut power. */
	sensors_power_down();

	if (alarm_status == 1){
		RTC_alarm_init(i2c_dev,1);
	}
	else {
		RTC_recover_int(i2c_dev);
	}
	RTC_debug_dump(i2c_dev, "after alarm branch");
	return 0;
	/*
	 * The RTC's alarm output isn't wired to this MCU - it drives a load
	 * switch that cuts power to this whole board. There is therefore no
	 * wake source for this SoC to arm: once the switch re-applies power,
	 * the SoC just goes through a normal power-on reset and main() runs
	 * again from scratch. All that's left to do here is stop spending
	 * power for however long remains until the RTC's alarm actually
	 * fires and the switch cuts power, so disable BLE and drop into the
	 * lowest-current System OFF state.
	 */
	//bt_disable();
	//sys_poweroff();
}
