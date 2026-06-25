# SAGE-Vision
### Sensor-Adaptive GPU-less Edge Vision

A Raspberry Pi 4B edge application that uses PIR, light (LDR), and ultrasonic sensors to throttle YOLOv8 inference resolution and frame rate — reducing idle power draw and CPU/SoC thermals, without a GPU and without sacrificing detection quality.

---

## The Problem

Running a continuous, fixed-resolution computer-vision loop on an ARM SoC like the Raspberry Pi 4B is thermally and energetically expensive — it pushes the Broadcom BCM2711 toward its 80 °C throttling boundary within minutes, even though most of the time the scene is empty or static. The usual fixes (a heatsink, an AI-accelerator hat, or a weaker model) either add hardware cost or sacrifice accuracy.

SAGE-Vision makes **compute proportional to scene demand**: cheap sensors gate *when* and *how hard* the YOLO model runs, so the node idles near-free when nothing is happening and scales inference resolution to subject distance when it is. The target is a measurable reduction in **average power draw and core temperature** versus an always-on baseline, with no meaningful loss in detection quality — at zero additional hardware cost beyond sensors already on the bench.

---

## System Overview

<img width="1536" height="950" alt="System Architecture" src="https://github.com/user-attachments/assets/e013ed95-f943-4051-b0da-51b809678421" />

The node runs **fully offline on the Pi alone**. Sensors wire directly to the 40-pin GPIO header (read by the `pigpio` background process — there is no microcontroller in the live path); a USB camera supplies frames; a two-thread core (sensor harvester + adaptive vision/FSM) does the work, with optional background threads for power/telemetry. Inference runs on `tflite-runtime` with INT8 YOLOv8-nano models.

---

## Hardware & Wiring

### Components

| Component | Part | Role |
|---|---|---|
| Compute | Raspberry Pi 4B | edge inference node |
| Camera | USB UVC webcam | video frames |
| Motion | HC-SR501 PIR | wake / presence signal |
| Light | LM393 comparator module | dark/bright gate for CLAHE |
| Distance | HC-SR04 ultrasonic | subject distance → model selection |
| Power measurement | INA219 | whole-Pi power for the energy figure |

### Sensor → GPIO wiring (BCM numbering)

| Sensor | Signal | Pi pin | Notes |
|---|---|---|---|
| HC-SR501 PIR | OUT | GPIO 17 (pin 11) | 5V supply; output already 3.3V-safe |
| LM393 light | DO | GPIO 27 (pin 13) | 3.3V supply; active-low (LOW = dark); hardware hysteresis via onboard pot |
| HC-SR04 | TRIG | GPIO 23 (pin 16) | direct connection |
| HC-SR04 | ECHO | GPIO 24 (pin 18) | **via 1kΩ/2kΩ voltage divider** (steps the 5V echo down to 3.3V) |
| INA219 | SDA / SCL | GPIO 2 / GPIO 3 (pins 3 / 5) | I²C; address `0x40` |

### Power rails

| Rail | Powers | Pi pins |
|---|---|---|
| 5V | HC-SR501, HC-SR04 | 2, 4 |
| 3.3V | LM393, INA219 (logic) | 1, 17 |
| GND | all sensor grounds + divider leg | 6, 9, 14, 20, 25, 30, 34, 39 |

### INA219 power-measurement path

The INA219 sits high-side, inline on the Pi's 5V USB-C feed, broken out with a female + male USB-C breakout pair so no cable is cut.

| Connection | From → To |
|---|---|
| Shunt in | female USB-C `VBUS` → INA219 `VIN+` |
| Shunt out | INA219 `VIN-` → male USB-C `V+` |
| Ground | female `GND` → male `G` (common) |
| CC pull-downs | 5.1kΩ from CC1→GND and CC2→GND on the female board (so the charger enables VBUS) |
| Logic | `VCC`→3.3V, `GND`→GND, `SDA`→GPIO 2, `SCL`→GPIO 3 |

