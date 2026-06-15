import os
import sys
import time
import socket
import struct
import threading
import cv2
import numpy as np
import psutil
import pigpio
from yolo_tflite import YoloTFLite

# =========================================================================
# SECTION 1: GLOBAL STATE ENGINE CONFIGURATIONS & FINITE STATE MACHINE TOKENS
# =========================================================================

# Explicit Finite State Machine tokens representing the 5 core operational states
STATE_SLEEP     = "SLEEP"
STATE_STANDBY   = "STANDBY"
STATE_ACTIVE_LO = "ACTIVE-LO"
STATE_ACTIVE_HI = "ACTIVE-HI"
STATE_WATCHDOG  = "WATCHDOG"

# Initialize thread-safe shared state cache dictionary. The LDR is now a digital
# LM393 comparator (active-low), so light is a boolean dark/bright flag rather
# than an analog value.
shared_state = {
    "pir": 0,
    "distance": 400.0,  # Bounded initialized safe default far distance value
    "is_dark": False,
    "last_motion_epoch": time.time()
}
state_mutex = threading.Lock()

# Rolling buffer array to compute a true median filter for the ultrasonic metrics
DISTANCE_WINDOW_SIZE = 5
distance_history = [400.0] * DISTANCE_WINDOW_SIZE

# Heartbeat line tracking the exact epoch of the last completed ultrasonic echo
last_valid_reading_time = time.time()

# Failsafe limit: if 3 consecutive sonar triggers return no echo, trigger WATCHDOG
MAX_CONSECUTIVE_DROPS = 3
consecutive_dropped_readings = 0

# Network routing parameters
LAPTOP_SERVER_IP = "192.168.1.50"   # ⚠️ UPDATE with your laptop's actual IP address
NETWORK_PORT = 8080

# Mode ID mapping table for the 40-byte packed UDP payload network socket [cite: 177]
MODE_IDS = {
    STATE_SLEEP:     0,
    STATE_STANDBY:   1,
    STATE_ACTIVE_LO: 2,
    STATE_ACTIVE_HI: 3,
    STATE_WATCHDOG:  4
}

# Initialize local quantized TFLite engines. A full-integer INT8 model has a
# fixed input resolution, so adaptive switching uses two interpreters: one at
# 320x320 for ACTIVE-LO and one at 640x640 for ACTIVE-HI / WATCHDOG. [cite: 174]
_MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_LO_PATH = os.path.join(_MODEL_DIR, "yolov8n_320_int8.tflite")
MODEL_HI_PATH = os.path.join(_MODEL_DIR, "yolov8n_640_int8.tflite")
try:
    model_lo = YoloTFLite(MODEL_LO_PATH)  # 320x320
    model_hi = YoloTFLite(MODEL_HI_PATH)  # 640x640
    print("[SYSTEM] TFLite INT8 YOLO engines (320 + 640) successfully initialized.", flush=True)
except Exception as e:
    print(f"[FATAL] Failed to locate or parse TFLite model file: {e}", flush=True)
    sys.exit(1)


