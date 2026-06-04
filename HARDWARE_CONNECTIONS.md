# SAGE-Vision — Hardware Connections

This document covers every physical wire that needs to be made for the project. Work through each section in order. No soldering is required — all connections use jumper wires on the RV-IoT Board's screw terminals and headers.

---

## Overview of What Connects Where

```
[HC-SR501 PIR]  ──── GPIO 25 ──────────────────┐
[HC-SR04 TRIG]  ──── GPIO 26 ──────────────────┤
[HC-SR04 ECHO]  ──── voltage divider ── GPIO 27 ┤  RV-IoT Board (ESP32)
[LDR]           ──── GPIO 39 (pre-wired)────────┤
                                                 │
                         USB Micro-B ────────────┘
                              │
                         USB-A port
                              │
                    Raspberry Pi 4B
                              │
                         USB-A port
                              │
                    USB Web Camera (UVC)
```

The RV-IoT Board communicates with the Raspberry Pi purely over USB serial (UART bridged via the on-board CP2102 or CH340 USB-to-serial chip). There is no direct GPIO wiring between the Pi and the ESP32.

---

## Section 1: LDR (Photoresistor) — Pre-Wired On-Board

The RV-IoT Board has an LDR permanently soldered to the PCB and connected to **GPIO 39** via an internal voltage divider. No wiring is required for this sensor.

| What | Detail |
|---|---|
| Sensor type | LDR (Light Dependent Resistor / Photoresistor) |
| GPIO pin | 39 (input-only ADC pin) |
| Connection | Pre-wired on-board — nothing to do |
| Output range | 0–4095 raw ADC, mapped in firmware to 0–1023 (0 = dark, 1023 = bright) |
| Dark threshold used in code | 350 (out of 1023) |

**Important:** GPIO 39 is an input-only pin on the ESP32 with no internal pull-up or pull-down. The on-board voltage divider circuit handles biasing. Do not try to use GPIO 39 for any other purpose.

---

## Section 2: HC-SR501 PIR Motion Sensor

The HC-SR501 is a passive infrared sensor that outputs a digital HIGH (3.3V) when it detects movement within its field of view.

### Wiring Table

| HC-SR501 Pin | Connects to | RV-IoT Board Location |
|---|---|---|
| VCC | 5V supply | 5V header or VIN terminal |
| OUT | Signal input | GPIO 25 header pin |
| GND | Ground | GND header or GND terminal |

> **Voltage note:** The HC-SR501 requires a 5V supply on its VCC pin to operate the pyroelectric sensor and internal amplifier, but its OUTPUT signal is already 3.3V-compatible. It is safe to connect the OUT pin directly to GPIO 25 on the ESP32 (which is a 3.3V logic input) without a voltage divider.

### Physical Placement

