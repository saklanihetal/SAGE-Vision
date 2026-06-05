# SAGE-Vision
### Sensor-Adaptive GPU-less Edge Vision

A distributed edge intelligence pipeline that uses real-time environmental sensor data to dynamically govern a YOLOv8 INT8 object detection engine running on a Raspberry Pi 4B — reducing idle CPU usage, preventing thermal throttling, and preserving detection accuracy without a GPU.

---

## The Problem This Solves

Running a continuous computer vision inference loop on an ARM SoC like the Raspberry Pi 4B is thermally expensive. A fixed-resolution YOLOv8 model polling at full pace will push the Broadcom BCM2711 toward its 80°C throttling boundary within minutes, forcing the chip to drop its clock speed and degrade latency. Standard solutions — adding a heatsink, buying an AI accelerator hat, or switching to a weaker model — either add hardware cost or sacrifice detection quality.

SAGE-Vision takes a different approach: **let the physical environment decide how hard the CPU needs to work.** If nobody is in the room, inference should stop entirely. If someone is far away, lower resolution is sufficient. If the room is dark, the frame needs preprocessing before inference — not a resolution bump. Sensor data drives all three of these decisions in real time, with zero additional hardware cost beyond the IoT board already present in the lab.

---

## System Architecture

The pipeline is split across three physical nodes connected over serial and UDP:

```
┌─────────────────────────┐
│   RV-IoT Board (ESP32)  │
│                         │
│  PIR  ── GPIO 25        │
│  LDR  ── GPIO 39        │
│  HC-SR04                │
│    TRIG ── GPIO 26      │
│    ECHO ── GPIO 27      │
│    (via voltage divider) │
└────────────┬────────────┘
             │  7-byte packed binary struct
             │  UART Serial @ 115200 baud
             ▼
┌─────────────────────────┐
│   Raspberry Pi 4B       │
│                         │
│  Thread 1 (Core 1)      │
│   Serial harvester      │
│   ── reads sensor data  │
│   ── updates shared     │
│      state (mutex)      │
│                         │
│  Thread 2 (Cores 2+3)   │
│   Adaptive vision loop  │
│   ── PIR standby gate   │
│   ── LDR CLAHE gate     │
│   ── Ultrasonic res gate│
│   ── YOLOv8 INT8 TFLite │
│   ── packs 40-byte UDP  │
└────────────┬────────────┘
             │  40-byte packed binary UDP payload
             │  to port 8080
             ▼
┌─────────────────────────┐
│   Host Laptop           │
│                         │
│  laptop_logger.py       │
│   ── decodes payload    │
│   ── live terminal UI   │
│   ── CSV log writer     │
│                         │
│  plot_comparison.py     │
│   ── offline analysis   │
│   ── 4-panel dashboard  │
└─────────────────────────┘
```

### Why This Topology

The workload split is deliberately asymmetric. The ESP32 is responsible only for sensor sampling — it runs an interrupt-driven ultrasonic measurement loop and polls the PIR and LDR on a 60 ms cycle, then blasts a 7-byte binary struct down the serial line. It never does any computation on the data. The Raspberry Pi handles all inference and all adaptive decision-making. The laptop handles only logging and visualisation, keeping its processing entirely offline from the inference loop so it adds zero latency.

Each node does exactly one thing well, and a failure in one node degrades gracefully rather than crashing the whole pipeline.

---

## Finite State Machine & Sensor Core Management

The edge vision node uses a five-state finite state machine. Each state has explicit entry and exit conditions, making the system's behavior deterministic and auditable. The architecture eliminates resolution thrashing through hysteresis dead bands, filters ultrasonic noise through a rolling median window, and maintains a hardware failsafe that activates independently of all other state logic.

---

### The five states

**Sleep**

The system is fully idle. No camera frames are captured and the inference pipeline is completely bypassed. The CPU governor drops to powersave. This state is entered only when both sensors simultaneously agree the space is vacant: the PIR cooldown has expired with no motion detected for at least five continuous seconds, and the rolling median ultrasonic distance reads above 350 cm. Both conditions must hold — either sensor alone registering presence is sufficient to prevent entry. The system exits Sleep the instant either condition breaks: a new PIR trigger or median distance dropping below 350 cm. Exit always routes through Standby; there is no direct transition to an Active state.

