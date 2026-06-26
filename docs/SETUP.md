# SAGE-Vision — Setup & Execution Guide

This document covers everything from flashing the OS to running the live pipeline. Follow all five phases in order on your first setup.

---

## Hardware Requirements

### Raspberry Pi Node
- Raspberry Pi 4B (2 GB RAM minimum; 4 GB recommended)
- MicroSD card (16 GB minimum, Class 10 / A1 rated)
- USB Web Camera (UVC-compliant; plug-and-play, no drivers needed)
- A **display for the demo GUI** — either a physical **HDMI monitor** (+ keyboard) or remote **VNC** from your computer (see Phase 4). *(Not needed if you run with `--headless`.)*
- Sensors wired **directly to the Pi's 40-pin GPIO header** as documented in `HARDWARE_CONNECTIONS.md`:
  - LM393 light comparator module — DO → **GPIO 27** (powered from 3.3V)
  - HC-SR501 PIR Motion Sensor — OUT → **GPIO 17**
  - HC-SR04 Ultrasonic Sensor — Trigger → **GPIO 23**, Echo → **GPIO 24** *(via 1 kΩ / 2 kΩ voltage divider to step the 5V echo line down to 3.3V)*
  - *(Optional, for power telemetry)* INA219 power monitor inline on the Pi's 5V USB-C feed over I2C

> The sensors connect to the Pi directly — there is **no ESP32 in the live pipeline**. The original ESP32 firmware is retained under `firmware/` as legacy only and is not part of setup. The system runs **fully offline on the Pi alone** — no second machine is involved in live operation.

---

## Phase 1: Raspberry Pi OS Installation & Headless Setup

The Pi can be fully configured without an HDMI monitor, keyboard, or mouse.

### Step 1 — Download the Raspberry Pi Imager

Download and install the **Raspberry Pi Imager** for your operating system:

| Platform | Link |
|---|---|
| Windows | https://downloads.raspberrypi.com/imager/imager_latest.exe |
| macOS | https://downloads.raspberrypi.com/imager/imager_latest.dmg |
| Ubuntu | `sudo apt install rpi-imager` |

### Step 2 — Configure and Flash the OS

1. Insert your MicroSD card into your laptop.
2. Open Raspberry Pi Imager.
3. Click **Choose Device** → select **Raspberry Pi 4**.
4. Click **Choose OS** → choose based on how you'll view the demo GUI:
   - **Raspberry Pi OS (64-bit) with Desktop** — required if you want the on-Pi GUI window over **HDMI or VNC** (the window needs a display server).
   - **Raspberry Pi OS (64-bit) Lite** — no desktop; lighter (less CPU/RAM overhead). Use this only for **`--headless`** operation (terminal telemetry, no GUI).
5. Click **Choose Storage** → select your MicroSD card.
6. Click **Next**, then **Edit Settings** when prompted.

In the **OS Customisation** dialog, fill in:

**General tab:**
- Hostname: `raspberrypi.local`
- Username: `pi` (or a name of your choice — remember it for SSH)
- Password: choose a strong password
- Configure wireless LAN: enter your Wi-Fi SSID and password
- Wireless LAN country: your two-letter country code (e.g. `IN`, `US`, `GB`)

**Services tab:**
- Enable SSH: ✅
- Use password authentication: ✅

Click **Save**, then **Yes** to apply settings, then **Yes** again to confirm the write. Wait for the flash and verification to complete.

### Step 3 — First Boot

Eject the MicroSD card, insert it into the Raspberry Pi, and power the Pi on. Allow **60–90 seconds** for the first boot sequence to complete.

### Step 4 — Connect via SSH

**macOS / Linux / Ubuntu:**
```bash
ssh pi@raspberrypi.local
```

**Windows (PowerShell or Command Prompt):**
```cmd
ssh pi@raspberrypi.local
```

> **Windows note:** If `raspberrypi.local` does not resolve, install **Bonjour Print Services for Windows** (free, from Apple) and try again. Alternatively, find the Pi's assigned IP address in your router's device list and connect directly:
> ```cmd
> ssh pi@192.168.1.XX
> ```

Accept the host fingerprint prompt (type `yes` and press Enter), then enter your password.

---

## Phase 2: Raspberry Pi Software Setup

All commands in this phase are run inside your **Pi SSH session**.

### Step 1 — Update System Packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv python3-opencv git pigpio
```

### Step 1b — Enable the pigpio daemon

The Pi reads the sensors through `pigpiod`, which must be running for hardware-timestamped echo capture:

```bash
sudo systemctl enable --now pigpiod
```

Verify it is active with `systemctl status pigpiod` (look for `active (running)`).

### Step 2 — Clone the Repository

```bash
git clone https://github.com/HR-coding/SAGE-Vision.git
cd SAGE-Vision
```

### Step 3 — Create a Virtual Environment and Install Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r rpi_edge/requirements.txt
```

> The two model weights files (`yolov8n_320_int8.tflite` and `yolov8n_640_int8.tflite`) are already included in the repository under `rpi_edge/`. No separate download is needed.

