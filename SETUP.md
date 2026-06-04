# SAGE-Vision — Setup & Execution Guide

This document covers everything from flashing the OS to running the live pipeline. Follow all five phases in order on your first setup.

---

## Hardware Requirements

### Raspberry Pi Node
- Raspberry Pi 4B (2 GB RAM minimum; 4 GB recommended)
- MicroSD card (16 GB minimum, Class 10 / A1 rated)
- USB Web Camera (UVC-compliant; plug-and-play, no drivers needed)
- **RV-IoT Board (ESP32 Dev Module core)** with the following sensors wired up as documented in `HARDWARE_CONNECTIONS.md`:
  - LDR (Photoresistor) — pre-wired on-board to **GPIO 39**
  - HC-SR501 PIR Motion Sensor — **GPIO 25**
  - HC-SR04 Ultrasonic Sensor — Trigger → **GPIO 26**, Echo → **GPIO 27** *(via 1 kΩ / 2 kΩ voltage divider to step the 5V echo line down to 3.3V)*

### Host Laptop
- Windows 10/11, macOS 12+, or Ubuntu 20.04+
- Python 3.9 or newer
- Connected to the **same local Wi-Fi or Ethernet subnet** as the Raspberry Pi

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
4. Click **Choose OS** → select **Raspberry Pi OS (64-bit) Lite** *(the server variant with no desktop environment — strongly recommended to reduce CPU and memory overhead)*.
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

## Phase 2: ESP32 Firmware Flash (RV-IoT Board)

Do this **before** physically wiring the sensors to the board.

### Step 1 — Install Arduino IDE

Download **Arduino IDE 2.x** from `https://www.arduino.cc/en/software` and install it on your laptop.

### Step 2 — Add the ESP32 Board Package

1. Open Arduino IDE.
2. Go to **File → Preferences** (macOS: **Arduino IDE → Settings**).
3. In the *Additional Boards Manager URLs* field, add the following URL (append with a comma if other URLs already exist):
   ```
   https://dl.espressif.com/dl/package_esp32_index.json
   ```
4. Click **OK**.
5. Go to **Tools → Board → Boards Manager**.
6. Search for `esp32`, find the package by **Espressif Systems**, and click **Install**.

### Step 3 — Connect the RV-IoT Board

Connect the RV-IoT Board to your laptop using a **data-capable** Micro-USB cable. Charge-only cables will not work — the board will power up but the serial port will not appear.

### Step 4 — Configure Upload Settings

In Arduino IDE, set the following under the **Tools** menu:

| Setting | Value |
|---|---|
| Board | ESP32 Dev Module |
| Upload Speed | 921600 |
| CPU Frequency | 240MHz (WiFi/BT) |
| Flash Frequency | 80MHz |
| Flash Mode | QIO |
| Flash Size | 4MB (32Mb) |
| Partition Scheme | Default 4MB with spiffs |
| Port | See below |

**Selecting the port:**

- **Windows:** Look for `COM3`, `COM4`, or similar under **Tools → Port**. If multiple ports appear, unplug the board, note which ones remain, replug, and select the new one.
- **macOS:** Look for `/dev/cu.usbserial-XXXX` or `/dev/cu.SLAB_USBtoUART`.
- **Linux/Ubuntu:** Look for `/dev/ttyUSB0` or `/dev/ttyACM0`. If nothing appears, run:
  ```bash
  sudo usermod -aG dialout $USER
  # Log out and back in, then retry
  ```

### Step 5 — Upload the Firmware

1. Open `firmware/esp32_sensor_node/esp32_sensor_node.ino` in Arduino IDE.
2. Click the **Upload** (→) arrow button.

> **If the upload hangs at "Connecting…":** Hold down the **BOOT (FLASH)** button on the board, press and release **EN (RST)**, then release **BOOT** just as the IDE prints `Connecting......`. This manually triggers flash mode.

3. Wait until the status bar reads **Done uploading**.

### Step 6 — Verify (Optional)

Open **Tools → Serial Monitor** and set the baud rate to **115200**. You will not see readable text — the firmware transmits raw binary data, which is expected. If the monitor shows garbled characters, the firmware is running correctly.

---

## Phase 3: Raspberry Pi Software Setup

All commands in this phase are run inside your **Pi SSH session**.

### Step 1 — Update System Packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv python3-opencv git
```

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

> The model weights file (`yolov8n_full_integer_quant.tflite`) is already included in the repository under `rpi_edge/`. No separate download is needed.

> **Dependency note:** Do **not** separately install `tflite-runtime`. The `ultralytics` package manages TFLite inference internally, and installing `tflite-runtime` alongside it will cause import conflicts on the Pi's aarch64 architecture.

### Step 4 — Identify the ESP32 Serial Port

Plug the RV-IoT Board into one of the Pi's USB ports, then run:

```bash
dmesg | grep tty
```

Look for a line mentioning `ttyUSB0` or `ttyACM0`. Note the exact path — you will need it in Phase 5.

If running the script later gives a serial permission error:
```bash
sudo usermod -aG dialout $USER
newgrp dialout    # Apply without requiring a logout
```

---

## Phase 4: Host Laptop Software Setup

Open a **new terminal window on your laptop** (separate from the SSH session).

### Step 1 — Clone the Repository

**macOS / Linux / Ubuntu:**
```bash
git clone https://github.com/HR-coding/SAGE-Vision.git
cd SAGE-Vision
python3 -m venv .venv
source .venv/bin/activate
pip install -r host_laptop/requirements.txt
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/HR-coding/SAGE-Vision.git
cd SAGE-Vision
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r host_laptop\requirements.txt
```

> **Windows execution policy note:** If PowerShell blocks the activation script, run this first:
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> ```

