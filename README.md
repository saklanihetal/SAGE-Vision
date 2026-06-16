# SAGE-Vision
### Sensor-Adaptive GPU-less Edge Vision

A distributed edge intelligence pipeline that uses real-time environmental sensor data to dynamically govern a YOLOv8 INT8 object detection engine running on a Raspberry Pi 4B — reducing idle CPU usage, preventing thermal throttling, and preserving detection accuracy without a GPU.

---

## The Problem This Solves

Running a continuous computer vision inference loop on an ARM SoC like the Raspberry Pi 4B is thermally expensive. A fixed-resolution YOLOv8 model polling at full pace will push the Broadcom BCM2711 toward its 80°C throttling boundary within minutes, forcing the chip to drop its clock speed and degrade latency. Standard solutions — adding a heatsink, buying an AI accelerator hat, or switching to a weaker model — either add hardware cost or sacrifice detection quality.

SAGE-Vision takes a different approach: **let the physical environment decide how hard the CPU needs to work.** If nobody is in the room, inference should stop entirely. If someone is far away, lower resolution is sufficient. If the room is dark, the frame needs preprocessing before inference — not a resolution bump. Sensor data drives all three of these decisions in real time, with zero additional hardware cost beyond the IoT board already present in the lab.

---

## System Architecture

The system is **self-contained on the Raspberry Pi**. Sensors wire directly to its GPIO header; results are shown on a local HDMI GUI and logged to the Pi's own terminal — no second board and no network in the live path:

```
   Sensors ── wired to Raspberry Pi GPIO header
   ┌──────────────────────────────────────┐
   │  PIR (HC-SR501)  OUT ── GPIO 17        │
   │  LM393 light     DO  ── GPIO 27        │
   │  HC-SR04  TRIG ── GPIO 23              │
   │           ECHO ── GPIO 24 (divider)    │
   │  INA260 power    SDA/SCL ── I2C (opt.) │
   └────────────────────┬───────────────────┘
                        ▼
┌──────────────────────────────┐
│   Raspberry Pi 4B            │
│                              │
│  Thread 1 (Core 1)           │
│   GPIO sensor harvester      │
│   ── pigpio: PIR / LM393     │
│      + HC-SR04 echo ISR      │
│   ── median-filters distance │
│   ── updates shared state    │
│      (mutex)                 │
│                              │
│  Thread 2 (Cores 2+3)        │
│   Adaptive vision loop       │
│   ── PIR standby gate        │
│   ── LM393 CLAHE gate        │
│   ── Ultrasonic res gate     │
│   ── YOLOv8 INT8 TFLite      │
│   ── telemetry record        │
│      ──► terminal sink       │
│      ──► (cloud sink: TODO)  │
│   ── on-Pi GUI (boxes + HUD) │
└───────┬───────────────┬──────┘
        ▼               ▼
   HDMI monitor     Pi terminal
   (live video +    (telemetry log,
    blue boxes)      every state)
```

### Why This Topology

The Raspberry Pi reads the sensors directly off its GPIO header through the `pigpio` daemon, which captures the HC-SR04 echo edges with hardware timestamps in its own real-time thread — the equivalent of a microcontroller ISR, but without a second board or a serial link to keep in sync. Sensor sampling lives on a thread pinned to Core 1; all inference, adaptive decision-making, and the demo GUI run on a separate thread pinned to Cores 2 & 3. Telemetry is printed to the Pi's own terminal, so the whole system runs **fully offline** (an optional cloud sink and an INA260 power reading are stubbed in `pi_edge_node.py` for later).

The two on-Pi threads communicate only through a mutex-guarded latest-value snapshot, so a slow inference frame can never starve the sensor reader — and reading sensors directly on the Pi removes the ESP32, the serial link, and the whole class of serial-framing and buffer-overflow failure modes that came with them.

---

## Finite State Machine & Sensor Core Management