- Mount the PIR sensor facing the area you want to monitor (the camera's field of view).
- The HC-SR501 has a detection cone of approximately 120° horizontal and 7-metre range.
- The two orange potentiometers on the sensor board adjust **sensitivity** (left) and **hold time** (right). For this project, set sensitivity to mid-point and hold time to minimum (fully anti-clockwise).
- Allow the sensor **30–60 seconds** to stabilise after power-on before the first test run — it outputs false triggers during warm-up.

---

## Section 3: HC-SR04 Ultrasonic Distance Sensor

The HC-SR04 operates on 5V and its ECHO output pin swings to 5V, which **will damage the ESP32's GPIO pin** if connected directly. A resistor voltage divider is required on the ECHO line to step it down to a safe 3.3V level.

### Voltage Divider Circuit (ECHO line only)

The divider uses a 1 kΩ resistor and a 2 kΩ resistor:

```
HC-SR04 ECHO pin (5V)
        │
       [1kΩ]
        │
        ├──────────► GPIO 27 (ESP32 input, 3.3V)
        │
       [2kΩ]
        │
       GND
```

**Voltage calculation:** V_out = 5V × (2000 / (1000 + 2000)) = 3.33V ✓

Both resistors should be carbon film type (standard tolerance is fine). Wire them in series between the HC-SR04 ECHO pin and GND, with the mid-point (junction between the two resistors) connected to GPIO 27.

### Wiring Table

| HC-SR04 Pin | Connects to | Notes |
|---|---|---|
| VCC | 5V supply | Same 5V rail as the PIR |
| TRIG | GPIO 26 | Direct connection — TRIG is an input to the sensor, safe at 3.3V |
| ECHO | 1 kΩ resistor → GPIO 27 | Via voltage divider — **never connect ECHO directly to ESP32** |
| GND | Ground | Same GND rail |

### How the TRIG/ECHO Cycle Works (for reference)

The firmware sends a 10 µs HIGH pulse on GPIO 26 (TRIG) to initiate a measurement. The HC-SR04 then transmits an 8-burst 40 kHz ultrasonic pulse and holds ECHO HIGH for the duration of the return journey. The firmware measures this pulse width via an interrupt on GPIO 27 and calculates distance as:

```
distance_cm = (pulse_duration_µs × 0.0343) / 2
```

Values above 500 cm or below 0 are clamped to -1.0 (out of range).

---

## Section 4: ESP32 RV-IoT Board → Raspberry Pi 4B

The entire ESP32-to-Pi link is a single USB cable. The board's on-board USB-to-serial chip (CP2102 or CH340) creates a virtual serial port on the Pi.

| From | To | Cable |
|---|---|---|
| RV-IoT Board — Micro-USB port | Raspberry Pi 4B — any USB-A port | Micro-USB data cable (must support data transfer, not charge-only) |

On the Pi, this appears as `/dev/ttyUSB0` (CP2102) or `/dev/ttyACM0` (CH340 / CDC-ACM). Verify with:
```bash
dmesg | grep tty
```

---

## Section 5: USB Web Camera → Raspberry Pi 4B

| From | To | Cable |
|---|---|---|
| USB Web Camera | Raspberry Pi 4B — any USB-A port | Camera's own USB cable |

Any UVC-compliant camera works without driver installation. Plug it in and it appears as `/dev/video0`. Verify with:
```bash
ls /dev/video*
```

---

## Complete Connection Summary

### RV-IoT Board (ESP32) Pin Assignments

| GPIO | Function | Connected to | Notes |
|---|---|---|---|
| GPIO 25 | PIR signal input | HC-SR501 OUT | Direct connection, 3.3V safe |
| GPIO 26 | Ultrasonic trigger output | HC-SR04 TRIG | Direct connection |
| GPIO 27 | Ultrasonic echo input | HC-SR04 ECHO (via divider) | Must use 1kΩ/2kΩ voltage divider |
| GPIO 39 | LDR analog input | On-board LDR | Pre-wired, no action needed |

### Power Rails

| Rail | Powers |
|---|---|
| 5V (VIN or VCC terminal) | HC-SR501 VCC, HC-SR04 VCC |
| 3.3V (3V3 terminal) | Do **not** use for HC-SR501 or HC-SR04 |
| GND | HC-SR501 GND, HC-SR04 GND, divider bottom leg |

> Both the PIR and the Ultrasonic sensor require 5V. The RV-IoT Board's VIN pin passes through the USB 5V supply from the Pi directly. Do not use the 3.3V rail for either sensor — the HC-SR501 will malfunction and the HC-SR04 will produce unreliable distance readings below 3.8V supply.

### USB Connections on Raspberry Pi 4B

| Pi USB Port | Connected Device |
|---|---|
| Any USB-A | RV-IoT Board (ESP32) — Micro-USB cable |
| Any USB-A | USB Web Camera |

The Pi has four USB ports (two USB 2.0, two USB 3.0). It does not matter which ports you use; both devices are USB 2.0 compatible and will run at full speed on any port.

---

## Pre-Power Checklist

Before powering the system on, verify:

- [ ] HC-SR501 VCC connected to 5V (not 3.3V)
- [ ] HC-SR501 OUT connected to GPIO 25 (direct)
- [ ] HC-SR04 VCC connected to 5V
- [ ] HC-SR04 TRIG connected to GPIO 26 (direct)
- [ ] HC-SR04 ECHO connected to GPIO 27 **via the 1kΩ/2kΩ voltage divider**
- [ ] Voltage divider GND leg connected to GND
- [ ] All GND connections share the same ground rail
- [ ] RV-IoT Board connected to Pi via data-capable Micro-USB cable
- [ ] USB camera plugged into a separate Pi USB port
- [ ] No bare wire ends that could short against the PCB