> Full wiring, soldering, and bring-up steps are in [`docs/HARDWARE_CONNECTIONS.md`](docs/HARDWARE_CONNECTIONS.md).

---

## Sensing & Presence Fusion

Each sensor has a distinct role, and the readings are filtered before use:

- **Ultrasonic (primary):** each reading is spike-rejected (a jump beyond `MAX_PLAUSIBLE_JUMP_CM` is dropped unless it persists for several samples) then median-filtered over 5 samples, to absorb multipath bounce off walls/furniture.
- **PIR:** a motion / wake trigger only.
- **LDR (LM393):** the CLAHE low-light gate (digital dark/bright).

**"Presence" is a fusion of three signals (logical OR):** PIR motion, a close ultrasonic reading (`distance < PRESENCE_DISTANCE_CM`, ~350 cm), or a YOLO person-detection (the **vision vote**). Any one signal refreshes presence; absence is declared only when **all three** have been quiet for `PRESENCE_TIMEOUT_S` (~12 s).

The vision vote is essential because **PIR senses motion, not presence** — a motionless person reads identical to an empty room, so without the vision vote the node would wrongly drop to SLEEP on a still occupant.

---

## The 5-State FSM

Inference is controlled through a 5-state finite state machine.

<img width="1619" height="972" alt="5 state FSM" src="https://github.com/user-attachments/assets/f852ccb6-f5ae-4b7d-8592-cd0f8cd3200a" />

1. **SLEEP** — no inference; the loop polls at ~0.5 s watching for a wake signal. Exits to **STANDBY** when the PIR fires or the ultrasonic detects an object within `WAKE_DISTANCE_CM` (~300 cm), held long enough to pass the debounce gate.
2. **STANDBY** — a transitional state entered on waking; no inference. Polls fast (~0.05 s) to clear a brief warm-up (`STANDBY_WARMUP_S`, ~200 ms), then picks the active state by distance.
3. **ACTIVE-LO** — object present and **close** (`distance < HILO_CLOSE_CM`, ~120 cm). Runs the **320×320** model: a close subject is large in frame, so low resolution suffices and is cheap (low power, low latency). Loop polls at ~0.01 s for responsiveness. This is also the **wind-down** state — ACTIVE-HI drops here when presence has been quiet for over ~2 s. Exits to ACTIVE-HI if the target moves far, or to SLEEP once presence is absent (> ~12 s).
4. **ACTIVE-HI** — object present but **far** (`distance ≥ HILO_FAR_CM`, ~160 cm). Runs the **640×640** model: a distant/small subject needs the higher resolution, but it is expensive (~1 s/frame on the Pi), so the loop is paced slow (~0.40 s). Exits to ACTIVE-LO when the target comes close or presence goes quiet.
5. **WATCHDOG** — entered from any state immediately on sensor-health failure: a `pigpio` fault, no valid ultrasonic echo for > 2 s, or ≥ 3 consecutive dropped echo readings. Captured frames are CLAHE-preprocessed and inferred with the 640×640 model (worst-case assumption: far and dark). Exits to STANDBY only after the sensors stay healthy for ~1.5 s, so a marginal/flaky sensor cannot flap WATCHDOG ↔ ACTIVE.

> **Hysteresis note:** the close (`HILO_CLOSE_CM`, ~120 cm) and far (`HILO_FAR_CM`, ~160 cm) thresholds differ on purpose — the 120–160 cm gap is a hysteresis band that, together with the transition gate below, stops the model flickering HI↔LO at the boundary.

---

## Robustness / Anti-Flap

State decisions are debounced by a single timed **TransitionGate**: an edge commits only when its condition has held continuously for `hold_s` **and** the current state has been occupied for at least `dwell_s`. Timing the streak (rather than counting loop ticks) makes the debounce behave identically regardless of how fast the loop runs in each state.

The gate guards every flicker-prone edge:

- **HI ↔ LO** — condition held `HILO_HOLD_S` (~0.75 s) and ≥ `HILO_DWELL_S` (~1.5 s) in-state.
- **SLEEP wake** — hysteretic (wake at `WAKE_DISTANCE_CM`, stay awake out to the wider `PRESENCE_DISTANCE_CM`) and confirmed (`WAKE_HOLD_S`), so a stray PIR pulse can't wake the node.
- **WATCHDOG recovery** — sticky: leave only after the sonar stays healthy for `WATCHDOG_RECOVER_HOLD_S` and the minimum dwell elapses, so a marginal sensor can't flap the failsafe.

---

## Inference Engine

Inference runs on **`tflite-runtime`** (the lightweight CPU interpreter — not `ultralytics`/`torch`, which are too heavy for the Pi), using **full-integer INT8 YOLOv8-nano** models. INT8 is chosen for the Pi 4B's ARM Neon SIMD unit, which does INT8 multiply-accumulate faster than FP32 and at lower memory bandwidth.

Because a full-integer INT8 model bakes its input resolution in at export time, the adaptive 320/640 switch is achieved by loading **two models** (one per resolution) and selecting the matching interpreter per FSM state. The pre/post-processing that `ultralytics` would do internally is reimplemented by hand in NumPy/OpenCV: **letterbox → quantize → invoke → dequantize → decode the YOLOv8 head → NMS → map boxes back to the original frame's pixels**. Model export to `.tflite` is done off-device.

### CLAHE Preprocessing

Orthogonal to the 5 states, the captured frame is enhanced when the LDR reports darkness (LOW output). The BGR frame is converted to YUV and CLAHE is applied to the **Y (brightness) channel only**, leaving chroma (U, V) untouched so colour is not distorted — then converted back.

- `tileGridSize = (8, 8)` — splits the frame into an 8×8 grid (64 tiles, ~80×60 px each on a 640×480 frame), equalising each tile against its own local histogram (bilinearly interpolated across tiles to avoid blocky seams).
- `clipLimit = 2.0` — the contrast cap. The histogram has 256 bins (8-bit Y); OpenCV clips each bin at `clipLimit × (tile_pixels / 256)` ≈ 2× the average bin height (~4800 px/tile ÷ 256 ≈ 19 px → clip at ~38 px), then redistributes the excess. This bounds the slope of the equalisation curve, which stops near-flat dark regions from amplifying sensor noise. `2.0` is a deliberately mild value.

WATCHDOG forces CLAHE on regardless (worst-case low-light assumption).

---

## Concurrency: Threading & Core Pinning

To manage the Pi's limited compute and prevent jitter in frame capture and inference, work is split across threads and pinned to specific cores with `os.sched_setaffinity`:

| Core | Responsibility |
|---|---|
| 0 | OS tasks + (optional) snapshot writing and cloud telemetry upload |
| 1 | Sensor I/O (GPIO harvester) |
| 2 & 3 | Camera capture, CLAHE, FSM-based inference (`tflite-runtime`), and GUI — run sequentially |

The implementation uses up to **6 threads** — **4 run by default**, plus 2 that start only with their opt-in flags:

- **Main** — launches the worker threads and parks until a shutdown event or `Ctrl+C`.
- **GPIO harvester** — continuously samples PIR, LM393 (LDR), and HC-SR04 (ultrasonic).
- **Vision engine** — camera capture, CLAHE, YOLOv8 inference, and GUI.
- **pigpio echo callback** — runs in pigpio's own real-time thread (the ISR equivalent), timestamping ultrasonic echo edges.
- **Cloud uploader** *(opt-in, `--cloud`)* — uploads telemetry to ThingSpeak.
- **Snapshot writer** *(opt-in, `--snapshots`)* — writes detection frames to the SD card.

Cross-thread communication:

| Channel | Type | Between |
|---|---|---|
| `echo_lock` | mutex | pigpio echo callback ⇄ GPIO harvester |
| shared sensor state | mutex | GPIO harvester ⇄ vision engine |
| `_latest_record_lock` | mutex | vision engine ⇄ cloud uploader |
| `_snapshot_queue` | bounded queue (drop-oldest) | vision engine ⇄ snapshot writer |