The edge vision node uses a five-state finite state machine. Each state has explicit entry and exit conditions, making the system's behavior deterministic and auditable. The architecture eliminates resolution thrashing through hysteresis dead bands, filters ultrasonic noise through a rolling median window, and maintains a hardware failsafe that activates independently of all other state logic.

---

### The five states

**Sleep**

The system is fully idle. No camera frames are captured and the inference pipeline is completely bypassed. The CPU governor drops to powersave. This state is entered only when both sensors simultaneously agree the space is vacant: the PIR cooldown has expired with no motion detected for at least five continuous seconds, and the rolling median ultrasonic distance reads above 350 cm. Both conditions must hold — either sensor alone registering presence is sufficient to prevent entry. The system exits Sleep the instant either condition breaks: a new PIR trigger or median distance dropping below 350 cm. Exit always routes through Standby; there is no direct transition to an Active state.

**Standby**

A transient warmup state lasting exactly one 200 ms execution tick. No inference runs. Its purpose is to absorb hardware stabilization time before the first inference frame fires: the CPU governor is raised, a fresh sensor reading is allowed to settle, and the camera's auto-exposure arrays are given time to stabilise. After the single tick, the machine evaluates median distance and routes immediately to Active-Lo or Active-Hi. Standby also serves as the mandatory re-entry point after recovering from Watchdog, ensuring sensor data is fresh before any resolution decision is made.

**Active-Lo**

Runs YOLOv8n INT8 inference at 320×320 resolution with a 10 ms loop pace for responsive close-range tracking. Entered from Standby when median distance is below 120 cm. At close range, the subject occupies a large portion of the frame and 320×320 provides sufficient pixel density for reliable detection. To prevent thrashing at the boundary, the system holds this state until distance rises above 160 cm — it will not scale up at 120 cm. Presence loss (PIR cooldown expired and distance above 350 cm) transitions directly to Sleep.

**Active-Hi**

Runs YOLOv8n INT8 inference at 640×640 resolution with a 400 ms loop pace to conserve processing cycles. Entered from Standby when median distance is at or above 160 cm, or when the subject sits in the 120–160 cm overlap zone as the safe default. At range, the subject occupies a small pixel footprint — dropping to 320×320 here risks the detection falling below confidence threshold entirely. The system holds this state until distance drops below 120 cm. Presence loss transitions directly to Sleep.

**Watchdog**

A failsafe override that operates independently of all other state logic and takes unconditional priority. Entered when any of three data-integrity conditions is met: the `pigpio` daemon connection is lost, the time elapsed since the last valid ultrasonic echo exceeds 2.0 seconds, or the harvester registers three or more consecutive sonar triggers that returned no echo at all. While active, the system runs at maximum capability — 640×640 resolution with CLAHE permanently enabled — regardless of any cached sensor values, since those values can no longer be trusted. The only exit path is a successfully completed echo transaction. On exit, the machine routes through Standby before resuming normal resolution decisions.

Note that the watchdog can only monitor the ultrasonic sensor and the daemon connection. The PIR and LM393 are read as digital levels — a dead or disconnected level pin reads a constant value indistinguishable from a genuinely quiet or lit room, so those two sensors cannot be health-checked. An *out-of-range* echo (an empty room) still returns a valid pulse and is treated as a healthy reading, not a failure.

---

### Hysteresis zones

Two sensors use asymmetric entry and exit thresholds to prevent state oscillation caused by sensor noise near decision boundaries.

1. The ultrasonic distance gate uses a 40 cm dead band: the system enters Active-Lo below 120 cm and exits it only above 160 cm. Within the 120–160 cm band, the current state is held regardless of new readings.

2. The light gate's hysteresis now lives in hardware: the LM393 comparator supplies a digital dark/bright signal whose threshold is set by an onboard potentiometer, and the comparator's own built-in hysteresis prevents the output from chattering under flickering or transitional lighting. CLAHE simply follows that digital line — no software dead-band required.

