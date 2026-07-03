# SAGE-Vision — Hardware Connections

This document covers every physical wire that needs to be made for the project. Work through each section in order. **No soldering is required** — the sensors use jumper wires onto the Raspberry Pi's 40-pin GPIO header, and power is measured with a plug-in inline USB-C meter (Section 4) that needs no wiring at all.

> **Architecture note:** The sensors now wire **directly to the Raspberry Pi 4B GPIO header**. The Pi reads the PIR, the LM393 light comparator, and the HC-SR04 ultrasonic sensor itself (via the `pigpio` daemon), so there is no longer an ESP32 microcontroller in the live data path. The original ESP32 firmware is retained under `firmware/` as **legacy** only — see the note at the end of this document.

---

## Overview of What Connects Where

```
[HC-SR501 PIR]   OUT ──────────────────────── GPIO 17  (pin 11)  ┐
[LM393 light]    DO  ──────────────────────── GPIO 27  (pin 13)  │
[HC-SR04 TRIG]   ──────────────────────────── GPIO 23  (pin 16)  │  Raspberry Pi 4B
[HC-SR04 ECHO]   ── voltage divider ────────── GPIO 24  (pin 18)  │   40-pin header
                                                                  │
                              USB-A port ──────────────────────── ┘
                                   │
                          USB Web Camera (UVC)
```

All three sensors share the Pi's 5V / 3.3V / GND rails on the header. The HC-SR04's 5V ECHO line is the **only** signal that needs a voltage divider; every other signal pin is already 3.3 V-safe (see each section).

---

## Section 1: LM393 Light Comparator (replaces the analog LDR)

The light sensor is now an **LM393 dual-comparator module** that outputs a clean digital signal — HIGH or LOW — instead of an analog voltage. The module's onboard potentiometer sets the dark/bright threshold, and the LM393 provides its own comparator hysteresis, so **all light-gating logic lives in hardware**; the Pi simply reads one digital pin.

### Wiring Table

| LM393 Pin | Connects to | Pi Header |
|---|---|---|
| VCC | **3.3V** | pin 1 or pin 17 |
| GND | Ground | any GND pin |
| DO (digital out) | Signal input | **GPIO 27** (pin 13) |
| AO (analog out) | *not used* | leave unconnected |

> **Voltage note:** Power the LM393 from the Pi's **3.3V** rail (not 5V). The module's DO output level follows its supply voltage, so a 3.3V supply keeps DO directly safe for the Pi's GPIO with **no divider needed**. (If your specific module requires 5V, you must add a level shifter or divider on DO — but 3.3V works for standard LM393 light modules.)

### Polarity

On this build the module outputs `DO = HIGH` when the room is **dark** and `DO = LOW` when bright, so the code reads `is_dark = (gpio_read == LDR_DARK_LEVEL)` with `LDR_DARK_LEVEL = 1`. LM393 light modules vary by board and pot wiring; if yours is inverted (`DO = LOW` when dark), set `LDR_DARK_LEVEL = 0` (or adjust the potentiometer).

### Calibration

Cover the sensor (simulate darkness) and turn the potentiometer until the module's onboard LED just switches — that point is your threshold. Set it so that normal room lighting reads "bright" and the lighting condition you want CLAHE to kick in at reads "dark."

---

## Section 2: HC-SR501 PIR Motion Sensor

The HC-SR501 is a passive infrared sensor that outputs a digital HIGH (3.3V) when it detects movement within its field of view.

### Wiring Table

| HC-SR501 Pin | Connects to | Pi Header |
|---|---|---|
| VCC | 5V supply | pin 2 or pin 4 |
| OUT | Signal input | **GPIO 17** (pin 11) |
| GND | Ground | any GND pin |

> **Voltage note:** The HC-SR501 requires a 5V supply on VCC to operate its pyroelectric sensor and internal amplifier, but its OUTPUT signal is already 3.3V-compatible. It is safe to connect OUT directly to GPIO 17 (a 3.3V logic input) without a voltage divider.

### Physical Placement

