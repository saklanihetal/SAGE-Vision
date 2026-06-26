# SAGE-Vision — Hardware Connections

This document covers every physical wire that needs to be made for the project. Work through each section in order. The four sensor sections need **no soldering** — they use jumper wires onto the Raspberry Pi's 40-pin GPIO header. The **only** soldering in the build is the *optional* INA219 power-telemetry rig in Section 4 (two header strips, two CC resistors, and a couple of wires onto the USB-C male breakout); skip Section 4 entirely if you don't need power logging.

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

## Section 4: INA219 Power Monitor (Optional — Power Telemetry)

> **This is the only soldering in the build.** It is **optional** — the system runs fine without it and the telemetry `power_w` field reads `-- W` until it is wired. It exists to log whole-Pi power draw for the energy comparison between the adaptive node and the baseline.

The INA219 is an I²C current/voltage/power sensor that measures current as the drop across an external **0.1 Ω shunt** (already on-board on the GY-219 / Adafruit modules). We measure **whole-Pi power** by sitting it **high-side, inline on the Pi's incoming 5 V USB-C feed**, so it sees the Pi's total draw.

### Why two USB-C breakout boards (instead of an INA260 / cutting a cable)

To get inline on the 5 V feed without slicing a USB-C cable, the supply is broken out at both ends and `VBUS` is routed *through* the INA219 shunt between them (`GND` passes straight through):

- **Female USB-C breakout** (receptacle) — the wall charger / Pi PSU cable plugs **into** this. It is the **sink** side, exposes `VBUS, GND, CC1, CC2, D+, D-` on a 0.1″ header, and is mostly plug-in once its header strip is soldered on.
- **Male USB-C breakout** (plug) — plugs **into the Pi's USB-C power port**. It presents four **flat SMD pads** labelled `V+, D−, D+, G` (**no holes, no header**), so you solder wires directly onto `V+` and `G`. It carries **no CC pins** — which is exactly why the Rd resistors must live on the female board (see the CC note).

This is non-destructive and reversible, and it replaces the earlier (stubbed) INA260-on-a-bench-supply plan.

### Topology

```
  5 V USB-C charger / official Pi PSU
            │  (USB-C cable)
            ▼
 ┌──────────────────────────┐
 │  FEMALE USB-C breakout    │   ← charger plugs in here (the "sink")
 │  CC1 ──[5.1 kΩ]── GND     │   ← Rd resistors YOU add (see CC note)
 │  CC2 ──[5.1 kΩ]── GND     │
 │  D+ / D-  : unconnected   │
 └────┬─────────────────┬────┘
   VBUS │            GND │
        ▼                │
   ┌─────────────┐       │
   │   INA219    │       │
   │  VIN+ ◄─────┘ (VBUS in from female board)
   │   [ 0.1 Ω shunt ]   │
   │  VIN- ──────┐       │
   │  VCC SDA SCL│ GND   │  ← logic side → Pi 40-pin header (below)
   └─────────────┘       │
        │ (to male V+)   │
        ▼                ▼
 ┌──────────────────────────┐
 │  MALE USB-C breakout      │   V+ ◄ from INA219 VIN-
 │  (flat pads V+ D- D+ G)   │   G  ◄ common ground (from female GND)
 │  D- / D+  : unconnected   │
 └────────────┬─────────────┘
              │  (plugs into the Pi's USB-C power input)
              ▼
        Raspberry Pi 4B
```

### Wiring Tables

**High-current path (the 5 V feed — `VBUS` passes through the shunt):**

| From | To | Notes |
|---|---|---|
| Female breakout `VBUS` | INA219 `VIN+` | full Pi current — see the wire note below |
| INA219 `VIN-` | Male breakout `V+` | full Pi current |
| Female breakout `GND` | Male breakout `G` | common ground, straight through |
| Female breakout `GND` | INA219 `GND` (logic) | shared with the Pi GND below |

**INA219 logic side → Pi 40-pin header:**

| INA219 Pin | Connects to | Pi Header |
|---|---|---|
| VCC | 3.3 V (logic) | pin 1 |
| GND | Ground | pin 6 (same ground as the feed above) |
| SDA | I²C data | GPIO 2 (pin 3) |
| SCL | I²C clock | GPIO 3 (pin 5) |