---

### Ultrasonic median filter

The HC-SR04 ultrasonic sensor produces measurement noise of ±5–10 cm at distances above one metre due to acoustic multipath reflections. Raw readings are never used directly in state logic. Instead, the Pi's sensor harvester thread maintains a circular buffer of five consecutive distance samples and feeds only the median into the shared state each cycle. This eliminates spurious spikes without introducing meaningful latency at human-movement timescales.

---

### CLAHE preprocessing order

Low-light enhancement runs orthogonally to resolution state. When active, the processing pipeline follows this fixed order: capture the full-resolution frame, apply CLAHE equalization, then resize to the target resolution, then run inference. Applying CLAHE before the resize ensures the contrast enhancement operates on maximum available pixel data. Reversing this order degrades enhancement quality by discarding spatial information before processing it.

---

## Sensor Acquisition (on the Pi)

The harvester thread reads all three sensors directly off the GPIO header via `pigpio`, at roughly a 16.6 Hz sonar cadence:

| Signal | Source | Pi pin | Reading |
|---|---|---|---|
| Motion | HC-SR501 PIR `OUT` | GPIO 17 | digital level — 1 = motion |
| Light | LM393 comparator `DO` | GPIO 27 | digital level — active-low, LOW = dark |
| Distance | HC-SR04 `TRIG`/`ECHO` | GPIO 23 / 24 | echo pulse width → cm; −1.0 = out of range |

The ultrasonic measurement is interrupt-driven through `pigpio`: a 10 µs trigger pulse is emitted every ~60 ms, and a hardware-timestamped edge callback records the echo pulse width on both rising and falling edges — the same non-blocking ISR pattern the original firmware used, now running in the `pigpio` daemon's real-time thread. Because the daemon captures edges independently of the Python loop, sonar timing is never gated on inference load.

---

## Telemetry & the On-Pi Demo GUI

Each loop the vision thread assembles **one telemetry record** (a dict) and fans it out to a set of sinks:

| Field | Description |
|---|---|
| `state` | FSM state (`SLEEP`/`STANDBY`/`ACTIVE-LO`/`ACTIVE-HI`/`WATCHDOG`) |
| `model_res` | Inference resolution / model used: `320`, `640`, or `---` when idle |
| `latency_ms` | Per-frame inference duration |
| `cpu_pct`, `cpu_temp_c` | CPU utilisation % and core temperature °C |
| `power_w` | Whole-Pi power draw (INA260 over I2C — `-- W` until the sensor is wired) |
| `distance_cm` | Median ultrasonic distance |
| `detections` | List of `(label, confidence%)` — **all** detections, not capped |

The only active sink today is the **terminal sink** (`_terminal_sink`), which prints one line per loop to the Pi's own console — the system runs fully offline. Two sinks are stubbed for later, both designed to be non-blocking so they never stall the FSM:
- `read_power_w()` — read the INA260 and fill the `power_w` column.
- `_cloud_sink()` — push records to a cloud platform (one line to enable in `emit_telemetry`).

Sample terminal line:
```
[14:22:07] ACTIVE-LO | model 320 | lat   28.4ms | cpu 47.0% | temp 58.1C | pwr  -- W | dist   95.3cm | dets: Student/Person(94.2%)
```

### On-Pi GUI (demo)

For demos, the node opens a local window on the Pi's HDMI monitor (run with the default; pass `--headless` to disable it for remote deployment). It shows the live camera feed with **blue detection boxes** and a solid HUD header bar above the (unobstructed) video:

```
SAGE-Vision   ACTIVE-LO            ← state name colour-coded (green active / grey idle / red watchdog)
Model 320 | Objects: 3 | 28 ms | 31 FPS
cpu 47% | 58C | -- W | dist 95 cm
```