def get_pi_hardware_metrics():
    """Reads internal Pi telemetry sensors for project evaluation data."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp_raw = int(f.read())
        cpu_temp = temp_raw / 1000.0
    except Exception:
        cpu_temp = 0.0
        
    cpu_usage = psutil.cpu_percent(interval=None)
    return cpu_usage, cpu_temp


# =========================================================================
# THREAD 1: ASYNCHRONOUS GPIO SENSOR HARVESTER (PINNED TO CPU CORE 1)
# =========================================================================
# Hardware GPIO pin map (BCM numbering). The HC-SR04 ECHO line is 5V and MUST
# pass through a resistor voltage divider down to 3.3V before reaching the Pi.
PIR_PIN  = 17   # PIR digital motion output
LDR_PIN  = 27   # LM393 light comparator digital output (active-low: LOW = dark)
TRIG_PIN = 23   # HC-SR04 trigger output
ECHO_PIN = 24   # HC-SR04 echo input (via 5V -> 3.3V voltage divider)

# Ultrasonic timing constants mirroring the original 16.6 Hz firmware cadence
TRIGGER_INTERVAL_S = 0.060   # ~16.6 Hz sonar trigger pace
ECHO_TIMEOUT_S     = 0.058   # TUNABLE: no echo within this window => dead sonar.
                             # Must exceed your HC-SR04's no-object pulse length
                             # (~38 ms datasheet; some clones stretch longer).

# Echo pulse state shared between the pigpio edge callback and the harvester loop.
# pigpio fires the callback from its own real-time thread (the equivalent of the
# ESP32 IRAM ISR), so reads/writes are guarded by echo_lock (the critical section).
echo_lock = threading.Lock()
_echo_start_tick = 0
_echo_pulse_ready = False
_echo_duration_us = 0

# Set when a pigpio/daemon call fails -> forces an immediate WATCHDOG (the
# equivalent of the old "USB serial unplugged" detection).
pigpio_fault = False


def _echo_edge_callback(gpio, level, tick):
    """pigpio EITHER_EDGE callback: timestamps the HC-SR04 echo pulse width.

    Mirrors the firmware ISR: rising edge stamps the start tick, falling edge
    computes the pulse duration. pigpio.tickDiff handles the 32-bit microsecond
    counter wraparound correctly.
    """
    global _echo_start_tick, _echo_pulse_ready, _echo_duration_us
    with echo_lock:
        if level == 1:            # rising edge: pulse start
            _echo_start_tick = tick
        elif level == 0:          # falling edge: pulse end
            if _echo_start_tick != 0:
                _echo_duration_us = pigpio.tickDiff(_echo_start_tick, tick)
                _echo_start_tick = 0
                _echo_pulse_ready = True


def gpio_harvester_worker():
    # Pin this thread to CPU Core 1 to isolate sensor sampling from inference jitter
    os.sched_setaffinity(0, {1})
    print(f"[SYSTEM] GPIO Sensor Harvester pinned to CPU Core {os.sched_getaffinity(0)}", flush=True)

    global last_valid_reading_time, distance_history, consecutive_dropped_readings
    global pigpio_fault, _echo_pulse_ready, _echo_duration_us

    pi = pigpio.pi()  # connects to the local pigpiod daemon
    if not pi.connected:
        print("[FATAL] Cannot connect to pigpiod. Start it with 'sudo pigpiod'.", flush=True)
        sys.exit(1)

    pi.set_mode(TRIG_PIN, pigpio.OUTPUT)
    pi.set_mode(ECHO_PIN, pigpio.INPUT)
    pi.set_mode(PIR_PIN, pigpio.INPUT)
    pi.set_mode(LDR_PIN, pigpio.INPUT)
    pi.write(TRIG_PIN, 0)
    pi.callback(ECHO_PIN, pigpio.EITHER_EDGE, _echo_edge_callback)

    last_trigger = 0.0
    awaiting_echo = False
    trigger_sent_at = 0.0

    while True:
        try:
            now = time.monotonic()

            # 1. Non-blocking periodic 10us trigger pulse (mirrors firmware cadence)
            if now - last_trigger >= TRIGGER_INTERVAL_S:
                last_trigger = now
                pi.gpio_trigger(TRIG_PIN, 10, 1)   # 10 microsecond HIGH pulse
                awaiting_echo = True
                trigger_sent_at = now

            # 2. Consume a completed echo transaction if one is ready
            with echo_lock:
                pulse_ready = _echo_pulse_ready
                duration_us = _echo_duration_us
                _echo_pulse_ready = False

            if pulse_ready:
                awaiting_echo = False
                dist_cm = (duration_us * 0.0343) / 2.0
                # Outlier rejection + out-of-range sentinel (identical to firmware)
                if dist_cm > 500.0 or dist_cm <= 0:
                    dist_cm = -1.0
                # A COMPLETED echo (even out-of-range) is a valid heartbeat: the
                # sonar is alive, so refresh the watchdog timer and clear drops.
                with state_mutex:
                    if dist_cm > 0:
                        distance_history.pop(0)
                        distance_history.append(dist_cm)
                        # True median removes spikes from ultrasonic echo multipath
                        shared_state["distance"] = float(np.median(distance_history))
                    consecutive_dropped_readings = 0
                last_valid_reading_time = time.time()

            # 3. Sonar liveness: NO echo edges at all within the window => dead sensor
            elif awaiting_echo and (now - trigger_sent_at) > ECHO_TIMEOUT_S:
                awaiting_echo = False
                with state_mutex:
                    consecutive_dropped_readings += 1

            # 4. Poll the level-read sensors (PIR motion, LM393 light). These are
            #    not health-monitorable: a dead level pin reads a constant value
            #    indistinguishable from a genuinely quiet/lit room.
            pir_val = pi.read(PIR_PIN)
            is_dark = (pi.read(LDR_PIN) == 0)   # active-low: LOW output = dark
            with state_mutex:
                shared_state["pir"] = pir_val
                shared_state["is_dark"] = is_dark
                if pir_val == 1:
                    shared_state["last_motion_epoch"] = time.time()

            pigpio_fault = False
            time.sleep(0.01)   # light yield; pigpiod captures echo edges independently

        except Exception as loop_fault:
            # A pigpio call failed (e.g. pigpiod died) -> flag immediate failsafe
            pigpio_fault = True
            with state_mutex:
                consecutive_dropped_readings += 1
            print(f"[SENSOR ALERT] GPIO read fault or pigpiod connection lost: {loop_fault}", flush=True)
            time.sleep(0.2)


# =========================================================================
# THREAD 2: 5-STATE ADAPTIVE INFERENCE ENGINE (PINNED TO CPU CORES 2 & 3)
# =========================================================================
def adaptive_vision_streamer():
    # Pin the vision thread to CPU Cores 2 and 3 to maximize parallel frame math handling
    os.sched_setaffinity(0, {2, 3})
    print(f"[SYSTEM] Vision processing core engine pinned to CPU Cores {os.sched_getaffinity(0)}", flush=True)
    
    # Establish persistent telemetry socket link to Laptop using UDP
    network_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    camera_feed = cv2.VideoCapture(0)
    camera_feed.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera_feed.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    
    # Unified Packed Binary Output Definition: Exactly 40 Bytes structural space [cite: 177]
    udp_format = "<B f f f H f B 4B 4f"
    
    # Internal FSM state tracking variables
    current_state = STATE_SLEEP
    standby_ticks_start = 0.0
    clahe_active = False
    
    print("[INIT] Launching 5-State Finite State Machine Kernel Loop...", flush=True)
    
    while True:
        # -----------------------------------------------------------------
        # STEP A: WATCHDOG FAILSAFE OVERRIDE RULE EVALUATION (ENHANCED)
        # -----------------------------------------------------------------
        time_since_last_valid_read = time.time() - last_valid_reading_time

        with state_mutex:
            drops_count = consecutive_dropped_readings

        # Failsafe triggers on: lost pigpiod connection, 2s with no valid echo,
        # OR consecutive sonar echo timeouts hitting the drop threshold.
        if pigpio_fault or time_since_last_valid_read > 2.0 or drops_count >= MAX_CONSECUTIVE_DROPS:
            if current_state != STATE_WATCHDOG:
                print(f"[WATCHDOG CRITICAL] Sensor feed compromised! "
                      f"(pigpio_fault={pigpio_fault}, elapsed: {time_since_last_valid_read:.1f}s, "
                      f"consecutive drops: {drops_count}). Entering Failsafe Mode.", flush=True)
                current_state = STATE_WATCHDOG
        elif current_state == STATE_WATCHDOG:
            print("[WATCHDOG RESOLVED] Heartbeat restored. Returning to STANDBY.", flush=True)
            current_state = STATE_STANDBY
            standby_ticks_start = time.time()

        # -----------------------------------------------------------------
        # STEP B: EXTRACT MUTEX-LOCKED TELEMETRY & PRE-CALCULATE CONDITIONS
        # -----------------------------------------------------------------
        with state_mutex:
            pir = shared_state["pir"]
            distance = shared_state["distance"]
            is_dark = shared_state["is_dark"]
            last_motion = shared_state["last_motion_epoch"]
            
        # Presence calculations: PIR expired (5s window) AND filtered distance > 350 cm
        pir_expired = (time.time() - last_motion) >= 5.0
        presence_lost = pir_expired and (distance > 350.0)
        presence_confirmed = not presence_lost

        # -----------------------------------------------------------------
        # STEP C: FSM STATE TRANSITION HANDLING MATRIX
        # -----------------------------------------------------------------
        if current_state == STATE_SLEEP:
            # Wake condition: any new PIR movement OR target steps inside the 350cm boundary
            if pir == 1 or distance < 350.0:
                current_state = STATE_STANDBY
                standby_ticks_start = time.time()  # Start the 200 ms warmup timer hook
                print(f"[FSM TRANSITION] SLEEP -> STANDBY. Initiating warmup tick.", flush=True)

        elif current_state == STATE_STANDBY:
            # Hold transient state for exactly one 200 ms execution window tick
            if (time.time() - standby_ticks_start) >= 0.200:
                if distance < 120.0:
                    current_state = STATE_ACTIVE_LO
                elif distance >= 160.0:
                    current_state = STATE_ACTIVE_HI
                else:
                    current_state = STATE_ACTIVE_HI  # Default to high-res mode inside overlap zone
                print(f"[FSM TRANSITION] STANDBY -> {current_state} based on target distance: {distance:.1f}cm", flush=True)

        elif current_state == STATE_ACTIVE_LO:
            if presence_lost:
                current_state = STATE_SLEEP
                print("[FSM TRANSITION] ACTIVE-LO -> SLEEP. Target vacancy confirmed.", flush=True)
            elif distance > 160.0:  # Hysteresis exit boundary constraint check
                current_state = STATE_ACTIVE_HI
                print(f"[FSM TRANSITION] ACTIVE-LO -> ACTIVE-HI. Target exited close-zone threshold (> 160cm).", flush=True)

        elif current_state == STATE_ACTIVE_HI:
            if presence_lost:
                current_state = STATE_SLEEP
                print("[FSM TRANSITION] ACTIVE-HI -> SLEEP. Target vacancy confirmed.", flush=True)
            elif distance < 120.0:  # Hysteresis entry boundary constraint check
                current_state = STATE_ACTIVE_LO
                print(f"[FSM TRANSITION] ACTIVE-HI -> ACTIVE-LO. Target entered close-zone threshold (< 120cm).", flush=True)

        # -----------------------------------------------------------------
        # STEP D: ORTHOGONAL LDR PREPROCESSING GATE EVALUATION
        # -----------------------------------------------------------------
        # The LM393 comparator supplies a clean digital dark/bright signal; its
        # onboard potentiometer threshold and built-in comparator hysteresis now
        # replace the former software dead-band entirely.
        if current_state == STATE_WATCHDOG:
            clahe_active = True  # Watchdog assumes worst-case low light automatically
        else:
            clahe_active = is_dark

        # -----------------------------------------------------------------
        # STEP E: OUTPUT PROFILE EXECUTION BASED ON CURRENT DRIVING STATE
        # -----------------------------------------------------------------
        mode_id = MODE_IDS[current_state]

        # Handle non-inference states (SLEEP & STANDBY) to maximize power savings
        if current_state in [STATE_SLEEP, STATE_STANDBY]:
            cpu_pct, cpu_c = get_pi_hardware_metrics()
            # Send status update packet containing 0 tracked objects
            status_payload = struct.pack(
                udp_format, 
                mode_id, cpu_pct, cpu_c, distance, 0, 0.0, 
                0, 255, 255, 255, 255, 0.0, 0.0, 0.0, 0.0
            )
            try:
                network_socket.sendto(status_payload, (LAPTOP_SERVER_IP, NETWORK_PORT))
            except Exception: pass
            
            # Pacing adjustment: deep sleep if sleeping, rapid loop if warming up
            time.sleep(1.0 if current_state == STATE_SLEEP else 0.05)
            continue

        # Inference Processing States (ACTIVE-LO, ACTIVE-HI, WATCHDOG)
        success, raw_frame = camera_feed.read()
        if not success or raw_frame is None:
            continue

        # Apply orthogonal image preprocessing layers if activated [cite: 359]
        if clahe_active:
            yuv_buffer = cv2.cvtColor(raw_frame, cv2.COLOR_BGR2YUV)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
            yuv_buffer[:, :, 0] = clahe.apply(yuv_buffer[:, :, 0])
            processed_frame = cv2.cvtColor(yuv_buffer, cv2.COLOR_YUV2BGR)
        else:
            processed_frame = raw_frame

        # Resolve resolution model, size constraints and pacing rates [cite: 358, 360]
        if current_state == STATE_ACTIVE_LO:
            detector = model_lo
            img_inference_size = 320
            loop_pacing_rate = 0.01  # Target close: run highly responsive loop pacing
        elif current_state == STATE_ACTIVE_HI:
            detector = model_hi
            img_inference_size = 640
            loop_pacing_rate = 0.40  # Target far: slow down pacing to preserve resources
        else:  # STATE_WATCHDOG
            detector = model_hi
            img_inference_size = 640
            loop_pacing_rate = 0.10  # Failsafe fallback loop pacing rate

        # Run model inference and track math execution performance times
        start_inference = time.time()
        detected_classes, detected_confidences = detector(processed_frame)
        inference_duration_ms = (time.time() - start_inference) * 1000.0

        detection_count = min(len(detected_classes), 4)
        padded_classes = [255, 255, 255, 255]
        padded_confidences = [0.0, 0.0, 0.0, 0.0]
        
        for i in range(detection_count):
            padded_classes[i] = detected_classes[i]
            padded_confidences[i] = detected_confidences[i]

        cpu_pct, cpu_c = get_pi_hardware_metrics()

        # Compile and serialize the 40-byte binary packet structures [cite: 177]
        active_payload = struct.pack(
            udp_format,
            mode_id, cpu_pct, cpu_c, distance, img_inference_size, inference_duration_ms,
            detection_count, *padded_classes, *padded_confidences
        )

        try:
            network_socket.sendto(active_payload, (LAPTOP_SERVER_IP, NETWORK_PORT))
        except Exception as tx_err:
            print(f"[NET ERROR] Log frame dropped: {tx_err}", flush=True)

        time.sleep(loop_pacing_rate)


if __name__ == "__main__":
    print("[INIT] Launching 5-State Adaptive Video Edge Node Framework...", flush=True)
    harvester_thread = threading.Thread(target=gpio_harvester_worker, daemon=True)
    vision_thread = threading.Thread(target=adaptive_vision_streamer, daemon=True)
    
    harvester_thread.start()
    vision_thread.start()
    
    while True:
        time.sleep(1)