The hot threads (sensors, vision) never block on slow I/O — they hand work to the background threads via a latest-value snapshot (sensor state, telemetry record) or a bounded queue (snapshots). This scales across cores despite Python's GIL because the heavy sections (TFLite `invoke()`, OpenCV, socket I/O) release the GIL and run as true native parallel work.

---

## Telemetry, Cloud & GUI

Every loop the vision thread assembles **one telemetry record** (a dict) and fans it out to non-blocking sinks (so a sink can never stall the FSM). The record:

| Field | Description |
|---|---|
| `state` | FSM state (`SLEEP` / `STANDBY` / `ACTIVE-LO` / `ACTIVE-HI` / `WATCHDOG`) |
| `model_res` | inference resolution / model used (`320`, `640`, or `---` when idle) |
| `latency_ms` | per-frame inference duration |
| `cpu_pct`, `cpu_temp_c` | CPU utilisation % and core temperature °C |
| `power_w` | whole-Pi power draw (INA219 over I²C; `-- W` until the sensor is wired) |
| `distance_cm` | median ultrasonic distance |
| `detections` | list of `(label, confidence%)` |

The default sink is the **terminal sink**, which prints one fixed-format line per loop to the Pi's own console — so the system runs fully offline:

```
[14:22:07] ACTIVE-LO | model 320 | lat   28.4ms | cpu 47.0% | temp 58.1C | pwr  -- W | dist   95.3cm | dets: Student/Person(94.2%)
```

### Optional sinks (off by default)

- **`--cloud`** — a background thread POSTs power / latency / CPU-temp / distance to a ThingSpeak channel every 20 s (the free-tier rate limit). `emit_telemetry()` only stashes the latest record, so the network call never runs in the inference loop. The write key is read from a git-ignored `.env`. See [`docs/SETUP.md`](docs/SETUP.md), Phase 4.
- **`--snapshots`** — on a person detection, a worker thread writes a JPEG of the frame to `./snapshots/` (ring-buffered to a file cap, filename carries timestamp/class/conf/state).

