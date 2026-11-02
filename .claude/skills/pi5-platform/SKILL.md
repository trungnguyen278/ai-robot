---
name: pi5-platform
description: Raspberry Pi 5 (8 GB) platform knowledge and checklists for this robot — 40-pin header functions (I2C, SPI, UART, I2S, PWM), 3.3 V logic, power (5 V/5 A, USB current limit, powering through the header, halt power), thermal throttling, Wi-Fi/BLE, CSI/DSI/PCIe, the OS (Ubuntu 26.04 with ROS 2, ADR-0017), config.txt/EEPROM settings and GPIO libraries. Use whenever a task touches the Pi side of the hardware or OS — wiring something to the Pi, assigning GPIO pins, enabling interfaces or overlays, powering or cooling the Pi, choosing GPIO, camera or audio libraries, or deciding what runs on the Pi versus the ESP32.
---

# Raspberry Pi 5 platform

## Read first

1. `hardware/components/raspberry-pi-5.md` — researched specifications with sources.
2. `hardware/pinmap.md` — which pin is used for what.
3. `hardware/README.md` — power tree and open issues.

## Checklist: assigning pins or enabling interfaces

- [ ] GPIO is **3.3 V and not 5 V tolerant** — 5 V devices need a level shifter.
- [ ] Check `pinmap.md` for conflicts. Common ones:
  - The header exposes **a single I2S pin group (GPIO18–21)** — two I2S devices (e.g. an audio HAT plus a separate amplifier) will collide.
  - GPIO18/19 are both I2S and hardware PWM.
  - I2C1 (GPIO2/3) is a shared bus — record every I2C address in `pinmap.md`.
- [ ] Devices powered from the header's 5 V or 3.3 V pins go into the power budget in `hardware/README.md`.
- [ ] Update `pinmap.md` (and any `config.txt` sample) in the same commit.

## Checklist: power and thermals

- [ ] The Pi 5 needs a stable 5 V supply of up to 5 A. Powered through the header's 5 V pins (from the DC-DC converter) there is no USB-PD negotiation, so total USB current is capped at about 600 mA unless `usb_max_current_enable=1` (`config.txt`) or `PSU_MAX_CURRENT=5000` (EEPROM) is set. Only do that with a supply that really delivers 5 A.
- [ ] Never power servos or motors from the Pi's 5 V rail.
- [ ] On battery, consider `POWER_OFF_ON_HALT=1` (EEPROM) to cut halt power from about 1.2–1.6 W to about 0.01 W.
- [ ] Sustained AI or camera load needs the Active Cooler; the Pi starts throttling around 80 °C.

## Common software pitfalls

- `RPi.GPIO` **does not work on the Pi 5** (GPIO lives on the RP1 chip). Use `gpiozero`, `lgpio` or `libgpiod`; `rpi-lgpio` runs legacy RPi.GPIO code.
- Do not hard-code `gpiochip4` or `gpiochip0`: the number changed between kernels. Find the chip with `gpiodetect`/`gpioinfo` or by label.
- There is no 3.5 mm audio jack and no ADC. Audio goes through I2S, USB or HDMI; analog measurements such as battery voltage go through the ESP32-S3 or an ADC/INA chip.
- The robot runs Ubuntu 26.04 rather than Raspberry Pi OS (ADR-0017). Raspberry Pi's guides assume Raspberry Pi OS, so check each step on Ubuntu:
  - `config.txt` lives in `/boot/firmware/`, as on Raspberry Pi OS. Check that each overlay the robot needs (TPM, display) ships with Ubuntu's kernel;
  - the bootloader EEPROM must be dated 2025-02-11 or later;
  - Picamera2 and the IMX500's firmware are not in Ubuntu's archive (`hardware/components/raspberry-pi-ai-camera.md`);
  - Ubuntu's image puts the serial console on UART0 (GPIO 14/15, `ttyAMA0`), not on the debug connector: move it before wiring UART0;
  - Ubuntu loads the Wi-Fi driver without the options Raspberry Pi OS ships for stability (`options brcmfmac roamoff=1 feature_disable=0x282000`): add them in `/etc/modprobe.d/`;
  - the Chromium snap cannot use the Pi 5's GPU: snapd's `opengl` interface does not cover its PCIe path (`docs/devlog/2026-11.md`).

## When the reference is missing something

Check official sources first (raspberrypi.com/documentation, the product brief, the RP1 and BCM2712 datasheets), then **add the fact** to `hardware/components/raspberry-pi-5.md` with its link and the retrieval date. Mark secondary sources as such.