Boxes are pure blue `(255,0,0)`; each label (`Class conf%`) is blue with a thin black outline for legibility. During SLEEP/STANDBY the video area shows a "SYSTEM IDLE" placeholder so the window stays responsive. Keys: **`q`** quits the node cleanly, **`f`** toggles fullscreen. The GUI render runs inside the vision thread (Cores 2 & 3) and adds negligible cost — far less than encoding and streaming frames over a network would.

---

## CPU Core Pinning

The Pi runs two threads pinned to separate CPU cores using `os.sched_setaffinity`:

- **Core 1** — GPIO sensor harvester thread. Triggers the HC-SR04, consumes the `pigpio` echo callbacks, and polls the PIR and LM393, isolating sensor sampling from inference latency jitter.
- **Cores 2 & 3** — Vision processing thread. OpenCV frame capture and YOLO inference share the two higher-numbered cores, leaving Core 0 free for the OS and system services.

This prevents the bursty inference workload from starving the sensor reader, which would cause sensor state to fall behind reality and weaken gate responsiveness. (The `pigpio` daemon timestamps echo edges in its own thread regardless, so distance accuracy is preserved even during an inference spike.)

---

## Model

YOLOv8n (nano) exported to TFLite with full INT8 quantisation. The INT8 format was chosen specifically for the Pi 4B's ARM Neon SIMD unit, which performs INT8 multiply-accumulate operations faster than FP32 equivalents and consumes significantly less memory bandwidth. The nano variant is sufficient because the project's target objects (person, chair, laptop, water bottle) are well-represented in the COCO training set and do not require a larger backbone for acceptable accuracy at lab distances.

---

## Repository Structure

```
SAGE-Vision/
├── firmware/
│   └── esp32_sensor_node/
│       └── esp32_sensor_node.ino          # LEGACY — original ESP32 sensor transmitter (unused)
├── rpi_edge/
│   ├── pi_edge_node.py                    # Main adaptive inference daemon (reads GPIO sensors)
│   ├── yolo_tflite.py                     # Lightweight tflite-runtime YOLOv8 detector
│   ├── requirements.txt                   # Pi Python dependencies
│   ├── yolov8n_320_int8.tflite            # INT8 TFLite model — 320×320 (Active-Lo)
│   └── yolov8n_640_int8.tflite            # INT8 TFLite model — 640×640 (Active-Hi / Watchdog)
├── test/
│   └── test_baseline_edge.py              # Unoptimised control benchmark (terminal + GUI)
├── docs/
│   ├── SETUP.md                           # Installation and execution guide
│   ├── TESTING.md                         # Benchmarking and validation procedure
│   └── HARDWARE_CONNECTIONS.md            # Wiring tables for all sensors
├── .gitignore
└── README.md                              # This file — project overview & architecture
```

### Documentation

- [docs/SETUP.md](docs/SETUP.md) — installation and execution (HDMI or VNC display).
- [docs/HARDWARE_CONNECTIONS.md](docs/HARDWARE_CONNECTIONS.md) — full wiring for every sensor.
- [docs/TESTING.md](docs/TESTING.md) — benchmarking the adaptive node against the baseline.

---

## Key Design Decisions at a Glance

| Decision | Rationale |
|---|---|
| On-Pi terminal telemetry + local GUI | Fully offline; no network in the live path, no frame-encoding overhead, no second machine |
| Telemetry record + pluggable sinks | Terminal now; cloud/power sinks drop in later without touching the FSM |
| Direct GPIO sensors via `pigpio` | Removes the ESP32 and serial link; hardware-timestamped echo edges keep distance accurate |
| Thread-per-concern with mutex | Isolates GPIO sensor I/O jitter from inference loop timing |
| INT8 TFLite over FP32 PyTorch | 2–4× lower inference time on ARM Neon; no GPU required |
| CLAHE over global brightness | Preserves local contrast for detection; global boost washes out fine edges |
| CPU core affinity pinning | Prevents OS scheduler from migrating threads mid-inference, stabilising latency |