> **⚠ Polarity:** swapping `V+` and `GND` anywhere on the 5 V path feeds reverse voltage straight into the Pi and will destroy it. Verify with a multimeter before the Pi is ever connected.

### Wire note — *using standard jumper wire on the 5 V path*

The `VBUS` / `V+` / `GND` connections carry the Pi's whole current. The Pi 4B draws only ~1–1.5 A in normal operation (camera + inference) — well within standard dupont jumper wire — but it can spike toward **~3 A on boot/heavy load**, where thin jumper wire and friction-fit crimps get marginal. Three mitigations (no thick wire needed):

1. Keep the high-current jumpers **as short as possible**.
2. **Double them up** — run two strands in parallel for each of `VBUS`, `V+`, and `GND`.
3. On the male board, **snip the dupont connector off and solder the bare strands directly** to the `V+`/`G` pads — the crimp contact, not the wire, is the weak point at peak current.

After power-up, check `vcgencmd get_throttled` (or watch for the on-screen lightning-bolt): a non-zero under-voltage bit means the path is dropping too much — shorten or double the wires.

> Wire resistance *before* the shunt does **not** corrupt the current reading (the shunt measures current regardless), and bus voltage is sensed on the load side at `VIN-`, so accuracy holds; the concern above is purely thermal / brown-out.

### CC note — *why the Rd resistors are mandatory*

A compliant USB-C source (the official Pi PSU included) will **not** turn on `VBUS` until it detects a sink, and it detects one by seeing an **Rd = 5.1 kΩ pull-down to GND on the CC line(s)**. Normally the powered device presents Rd — but our **male breakout doesn't pass the CC pins through to the Pi**, so the Pi's own Rd never reaches the charger. Without help the charger sees an open CC, assumes nothing is plugged in, and delivers **0 V**.

Fix: present Rd ourselves on the **female** (sink) board — **one 5.1 kΩ resistor from CC1 → GND and one from CC2 → GND**. Because the female board has a 0.1″ header you can do this without soldering the board itself (bridge each resistor between a CC and a GND header pin); soldering it on permanently is cleaner if you prefer. (Both CC lines get an Rd; a real cable connects only one CC through, so the source sees a single Rd — standard sink behaviour, *not* a debug-accessory.) `D+`/`D-` stay unconnected on both boards — this is a power-only tap; the Pi's USB-C port carries no data.

### Soldering — materials

- Fine-tip soldering iron (~350 °C), 60/40 or lead-free solder, and flux
- Standard jumper wire (snip the connector off where you solder to a pad)
- Two **5.1 kΩ, ¼ W** resistors (the Rd pull-downs)
- The header strips that came loose with the INA219 and the female breakout
- Wire strippers, a vise / helping-hands, heat-shrink (and optionally hot glue for strain relief)
- A multimeter (continuity + voltage) — **not optional** for the pre-power checks

### Soldering — INA219 module (header strip for logic; screw terminals for the shunt)

The module's **logic side** (`VCC, GND, SCL, SDA`) ships with its header strip loose (that's the "pins not held in the holes" wobble). Push the strip's **short legs** down through the holes from the top so the long pins stick up for jumpers, hold it flush, flip the board, and solder each pin on the back — one joint per pin. Then jumper those four to the Pi header (tiny current — dupont is fine).

The **shunt side** (`VIN+`, `VIN−`) is a **screw terminal block** — *use it, not the pins.* It's the solid, low-resistance, mechanically locked connection the full-current 5 V feed wants, and it keeps any friction-fit crimp out of the highest-current path. Strip ~6–7 mm of wire, insert, tighten the screw, and tug-test:
- `VIN+` ← wire from the female breakout `VBUS`
- `VIN−` → wire to the male breakout `V+`