> **`host_laptop/requirements.txt` note:** This file should contain `pandas`, `matplotlib`, and `numpy`. If it is missing from the repository, install manually:
> ```bash
> pip install pandas matplotlib numpy
> ```

### Step 2 — Find Your Laptop's Local IP Address

You need this so the Pi knows where to send UDP packets.

**Windows:**
```cmd
ipconfig
```
Look for **IPv4 Address** under your active adapter (Wi-Fi or Ethernet). It will look like `192.168.x.x` or `10.0.x.x`.

**macOS:**
```bash
ipconfig getifaddr en0       # Wi-Fi
ipconfig getifaddr en1       # Ethernet (if Wi-Fi is en0)
```

**Linux / Ubuntu:**
```bash
ip route get 1 | awk '{print $7; exit}'
```

Write this IP address down — you will enter it in the next phase.

---

## Phase 5: Network & Script Configuration

Back in your **Pi SSH session**, open the edge script for editing:

```bash
nano rpi_edge/pi_edge_node.py
```

Find the configuration block near the top of the file and make the following two changes:

**1. Set your laptop's IP address:**
```python
LAPTOP_SERVER_IP = "192.168.1.XX"   # ← Replace with your actual laptop IP
```

**2. Set the correct serial port for the ESP32:**
```python
SERIAL_INTERFACE = "/dev/ttyUSB0"   # ← Change to /dev/ttyACM0 if that is what dmesg showed
```

Save the file: `Ctrl+O` → `Enter` → `Ctrl+X`.

---

## Phase 6: Running the System

Open your workspace with **two terminal windows** side by side.

### Terminal A — Host Laptop Logger (start this first)

The laptop must be listening before the Pi starts transmitting, or the first few packets will be dropped.

**macOS / Linux / Ubuntu:**
```bash
cd SAGE-Vision
source .venv/bin/activate
python3 host_laptop/laptop_logger.py
```

**Windows:**
```powershell
cd SAGE-Vision
.venv\Scripts\Activate.ps1
python host_laptop\laptop_logger.py
```

You will see a dashboard header printed to the terminal. The logger will now wait silently for incoming packets.

### Terminal B — Pi Edge Node (SSH)

```bash
ssh pi@raspberrypi.local
cd SAGE-Vision
source .venv/bin/activate
python3 rpi_edge/pi_edge_node.py
```

You should see the following boot messages confirming everything initialised correctly:

```
[SYSTEM] TFLite INT8 YOLO engine successfully initialized.
[SYSTEM] Serial Harvester thread successfully pinned to CPU Core {1}
[SYSTEM] Vision processing core engine pinned to CPU Cores {2, 3}
```

The laptop terminal will immediately begin displaying live telemetry rows.

### What to Expect During Live Operation

| You do this | Expected system behaviour |
|---|---|
| Walk in front of the camera | Pipeline switches from STANDBY to ACTIVE; inference starts |
| Step fully out of frame and wait 5 seconds | Pipeline returns to STANDBY; CPU usage drops sharply |
| Walk close to the camera (< 150 cm) | Inference resolution drops to 320×320; latency decreases |
| Cover the LDR on the RV-IoT Board | CLAHE filter activates on the frame before inference |

### Stopping the System

Press `Ctrl+C` in **both** Terminal A and Terminal B. The CSV log file is written incrementally, so no data is lost on clean shutdown.

Log files are saved at:
- **Laptop:** `host_laptop/pi_optimization_logs.csv`

---

## Troubleshooting

### `raspberrypi.local` does not resolve (Windows)

Install **Bonjour Print Services for Windows** from Apple (`https://support.apple.com/kb/DL999`), which adds mDNS resolution. Alternatively, use the Pi's numeric IP address directly.

### SSH connection refused

The Pi may still be booting. Wait 90 seconds from power-on and retry. If it still fails, verify your Wi-Fi credentials were entered correctly during the imaging step — the Pi will not connect to the network if the SSID or password is wrong, and you will need to re-flash.

### `SerialException` / permission denied on serial port

```bash
sudo usermod -aG dialout $USER
newgrp dialout
```

### ESP32 not appearing as a serial device

Swap your Micro-USB cable for one you know supports data transfer. Charge-only cables are extremely common and look identical. After swapping, unplug and replug the board.

### `ModuleNotFoundError` for `ultralytics`, `cv2`, or `psutil`

The virtual environment is not active. Run `source .venv/bin/activate` (Linux/macOS) or `.venv\Scripts\Activate.ps1` (Windows) from the repo root before running any scripts.

### Laptop receives no UDP packets

1. Confirm both devices are on the same subnet — they must be connected to the same router, not one on Wi-Fi and one on a separate Ethernet switch with no shared gateway.
2. Double-check `LAPTOP_SERVER_IP` in `pi_edge_node.py` — a single wrong digit will silently drop all packets.
3. **Windows Firewall** commonly blocks inbound UDP. Either temporarily disable the firewall for testing, or add an inbound rule: *Windows Defender Firewall → Advanced Settings → Inbound Rules → New Rule → Port → UDP → 8080 → Allow the connection*.

### Pipeline stays in STANDBY permanently despite motion

This is caused by the serial protocol mismatch bug described in `CODE_ISSUES_AND_FIXES.md`. The `serial_harvester_worker` in the original code uses `readline()` on a binary serial stream, which blocks indefinitely and never updates the shared PIR state. Apply the fix in that document before running the system.