> Both are opt-in and add Wi-Fi/disk activity, so keep them **off during measured benchmark runs** (they would bias the INA219's whole-Pi reading).

### On-Pi GUI (demo)

For demos the node opens a local window on the Pi's HDMI monitor (default; pass `--headless` to disable it for remote deployment). It shows the live feed with **blue detection boxes** and a HUD header above the unobstructed video:

```
SAGE-Vision   ACTIVE-LO            ← state name colour-coded (green active / grey idle / red watchdog)
Model 320 | Objects: 3 | 28 ms | 31 FPS
cpu 47% | 58C | -- W | dist 95 cm
```

During SLEEP/STANDBY the video area shows a "SYSTEM IDLE" placeholder. Keys: **`q`** quits cleanly, **`f`** toggles fullscreen. The GUI runs inside the vision thread (Cores 2 & 3) and adds negligible cost.

---

## Power Measurement

The headline metric — energy draw — is measured with an **INA219** sitting high-side, inline on the Pi's incoming 5V USB-C feed (see [Hardware & Wiring](#hardware--wiring)), so it reads the **whole-Pi** power draw over I²C. The reading fills the `power_w` telemetry column (watts), and reads `-- W` until the sensor is wired. The node runs fine without it — power instrumentation is only for benchmarking.

---

## Limitations & Future Work

- The 640 INT8 model currently saturates (an INT8 calibration issue), weakening the ACTIVE-HI vision vote; it needs re-export with a representative calibration set.
- **PIR and LDR are not health-monitorable** — a dead pin reads as a quiet/lit room and is invisible to the WATCHDOG (only the ultrasonic sensor is observable). An *out-of-range* echo (an empty room) still returns a valid pulse and counts as healthy.
- `is_dark` has **no software debounce**, so CLAHE can toggle frame-to-frame at the light threshold (it relies on the LM393's hardware hysteresis).
- The median distance filter adds ~0.12 s of lag (accepted for stability).
- The energy benefit is **not yet proven** — it requires the INA219 rig built and a controlled measurement run against the baseline.

---

## Repository Structure

```
SAGE-Vision/
├── firmware/
│   └── esp32_sensor_node/
│       └── esp32_sensor_node.ino          # LEGACY — original ESP32 sensor transmitter (unused)
├── rpi_edge/
│   ├── pi_edge_node.py                    # Main adaptive inference node (reads GPIO sensors)
│   ├── yolo_tflite.py                     # Lightweight tflite-runtime YOLOv8 detector
│   ├── requirements.txt                   # Pi Python dependencies
│   ├── .env.example                       # Template for the ThingSpeak key (copy to .env)
│   ├── yolov8n_320_int8.tflite            # INT8 TFLite model — 320×320 (ACTIVE-LO)
│   └── yolov8n_640_int8.tflite            # INT8 TFLite model — 640×640 (ACTIVE-HI / WATCHDOG)
├── test/
│   ├── test_baseline_edge.py              # Unoptimised control benchmark (terminal + GUI)
│   └── analyze_log.py                     # Parse a captured telemetry log into per-run metrics
├── docs/
│   ├── SETUP.md                           # Installation and execution guide
│   ├── TESTING.md                         # Benchmarking and validation procedure
│   ├── HARDWARE_CONNECTIONS.md            # Wiring tables for all sensors + INA219
│   └── ENGINEERING_LOG.md                 # Problems faced, solutions, tradeoffs, limitations
├── snapshots/                             # Detection JPEGs (created at runtime; git-ignored)
├── .env.example                           # Template for the ThingSpeak key (copy to .env)
├── .gitignore
└── README.md                              # This file — project overview & architecture
```

### Documentation

- [docs/SETUP.md](docs/SETUP.md) — installation and execution (HDMI or VNC display), plus the optional `--cloud` setup.
- [docs/HARDWARE_CONNECTIONS.md](docs/HARDWARE_CONNECTIONS.md) — full wiring for every sensor and the INA219 power rig.
- [docs/TESTING.md](docs/TESTING.md) — benchmarking the adaptive node against the baseline.

---

## Key Design Decisions at a Glance

| Decision | Rationale |
|---|---|
| On-Pi terminal telemetry + local GUI, offline by default | No network in the live path, no frame-encoding overhead, no second machine |
| Telemetry record + non-blocking sinks | Terminal always; cloud/snapshot sinks drop in without touching the FSM |
| Direct GPIO sensors via `pigpio` | Removes the ESP32 and serial link; hardware-timestamped echo edges keep distance accurate |
| Two pinned threads + core affinity | Isolates GPIO sensor I/O jitter from the inference loop's timing |
| Sensor-fused presence (PIR ∨ proximity ∨ vision vote) | A still person is invisible to PIR alone; fusion prevents false SLEEP |
| Timed TransitionGate on every edge | Debounce behaves identically regardless of per-state loop pace; kills flicker |
| Two fixed-resolution INT8 models | Full-integer export bakes input size; switching models is the adaptive resolution |
| INT8 TFLite over FP32 PyTorch | 2–4× lower inference time on ARM Neon; no GPU required |
| CLAHE on luma over global brightness | Preserves local contrast for detection; global boost washes out fine edges |

---

## License

This project's source code is released under the [MIT License](LICENSE) — you're free to use, modify, and distribute it with attribution.

> **Note on the model weights:** the bundled `yolov8n_*_int8.tflite` files are derived from [Ultralytics YOLOv8](https://github.com/ultralytics/ultralytics), which is licensed **AGPL-3.0**. The MIT license above covers this project's own code; redistributing or deploying the YOLOv8-derived weights may carry AGPL-3.0 obligations. For non-AGPL use, see Ultralytics' commercial licensing.
