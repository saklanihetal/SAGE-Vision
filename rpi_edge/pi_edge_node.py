import glob
import os
import sys
import time
import serial
import socket
import struct
import threading
import cv2
import numpy as np
import psutil
from ultralytics import YOLO

# =========================================================================
# SECTION 1: GLOBAL STATE ENGINE CONFIGURATIONS & FINITE STATE MACHINE TOKENS
# =========================================================================

# Explicit Finite State Machine tokens representing the 5 core operational states
STATE_SLEEP     = "SLEEP"
STATE_STANDBY   = "STANDBY"
STATE_ACTIVE_LO = "ACTIVE-LO"
STATE_ACTIVE_HI = "ACTIVE-HI"
STATE_WATCHDOG  = "WATCHDOG"

# Initialize thread-safe shared state cache dictionary
shared_state = {
    "pir": 0,
    "distance": 400.0,  # Bounded initialized safe default far distance value
    "ldr": 500,
    "last_motion_epoch": time.time()
}
state_mutex = threading.Lock()

# Rolling buffer array to compute a true median filter for the ultrasonic metrics
DISTANCE_WINDOW_SIZE = 5
distance_history = [400.0] * DISTANCE_WINDOW_SIZE

# Heartbeat line tracking the exact epoch of the last SUCCESSFULLY parsed sensor packet
last_valid_reading_time = time.time()

# Failsafe limit: if we drop 3 consecutive packet reads, trigger WATCHDOG immediately
MAX_CONSECUTIVE_DROPS = 3
consecutive_dropped_readings = 0

# Network routing parameters and fallback hardware ports
SERIAL_INTERFACE = "/dev/ttyUSB0"  
LAPTOP_SERVER_IP = "192.168.1.50"   # ⚠️ UPDATE with your laptop's actual IP address
NETWORK_PORT = 8080

# Fixed Struct Packing Definition for Incoming Sensor Data (Exactly 7 Bytes) [cite: 354, 355]
SENSOR_PACKET_FMT  = "<B H f"   # little-endian: uint8 (PIR), uint16 (LDR), float (Distance)
SENSOR_PACKET_SIZE = struct.calcsize(SENSOR_PACKET_FMT)

# Mode ID mapping table for the 40-byte packed UDP payload network socket [cite: 177]
MODE_IDS = {
    STATE_SLEEP:     0,
    STATE_STANDBY:   1,
    STATE_ACTIVE_LO: 2,
    STATE_ACTIVE_HI: 3,
    STATE_WATCHDOG:  4
}

# Initialize local quantized TFLite engine [cite: 174]
try:
    yolo_model = YOLO("yolov8n_full_integer_quant.tflite", task="detect")
    print("[SYSTEM] TFLite INT8 YOLO engine successfully initialized.", flush=True)
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
# THREAD 1: ASYNCHRONOUS USB SERIAL HARVESTER (PINNED TO CPU CORE 1)
# =========================================================================
def find_esp32_serial_port():
    """Auto-detect the ESP32 serial port from common Linux device paths."""
    candidates = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
    if candidates:
        port = sorted(candidates)[0]
        print(f"[SYSTEM] Auto-detected ESP32 serial port: {port}", flush=True)
        return port
    return SERIAL_INTERFACE