**Standby**

A transient warmup state lasting exactly one 200 ms execution tick. No inference runs. Its purpose is to absorb hardware stabilization time before the first inference frame fires: the CPU governor is raised, serial buffers are flushed, and the camera's auto-exposure arrays are given time to settle. After the single tick, the machine evaluates median distance and routes immediately to Active-Lo or Active-Hi. Standby also serves as the mandatory re-entry point after recovering from Watchdog, ensuring sensor data is fresh before any resolution decision is made.

**Active-Lo**

Runs YOLOv8n INT8 inference at 320×320 resolution with a 10 ms loop pace for responsive close-range tracking. Entered from Standby when median distance is below 120 cm. At close range, the subject occupies a large portion of the frame and 320×320 provides sufficient pixel density for reliable detection. To prevent thrashing at the boundary, the system holds this state until distance rises above 160 cm — it will not scale up at 120 cm. Presence loss (PIR cooldown expired and distance above 350 cm) transitions directly to Sleep.

**Active-Hi**

Runs YOLOv8n INT8 inference at 640×640 resolution with a 400 ms loop pace to conserve processing cycles. Entered from Standby when median distance is at or above 160 cm, or when the subject sits in the 120–160 cm overlap zone as the safe default. At range, the subject occupies a small pixel footprint — dropping to 320×320 here risks the detection falling below confidence threshold entirely. The system holds this state until distance drops below 120 cm. Presence loss transitions directly to Sleep.

**Watchdog**

A failsafe override that operates independently of all other state logic and takes unconditional priority. Entered when either of two data-integrity conditions is met: the time elapsed since the last valid serial frame exceeds 2.0 seconds, or the asynchronous reader thread registers three or more consecutive dropped or corrupted packets. While active, the system runs at maximum capability — 640×640 resolution with CLAHE permanently enabled — regardless of any cached sensor values, since those values can no longer be trusted. The only exit path is a successfully validated incoming packet. On exit, the machine routes through Standby before resuming normal resolution decisions.

---

### Hysteresis zones

Two sensors use asymmetric entry and exit thresholds to prevent state oscillation caused by sensor noise near decision boundaries.

1. The ultrasonic distance gate uses a 40 cm dead band: the system enters Active-Lo below 120 cm and exits it only above 160 cm. Within the 120–160 cm band, the current state is held regardless of new readings.

2. The LDR light gate uses a 70-count dead band: CLAHE activates when the LDR value drops below 350 and deactivates only when it rises above 420. This prevents preprocessing from toggling on and off under flickering or transitional lighting conditions.

---

### Ultrasonic median filter

The HC-SR04 ultrasonic sensor produces measurement noise of ±5–10 cm at distances above one metre due to acoustic multipath reflections. Raw readings are never used directly in state logic. Instead, the ESP32 maintains a circular buffer of five consecutive distance samples and transmits only the median value each cycle. This eliminates spurious spikes without introducing meaningful latency at human-movement timescales.

---

### CLAHE preprocessing order

Low-light enhancement runs orthogonally to resolution state. When active, the processing pipeline follows this fixed order: capture the full-resolution frame, apply CLAHE equalization, then resize to the target resolution, then run inference. Applying CLAHE before the resize ensures the contrast enhancement operates on maximum available pixel data. Reversing this order degrades enhancement quality by discarding spatial information before processing it.

---

## Serial Protocol

The ESP32 transmits a packed 7-byte binary struct at approximately 16.6 Hz:

| Field | C Type | Bytes | Description |
|---|---|---|---|
| `pir_state` | `uint8_t` | 1 | 0 = no motion, 1 = motion detected |
| `ldr_value` | `uint16_t` | 2 | Light level mapped 0–1023 (0 = dark) |
| `distance_cm` | `float` | 4 | Ultrasonic distance in cm; −1.0 = out of range |