> **Dependency note:** The edge node uses the lightweight **`tflite-runtime`** interpreter (installed by `requirements.txt`), **not** the full `ultralytics`/`torch` stack — those are too heavy for the Pi and are only used off-device to export the `.tflite` models. See `rpi_edge/yolo_tflite.py`.

### Step 4 — Verify GPIO access

The default `pi` user is already in the `gpio` group and can talk to `pigpiod` without `sudo`. Confirm the daemon is reachable:

```bash
python3 -c "import pigpio; p=pigpio.pi(); print('pigpio connected:', p.connected)"
```

It should print `pigpio connected: True`. If it prints `False`, start the daemon with `sudo systemctl start pigpiod` (see Phase 2, Step 1b).

---

## Phase 3: Configuration (optional)

By default the system needs **no network or laptop configuration** — telemetry prints to the Pi's own terminal and the demo GUI opens on the Pi's HDMI monitor. (Optional cloud streaming is covered at the end of this phase.)

The only thing you may need to change is the GPIO pin map, and only if you wired the sensors differently from the defaults. In your Pi session:

```bash
nano rpi_edge/pi_edge_node.py
```

The pin constants are at the top of `gpio_harvester_worker`:
```python
PIR_PIN  = 17   # PIR OUT
LDR_PIN  = 27   # LM393 DO (HIGH = dark on this module; see LDR_DARK_LEVEL)
TRIG_PIN = 23   # HC-SR04 TRIG
ECHO_PIN = 24   # HC-SR04 ECHO (via divider)
```
Edit them to match your wiring, then save: `Ctrl+O` → `Enter` → `Ctrl+X`.

### Optional — ThingSpeak cloud telemetry (`--cloud`)