- Mount the PIR facing the area you want to monitor (the camera's field of view).
- The HC-SR501 has a detection cone of approximately 120° horizontal and ~7-metre range.
- The two orange potentiometers adjust **sensitivity** (left) and **hold time** (right). Set sensitivity to mid-point and hold time to minimum (fully anti-clockwise).
- Allow the sensor **30–60 seconds** to stabilise after power-on before the first test run — it outputs false triggers during warm-up.

---

## Section 3: HC-SR04 Ultrasonic Distance Sensor

The HC-SR04 operates on 5V and its ECHO output pin swings to 5V, which **will damage the Pi's GPIO pin** if connected directly. A resistor voltage divider is required on the ECHO line to step it down to a safe 3.3V level.

### Voltage Divider Circuit (ECHO line only)

The divider uses a 1 kΩ resistor and a 2 kΩ resistor:

```
HC-SR04 ECHO pin (5V)
        │
       [1kΩ]
        │
        ├──────────► GPIO 24 (Pi input, 3.3V)
        │
       [2kΩ]
        │
       GND
```

**Voltage calculation:** V_out = 5V × (2000 / (1000 + 2000)) = 3.33V ✓

Both resistors should be carbon film type (standard tolerance is fine). Wire them in series between the HC-SR04 ECHO pin and GND, with the mid-point (junction between the two resistors) connected to GPIO 24.

### Wiring Table

| HC-SR04 Pin | Connects to | Pi Header / Notes |
|---|---|---|
| VCC | 5V supply | pin 2 or pin 4 — same 5V rail as the PIR |
| TRIG | GPIO 23 (pin 16) | Direct connection — TRIG is an input to the sensor, safe at 3.3V |
| ECHO | 1 kΩ resistor → GPIO 24 (pin 18) | Via voltage divider — **never connect ECHO directly to the Pi** |
| GND | Ground | Same GND rail |

### How the TRIG/ECHO Cycle Works (for reference)

The Pi's `pigpio`-driven harvester thread sends a 10 µs HIGH pulse on GPIO 23 (TRIG) every ~60 ms to initiate a measurement. The HC-SR04 transmits an 8-burst 40 kHz ultrasonic pulse and holds ECHO HIGH for the duration of the return journey. A `pigpio` edge callback (hardware-timestamped, the equivalent of the old firmware ISR) measures this pulse width on GPIO 24 and computes distance as:

```
distance_cm = (pulse_duration_µs × 0.0343) / 2
```

Values above 500 cm or below 0 are clamped to -1.0 (out of range). A *completed* echo — even an out-of-range one — counts as a healthy reading; only the total absence of an echo pulse is treated as a sensor failure by the watchdog.

---

## Section 4: Power Measurement (Optional — Inline USB-C Meter)

> **No wiring and no soldering.** Power measurement is **optional** — the node runs identically without it. It exists only to log whole-Pi power draw for the energy comparison between the adaptive node and the baseline.

Whole-Pi power is measured with a **standalone inline USB-C power meter** — the kind that has a USB-C plug on one end, a USB-C socket on the other, and a small display showing live **volts / amps / watts**. It sits **inline on the Pi's incoming 5 V USB-C feed**, so it reads the Pi's total draw.

### Placement

```
  5 V USB-C charger / official Pi PSU
            │  (USB-C cable)
            ▼
 ┌──────────────────────────┐
 │  Inline USB-C power meter │   ← reads V / A / W on its own display
 └────────────┬─────────────┘
              │  (plugs into the Pi's USB-C power input)
              ▼
        Raspberry Pi 4B
```

1. Unplug the Pi's USB-C power.
2. Plug the meter's **male (plug)** end into the Pi's USB-C power port.
3. Plug the charger's USB-C cable into the meter's **female (socket)** end.
4. Power on — the meter lights up and shows the live draw.

If the meter has a direction/orientation marking, follow it; a good meter is bidirectional and works either way. Some cheaper units add a small series resistance — if the Pi shows an under-voltage warning (`vcgencmd get_throttled` non-zero, or the on-screen lightning-bolt), try a shorter high-quality cable or a beefier charger.

### Logging power

Power is **read by hand off the meter's display** and noted against each run — the node does no power sensing and its telemetry has **no power column**. For a benchmark, watch the meter through the run and record either the steady-state watts per state or an eyeballed average (a meter with a running Wh/average readout makes this easier). Use the **same meter for both** the baseline and adaptive runs so the two share one instrument. See `docs/TESTING.md` for exactly when to read it during the activity schedule.

### Why an off-the-shelf meter (vs the earlier INA219 shunt rig)

An earlier revision measured power with an INA219 shunt sensor tapped onto a pair of USB-C breakout boards over I²C. The inline meter replaces it entirely because it needs **no soldering, no CC-resistor bring-up, no I²C wiring, and no software** — you plug it in and read the number. The tradeoff is that the reading is **manual and off-device** (not timestamped into the telemetry), so power can't be correlated frame-by-frame with FSM state — only summarised per run. For the energy headline (average watts over a run) that is sufficient. See `docs/ENGINEERING_LOG.md` (P7) for the reasoning.

---

## Section 5: Display & USB Web Camera → Raspberry Pi 4B

The demo GUI window opens on a **monitor attached to the Pi's HDMI port** (use the micro-HDMI port nearest the USB-C/power corner on the Pi 4B). A monitor is only needed for the GUI — running the node with `--headless` requires no display.

| From | To | Cable |
|---|---|---|
| HDMI monitor | Raspberry Pi 4B — micro-HDMI (HDMI0) | micro-HDMI → HDMI cable |
| USB Web Camera | Raspberry Pi 4B — any USB-A port | Camera's own USB cable |

Any UVC-compliant camera works without driver installation. Plug it in and it appears as `/dev/video0`. Verify with:
```bash
ls /dev/video*
```

---

## Complete Connection Summary

### Raspberry Pi 4B GPIO Pin Assignments (BCM numbering)

| BCM GPIO | Header pin | Function | Connected to | Notes |
|---|---|---|---|---|
| GPIO 17 | 11 | PIR signal input | HC-SR501 OUT | Direct connection, 3.3V safe |
| GPIO 27 | 13 | Light digital input | LM393 DO | Direct connection (LM393 powered at 3.3V) |
| GPIO 23 | 16 | Ultrasonic trigger output | HC-SR04 TRIG | Direct connection |
| GPIO 24 | 18 | Ultrasonic echo input | HC-SR04 ECHO (via divider) | Must use 1kΩ/2kΩ voltage divider |

> The sensor pins are defined as constants at the top of `gpio_harvester_worker` in `rpi_edge/pi_edge_node.py` (`PIR_PIN`, `LDR_PIN`, `TRIG_PIN`, `ECHO_PIN`). If you wire to different pins, change them there to match.

### Power Rails

| Rail | Powers | Pi Header |
|---|---|---|
| 5V | HC-SR501 VCC, HC-SR04 VCC | pins 2, 4 |
| 3.3V | LM393 VCC | pins 1, 17 |
| GND | All sensor grounds + divider bottom leg | pins 6, 9, 14, 20, 25, 30, 34, 39 |

> The PIR and the Ultrasonic sensor require **5V**; the LM393 light module is powered from **3.3V** so its digital output is GPIO-safe. Do not power the HC-SR501 or HC-SR04 from 3.3V — the PIR will malfunction and the HC-SR04 produces unreliable readings below ~3.8V supply.

### USB / Display Connections on Raspberry Pi 4B

| Pi Port | Connected Device |
|---|---|
| Any USB-A | USB Web Camera |
| micro-HDMI (HDMI0) | Monitor for the demo GUI *(omit if running `--headless`)* |

---

## Pre-Power Checklist

Before powering the system on, verify:

- [ ] HC-SR501 VCC connected to 5V (not 3.3V)
- [ ] HC-SR501 OUT connected to GPIO 17 (direct)
- [ ] LM393 VCC connected to **3.3V** (not 5V)
- [ ] LM393 DO connected to GPIO 27 (direct)
- [ ] HC-SR04 VCC connected to 5V
- [ ] HC-SR04 TRIG connected to GPIO 23 (direct)
- [ ] HC-SR04 ECHO connected to GPIO 24 **via the 1kΩ/2kΩ voltage divider**
- [ ] Voltage divider GND leg connected to GND
- [ ] All GND connections share the same ground rail (sensor grounds + Pi GND)
- [ ] USB camera plugged into a Pi USB port
- [ ] HDMI monitor connected (for the demo GUI; skip if running `--headless`)
- [ ] *(Optional)* Inline USB-C power meter plugged between the charger and the Pi's USB-C port (no GPIO wiring)
- [ ] No bare wire ends that could short against the header or the board

---

## Legacy: ESP32 RV-IoT Board Path (no longer used)

Earlier revisions of this project read the sensors on an ESP32 RV-IoT Board and streamed a 7-byte packed binary struct to the Pi over USB serial. That firmware (`firmware/esp32_sensor_node/esp32_sensor_node.ino`) is **kept for reference only** and is not part of the current pipeline. If you ever revert to it, the original ESP32 pin map was: PIR → GPIO 25, HC-SR04 TRIG → GPIO 26, HC-SR04 ECHO → GPIO 27 (via divider), on-board analog LDR → GPIO 39, with the board linked to the Pi by a single data-capable Micro-USB cable.