The ultrasonic measurement is fully interrupt-driven on the ESP32. The echo pin fires an ISR on both rising and falling edges, recording pulse duration without blocking the main loop. This means the serial transmission rate is never gated on the sonar cycle time.

---

## UDP Telemetry Payload

The Raspberry Pi packs all telemetry into a fixed 40-byte little-endian struct before each UDP transmission:

| Field | Format | Bytes | Description |
|---|---|---|---|
| `mode` | `uint8` | 1 | 0 = standby, 1 = active inference |
| `cpu_pct` | `float` | 4 | CPU utilisation % |
| `cpu_temp` | `float` | 4 | Core temperature °C |
| `distance` | `float` | 4 | Last sensor distance reading (cm) |
| `img_res` | `uint16` | 2 | Inference resolution (320 or 640) |
| `latency_ms` | `float` | 4 | Per-frame inference duration (ms) |
| `det_count` | `uint8` | 1 | Number of active detections (max 4) |
| `classes[4]` | `4× uint8` | 4 | COCO class IDs (255 = empty slot) |
| `confs[4]` | `4× float` | 16 | Detection confidence scores (0.0–1.0) |
| **Total** | | **40** | |

The fixed-width binary format is chosen deliberately over JSON or CSV. At the logging frequency the system runs, a JSON packet would be 3–5× larger and require string parsing on every frame. The binary struct unpacks in a single `struct.unpack` call with zero string allocation.

---

## CPU Core Pinning

The Pi runs two threads pinned to separate CPU cores using `os.sched_setaffinity`:

- **Core 1** — Serial harvester thread. Dedicated to blocking I/O, isolating serial reads from inference latency jitter.
- **Cores 2 & 3** — Vision processing thread. OpenCV frame capture and YOLO inference share the two higher-numbered cores, leaving Core 0 free for the OS and system services.

This prevents the bursty inference workload from starving the serial reader, which would cause sensor state to fall behind reality and weaken gate responsiveness.

---

## Model

YOLOv8n (nano) exported to TFLite with full INT8 quantisation. The INT8 format was chosen specifically for the Pi 4B's ARM Neon SIMD unit, which performs INT8 multiply-accumulate operations faster than FP32 equivalents and consumes significantly less memory bandwidth. The nano variant is sufficient because the project's target objects (person, chair, laptop, water bottle) are well-represented in the COCO training set and do not require a larger backbone for acceptable accuracy at lab distances.

---

## Repository Structure

```
SAGE-Vision/
├── firmware/
│   └── esp32_sensor_node/
│       └── esp32_sensor_node.ino          # ESP32 binary sensor transmitter
├── rpi_edge/
│   ├── pi_edge_node.py                    # Main adaptive inference daemon
│   ├── requirements.txt                   # Pi Python dependencies
│   └── yolov8n_full_integer_quant.tflite  # INT8 TFLite model weights
├── host_laptop/
│   ├── laptop_logger.py                   # UDP receiver, terminal UI, CSV writer
│   └── requirements.txt                   # Laptop Python dependencies
├── test/
│   └── test_baseline_edge.py              # Unoptimised control-group benchmark
├── plot_comparison.py                     # Offline 4-panel performance dashboard
├── .gitignore
├── README.md                              # This file — project overview & architecture
├── SETUP.md                               # Installation and execution guide
├── TESTING.md                             # Benchmarking and validation procedure
└── HARDWARE_CONNECTIONS.md                # Wiring tables for all sensors
```

---

## Key Design Decisions at a Glance

| Decision | Rationale |
|---|---|
| UDP over TCP for telemetry | No connection-handshake latency; dropped log frames are acceptable |
| Thread-per-concern with mutex | Isolates serial I/O jitter from inference loop timing |
| INT8 TFLite over FP32 PyTorch | 2–4× lower inference time on ARM Neon; no GPU required |
| CLAHE over global brightness | Preserves local contrast for detection; global boost washes out fine edges |
| CPU core affinity pinning | Prevents OS scheduler from migrating threads mid-inference, stabilising latency |
