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

## The Three Adaptive Gates

All three gates are evaluated on every vision loop iteration, in order of priority:

### Gate 1 — PIR Standby Suppression
If the PIR sensor reports no motion and the cooldown window (5 seconds) has expired, the vision loop sleeps for 1 second and transmits a standby packet. The camera is not read. The YOLO model is not invoked. CPU utilisation drops to single-digit percentages. This is the most impactful optimisation — in a real lab environment, the camera field of view is unoccupied for the majority of the day.

### Gate 2 — Ultrasonic Resolution Scaling
When motion is detected, the ultrasonic sensor measures the distance to the nearest object. If the subject is closer than 150 cm, the inference input is downsampled to 320×320 pixels instead of 640×640. This reduces the pixel count fed to the model by 75%, cutting matrix multiplication workload proportionally and dropping per-frame latency significantly. At close range the object occupies a large fraction of the frame, so the lower resolution does not meaningfully reduce detection confidence.

### Gate 3 — LDR CLAHE Low-Light Enhancement
If the LDR reads below the dark threshold (350 out of 1023 on the mapped scale), the frame is converted to YUV colour space and a CLAHE (Contrast Limited Adaptive Histogram Equalisation) filter is applied to the Y (luminance) channel before inference. This compensates for poor ambient lighting without globally brightening the image, preserving the fine local contrast needed for accurate bounding-box localisation.

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