By default the node is fully offline. To additionally stream telemetry to a [ThingSpeak](https://thingspeak.com) channel, set it up once:

1. **Create a ThingSpeak channel** with four fields, named to match the upload mapping:

   | Field | Name | Unit |
   |---|---|---|
   | Field 1 | Power | W |
   | Field 2 | Latency | ms |
   | Field 3 | CPU temp | °C |
   | Field 4 | Distance | cm |

2. **Copy the channel's Write API Key** (channel → *API Keys* tab).
3. **Create the `.env` file** on the Pi from the committed template and paste your key in. It can live in the **project root** or in **`rpi_edge/`** — the loader checks the root first, then `rpi_edge/`:
   ```bash
   cp .env.example .env     # project root (or: cp rpi_edge/.env.example rpi_edge/.env)
   nano .env                # set THINGSPEAK_API_KEY=<your write key>
   ```
   `.env` is git-ignored in both locations, so the key is never committed; `python-dotenv` (installed by `requirements.txt`) loads it at startup.
4. **Run with `--cloud`** (see Phase 4). Without the flag the node ignores ThingSpeak entirely; if `--cloud` is set but the key is missing, the node logs a warning and continues offline.

> The uploader posts once every 20 s (ThingSpeak's free tier permits one update per 15 s) on a background thread, so it never slows inference. Network failures are logged and skipped — they never crash the node. Metrics that are `None` in the current state (e.g. latency while idle) are simply left out of that update.

### Optional — detection snapshots (`--snapshots`)

Run with `--snapshots` to save a JPEG of the frame to `./snapshots/` (at the repo root) whenever a person is detected. No setup is needed — the folder is created at runtime and is git-ignored. Captures are cooldown-gated (one per ~30 s) and the folder is ring-buffered to a file cap, so the SD card can't fill. Each filename carries the timestamp, class, confidence, and FSM state.

> Like `--cloud`, this adds disk/CPU work, so keep it **off during measured benchmark runs** (see `TESTING.md`).

---

## Phase 4: Running the System

The node runs on the Pi alone. The demo GUI window needs a desktop display — view it on a **physical HDMI monitor** or remotely over **VNC** (pick one below). With no display at all, run `--headless` for terminal-only telemetry.

### Option A — HDMI monitor (simplest)

Connect a monitor to the Pi's micro-HDMI port and a keyboard, log in to the desktop, open a terminal, and run the node (Option C commands). The `SAGE-Vision` window appears on the monitor.

### Option B — VNC (remote desktop, no monitor)

Run the GUI on your own computer's screen by viewing the Pi's desktop over VNC. Requires **Raspberry Pi OS with Desktop** (see Phase 1).

1. **Enable the VNC server on the Pi** (over SSH):
   ```bash
   sudo raspi-config
   # Interface Options → VNC → Enable, then finish.
   ```
2. **Set a virtual display resolution** so VNC has a desktop to draw even with no monitor attached:
   ```bash
   sudo raspi-config
   # Display Options → VNC Resolution → e.g. 1280x720 → finish → reboot.
   ```
3. **Install a VNC viewer on your computer** — [RealVNC Viewer](https://www.realvnc.com/en/connect/download/viewer/) (Windows/macOS/Linux), free.
4. **Connect** the viewer to `raspberrypi.local` (or the Pi's IP) and log in with your Pi username/password. The Pi desktop appears in the viewer.
5. Open a **terminal inside the VNC desktop** and run the node (Option C commands). The `SAGE-Vision` window appears in the VNC session — keys `q`/`f` work the same.

> Do **not** launch the GUI from a plain `ssh` shell — it has no display and `imshow` will error. Use the terminal *inside* the HDMI or VNC desktop, or run `--headless`.

### Option C — run the node

From a terminal on the HDMI desktop or the VNC desktop (or any SSH shell if using `--headless`):

```bash
cd SAGE-Vision
source .venv/bin/activate
python3 rpi_edge/pi_edge_node.py             # demo GUI (HDMI or VNC desktop)
# or, with no display at all:
python3 rpi_edge/pi_edge_node.py --headless  # terminal telemetry only
# optional flags (combine with either of the above):
python3 rpi_edge/pi_edge_node.py --headless --cloud       # also stream telemetry to ThingSpeak (Phase 3)
python3 rpi_edge/pi_edge_node.py --headless --snapshots   # also save a JPEG to ./snapshots/ on a person detection
```

You should see the following boot messages confirming everything initialised correctly:

```
[INIT] Launching SAGE-Vision edge node (GUI enabled, cloud disabled, snapshots disabled)...
[SYSTEM] GPIO sensor harvester pinned to CPU core {1}
[SYSTEM] TFLite INT8 YOLO engines (320 + 640) initialized.
[SYSTEM] Vision engine pinned to CPU cores {2, 3}
[INIT] Launching 5-state FSM kernel loop...
```
(The `cloud`/`snapshots` flags in the first line reflect whether you passed `--cloud` / `--snapshots`.)

Telemetry begins printing to the terminal immediately — one line per loop, in every state. With the GUI enabled, the `SAGE-Vision` window shows the live feed with blue detection boxes and a HUD header bar. **Press `q` to quit cleanly, `f` to toggle fullscreen.**

### What to Expect During Live Operation

| You do this | Expected system behaviour |
|---|---|
| Walk in front of the camera | Pipeline switches from STANDBY to ACTIVE; inference starts |
| Step fully out of frame and wait 5 seconds | Pipeline returns to SLEEP; CPU usage drops sharply; GUI shows "SYSTEM IDLE" |
| Walk close to the camera (< 120 cm) | HUD shows `Model 320`; inference resolution drops to 320×320; latency decreases |
| Cover the LM393 light sensor | CLAHE filter activates on the frame before inference |

### Stopping the System

With the GUI: press **`q`** in the window. Otherwise (or in `--headless`): press **`Ctrl+C`** in the Pi terminal. Telemetry is printed live to the terminal; there is no file written, so nothing needs flushing on shutdown.

---

## Troubleshooting

### `raspberrypi.local` does not resolve (Windows)

Install **Bonjour Print Services for Windows** from Apple (`https://support.apple.com/kb/DL999`), which adds mDNS resolution. Alternatively, use the Pi's numeric IP address directly.

### SSH connection refused

The Pi may still be booting. Wait 90 seconds from power-on and retry. If it still fails, verify your Wi-Fi credentials were entered correctly during the imaging step — the Pi will not connect to the network if the SSID or password is wrong, and you will need to re-flash.

### `Cannot connect to pigpiod` on startup

The pigpio daemon is not running. Start it (and enable it at boot):
```bash
sudo systemctl enable --now pigpiod
```

### Distance readings are erratic or always out of range

Check the HC-SR04 ECHO voltage divider (1 kΩ / 2 kΩ) and that TRIG/ECHO are on the expected pins (GPIO 23 / 24). A missing divider can also damage the GPIO pin. Confirm the sensor has a solid 5V supply — it reads unreliably below ~3.8V.

### `ModuleNotFoundError` for `pigpio`, `cv2`, `psutil`, or `tflite_runtime`

The virtual environment is not active. Run `source .venv/bin/activate` (Linux/macOS) or `.venv\Scripts\Activate.ps1` (Windows) from the repo root before running any scripts.

### GUI window doesn't open / `cv2.error` about display or `imshow`

The GUI needs a display and the GUI build of OpenCV. Run it from a terminal **inside the Pi's HDMI or VNC desktop session** (see Phase 4), not a bare SSH shell — or pass `--headless` to skip the window. If you intentionally installed `opencv-python-headless`, the window is unavailable — either run `--headless`, or `pip install opencv-python`.

### Pipeline stays in SLEEP/STANDBY permanently despite motion

Confirm the PIR is wired to GPIO 17 and has finished its 30–60 s warm-up. Test the raw pin while the daemon runs:
```bash
python3 -c "import pigpio,time; p=pigpio.pi(); 
print([p.read(17) for _ in range(5)])"
```
Wave your hand in front of the PIR and you should see `1`s appear. If it never reads `1`, recheck the PIR's VCC (5V) and OUT wiring and its sensitivity potentiometer.
