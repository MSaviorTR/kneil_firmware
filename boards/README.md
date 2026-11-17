# Board: es4l15ba1 (KAGAFEI module with nRF54L15)

Custom Zephyr board definition (hardware model v2) for the module used in the KNEIL/NeiL prototype.
Board target: `es4l15ba1/nrf54l15/cpuapp` (also `cpuflpr` and `cpuflpr/xip` for the RISC-V core, not used).
Build with `-DBOARD_ROOT=<repository root>` (see [../docs/BUILD.md](../docs/BUILD.md)).

| Item | Value |
|---|---|
| SoC | nRF54L15, Cortex-M33 at 128 MHz, 1.4 MB RRAM, 188 KB RAM (application core) |
| I2C | `i2c21` (TWIM21): SDA P1.07, SCL P1.06, internal pull-ups; 100 kHz (`es4l15ba1_nrf54l15_cpuapp.overlay`) |
| Accelerometer | Bosch BMA580 at I2C 0x18 (CHIP_ID 0xC4); its INT1 pin is not connected to the SoC |
| Other I2C devices | TMP118 temperature sensor, external RTC (its alarm drives a load switch that cuts the board's power) |
| Low-frequency clock | no 32.768 kHz crystal: internal RC oscillator (`CONFIG_CLOCK_CONTROL_NRF_K32SRC_RC`) |
| Power | DC/DC regulator enabled in the devicetree |
| Console | none (no UART/USB); RTT through the J-Link SWD connection for debugging |
| Programming | SWD with a J-Link (here: the debug-out of an nRF52 DK, PCA10040); `west flash` uses nrfutil |

Files: `KAGAFEI/es4l15ba1/` (board.yml, devicetree, pinctrl, Kconfig, defconfigs) and the application overlay
`es4l15ba1_nrf54l15_cpuapp.overlay` in this folder.