(Don't connect both the screw terminal and the header pin for a given VIN — same node, just use the screw.)

### Soldering — FEMALE breakout (header strip + Rd)

1. If its header strip is loose, solder it in the same way (short legs through, solder on the back).
2. **Rd resistors:** connect a 5.1 kΩ from `CC1` → `GND` and another from `CC2` → `GND` (at the header per the CC note, or soldered on). Insulate the bare leads so they can't touch `VBUS`.
3. Bring `VBUS` and `GND` out to the INA219 / male board (see the wire note — short, doubled). `D+`/`D-` stay unconnected.
4. **Verify (multimeter):** `CC1`→`GND` ≈ 5.1 kΩ, `CC2`→`GND` ≈ 5.1 kΩ, `VBUS`→`GND` **open** (no short).

### Soldering — MALE breakout (flat pads — direct wire)

The four pads (`V+, D−, D+, G`) are flat SMD pads with no holes, so header pins can't seat — solder wire straight to the pads. You only need `V+` and `G`.

1. Clamp the board in a vise, pads up.
2. **Pre-tin the `V+` and `G` pads:** touch the iron + a little solder so each pad wears a thin shiny dome.
3. **Pre-tin the wire:** strip ~5 mm of jumper wire (connector snipped off), twist the strands, tin them.
4. **`V+` joint:** lay the tinned wire flat on the tinned `V+` pad, press the iron down until both pools merge, remove the iron, and **hold the wire dead still ~2 s** while it solidifies. This wire runs to the INA219 `VIN−`.
5. **`G` joint:** repeat for the `G` pad → common ground (female `GND` / Pi `GND`).
6. Leave `D−` and `D+` unconnected; don't touch the fine pin-comb beside the pads (that's the connector's own contacts).
7. **Strain-relief** each joint with heat-shrink, and optionally anchor the wires to the board edge with hot glue so flexing never stresses the pads.
8. **Inspect:** a good joint is shiny with a concave fillet, not a dull grey ball — re-flow anything blobby.
9. **Verify (multimeter):** `V+`→`G` reads **open** (you didn't bridge them). Dry-fit the plug into the Pi's USB-C port to confirm it seats before any power.

### Bring-up sequence (in order)

1. Enable I²C: `sudo raspi-config` → *Interface Options* → *I2C* → enable, then reboot.
2. With the **Pi still disconnected** (male plug out), plug the charger into the female board and measure `VBUS`→`GND` on the female board — it should read **~5 V**. If it reads ~0 V, the Rd resistors are missing/wrong; fix before continuing.
3. Power off. Connect the male plug into the Pi's USB-C port and boot.
4. Confirm the chip is on the bus: `sudo i2cdetect -y 1` shows the INA219 at **`0x40`** (default address).
5. `read_power_w()` in `pi_edge_node.py` reads it via the `pi-ina219` library (see `rpi_edge/requirements.txt`) and fills the `power_w` telemetry column.

### Ratings & headroom

The 0.1 Ω shunt at the INA219's 320 mV gain tops out at **3.2 A**, and the 7Semi female breakout is rated ~1.5 A nominal / 3 A peak. A Pi 4B with the camera sits well under that normally but can spike toward ~3 A on boot — **thin headroom**. Don't hang power-hungry USB peripherals off the Pi while measuring, and keep the high-current wires short (see the wire note).

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
| GPIO 2 | 3 | I²C data (SDA) | INA219 SDA | Optional power monitor |
| GPIO 3 | 5 | I²C clock (SCL) | INA219 SCL | Optional power monitor |

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
- [ ] *(Optional)* INA219 VCC→3.3V, GND→GND, SDA→GPIO 2, SCL→GPIO 3; VIN+ from female `VBUS`, VIN− to male `V+`; 5.1 kΩ Rd on CC1/CC2; verified ~5 V at the female board before connecting the Pi
- [ ] No bare wire ends that could short against the header or the board

---

## Legacy: ESP32 RV-IoT Board Path (no longer used)

Earlier revisions of this project read the sensors on an ESP32 RV-IoT Board and streamed a 7-byte packed binary struct to the Pi over USB serial. That firmware (`firmware/esp32_sensor_node/esp32_sensor_node.ino`) is **kept for reference only** and is not part of the current pipeline. If you ever revert to it, the original ESP32 pin map was: PIR → GPIO 25, HC-SR04 TRIG → GPIO 26, HC-SR04 ECHO → GPIO 27 (via divider), on-board analog LDR → GPIO 39, with the board linked to the Pi by a single data-capable Micro-USB cable.