def serial_harvester_worker():
    # Pin this thread to CPU Core 1 to isolate UART serial parsing operations completely
    os.sched_setaffinity(0, {1})
    print(f"[SYSTEM] Serial Harvester pinned to CPU Core {os.sched_getaffinity(0)}", flush=True)

    try:
        ser = serial.Serial(find_esp32_serial_port(), 115200, timeout=1)
        ser.reset_input_buffer()
    except Exception as hardware_err:
        print(f"[FATAL] USB Serial unreadable at {find_esp32_serial_port()}: {hardware_err}", flush=True)
        sys.exit(1)

    global shared_state, last_valid_reading_time, distance_history, consecutive_dropped_readings
    while True:
        try:
            # Read exactly 7 bytes matching the layout of your packed SensorPacket
            raw_bytes = ser.read(SENSOR_PACKET_SIZE)
            
            # DROPPED ENHANCEMENT: Catch incomplete or timed-out packet readings
            if len(raw_bytes) != SENSOR_PACKET_SIZE:
                with state_mutex:
                    consecutive_dropped_readings += 1
                continue  # Skip processing line silently

            # Successful byte harvest, proceed to unpack binary network structures
            pir_val, ldr_val, dist_val = struct.unpack(SENSOR_PACKET_FMT, raw_bytes)

            with state_mutex:
                shared_state["pir"] = pir_val
                shared_state["ldr"] = ldr_val
                
                # Apply incoming values to rolling window filter history array if reading is valid
                if dist_val > 0:
                    distance_history.pop(0)
                    distance_history.append(dist_val)
                    # Extract the true median to remove spikes caused by ultrasonic echoing anomalies
                    shared_state["distance"] = float(np.median(distance_history))
                    
                if pir_val == 1:
                    shared_state["last_motion_epoch"] = time.time()

                # Reset consecutive drop counter back to 0 since a valid packet was completely parsed
                consecutive_dropped_readings = 0

            # Update the heartbeat tracker to the current valid execution time epoch
            last_valid_reading_time = time.time()

        except Exception as loop_fault:
            # Track any unexpected decoding exceptions or string bit corruptions
            with state_mutex:
                consecutive_dropped_readings += 1
            print(f"[SERIAL ALERT] Intermittent telemetry loss or corrupted packet: {loop_fault}", flush=True)
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

        # Failsafe triggers if 2 seconds pass with no data OR if consecutive dropped packets hit the threshold
        if time_since_last_valid_read > 2.0 or drops_count >= MAX_CONSECUTIVE_DROPS:
            if current_state != STATE_WATCHDOG:
                print(f"[WATCHDOG CRITICAL] Sensor stream compromised! "
                      f"(Time elapsed: {time_since_last_valid_read:.1f}s, Consecutive drops: {drops_count}). "
                      f"Entering Failsafe Mode.", flush=True)
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
            ldr = shared_state["ldr"]
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
        # STEP D: ASYMMETRIC ORTHOGONAL LDR PREPROCESSING GATE EVALUATION
        # -----------------------------------------------------------------
        if current_state == STATE_WATCHDOG:
            clahe_active = True  # Watchdog assumes worst-case low light automatically
        else:
            if ldr < 350:
                clahe_active = True
            elif ldr > 420:
                clahe_active = False

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

        # Resolve resolution size constraints and pacing rates [cite: 358, 360]
        if current_state == STATE_ACTIVE_LO:
            img_inference_size = 320
            loop_pacing_rate = 0.01  # Target close: run highly responsive loop pacing
        elif current_state == STATE_ACTIVE_HI:
            img_inference_size = 640
            loop_pacing_rate = 0.40  # Target far: slow down pacing to preserve resources
        else:  # STATE_WATCHDOG
            img_inference_size = 640
            loop_pacing_rate = 0.10  # Failsafe fallback loop pacing rate

        # Run model inference and track math execution performance times
        start_inference = time.time()
        inference_results = yolo_model(processed_frame, imgsz=img_inference_size, verbose=False)
        inference_duration_ms = (time.time() - start_inference) * 1000.0

        # Extract and compile target predictions safely
        detected_classes = []
        detected_confidences = []
        
        for result in inference_results:
            if result.boxes is not None and len(result.boxes) > 0:
                tensor_classes = result.boxes.cls.cpu().int().tolist()
                tensor_confs = result.boxes.conf.cpu().float().tolist()
                detected_classes.extend(tensor_classes)
                detected_confidences.extend(tensor_confs)

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
    harvester_thread = threading.Thread(target=serial_harvester_worker, daemon=True)
    vision_thread = threading.Thread(target=adaptive_vision_streamer, daemon=True)
    
    harvester_thread.start()
    vision_thread.start()
    
    while True:
        time.sleep(1)