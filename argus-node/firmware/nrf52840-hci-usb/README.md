# nRF52840 dongle firmware for ARGUS nodes

Zephyr `hci_usb` controller firmware for the Nordic nRF52840 USB dongle (PCA10059),
built with **extended advertising** so the node can scan Bluetooth 5 Long Range
(Coded PHY) Remote ID as well as Bluetooth 4.

The common prebuilt `hci_usb.zip` reports Coded PHY but rejects the extended scan
commands (LE Set Extended Scan Parameters returns 0x01, Unknown HCI Command), so
ARGUS falls back to BT4-only scanning with it. This build fixes that.

| File | Purpose |
| --- | --- |
| `argus-hci-usb-ext.hex` | Ready-to-flash image (loads at 0x1000, above the dongle's bootloader) |
| `argus-rid.conf` | Kconfig overlay applied on top of the stock `hci_usb` sample |

SHA-256 of the hex: `74c363cc8adb01f62cdaa79ded5b4466c4a0bdea733b8bd048f5d3a4343b9298`

## Flash it (Windows, nrfutil with nrf5sdk-tools)

1. Package the hex for the dongle's bootloader:

   ```
   nrfutil nrf5sdk-tools pkg generate --hw-version 52 --sd-req=0x00 --application argus-hci-usb-ext.hex --application-version 1 argus-hci-usb-ext.zip
   ```

2. Put the dongle in bootloader mode: press the small side RESET button while it is
   plugged in (the red LED pulses).
3. Flash, replacing COM6 with the dongle's port:

   ```
   nrfutil nrf5sdk-tools dfu usb-serial -pkg argus-hci-usb-ext.zip -p COM6
   ```

4. Plug it back into the node and restart the agent. The log should show
   `hci1: extended scanning, 1M + Coded PHY (BT4 + BT5 Long Range)`.

## Rebuild it

Run from this folder. Built from Zephyr `v3.7-branch` (LTS) at commit `c3ef6f38`, hal_nordic `ab5cb2e`,
tinycrypt `1012a3e`, with the GNU Arm Embedded toolchain:

```bash
west init -m https://github.com/zephyrproject-rtos/zephyr --mr v3.7-branch zp && cd zp
west config manifest.project-filter -- '-.*,+cmsis,+hal_nordic,+tinycrypt'
west update --narrow -o=--depth=1
pip install -r zephyr/scripts/requirements-base.txt
export ZEPHYR_TOOLCHAIN_VARIANT=gnuarmemb GNUARMEMB_TOOLCHAIN_PATH=/usr   # or use the Zephyr SDK
west build -p always -b nrf52840dongle/nrf52840 zephyr/samples/bluetooth/hci_usb \
    -- -DEXTRA_CONF_FILE=$PWD/../argus-rid.conf
# output: build/zephyr/zephyr.hex
```

Firmware is Zephyr (Apache-2.0); see https://github.com/zephyrproject-rtos/zephyr.
