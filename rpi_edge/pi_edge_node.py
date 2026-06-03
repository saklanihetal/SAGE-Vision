import glob
import os
import sys
import time
import serial
import socket
import struct
import json
import threading
import cv2
import psutil
from ultralytics import YOLO

# Initialize thread-safe shared state cache dictionary
shared_state = {
    "pir": 0,
    "distance": 300.0,
    "ldr": 500,
    "last_motion_epoch": time.time()
}
state_mutex = threading.Lock()

# Target Threshold Constraints from Specification Profile
PIR_COOLDOWN_WINDOW = 5.0  # Stream active delay buffer (seconds)
LDR_DARK_CUTOFF = 350      # Equalization activation boundary
DISTANCE_FAR_GATE = 250.0  # High-resolution boundary (cm)
DISTANCE_NEAR_GATE = 150.0 # Low-resolution boundary (cm)

# Hardware and Server Node Configurations
SERIAL_INTERFACE = "/dev/ttyUSB0"  
LAPTOP_SERVER_IP = "192.168.1.50"   # ⚠️ UPDATE with your laptop's actual IP address
NETWORK_PORT = 8080

# Fixed Struct Packing Definition for Incoming Sensor Data
SENSOR_PACKET_FMT  = "<B H f"   # little-endian: uint8, uint16, float
SENSOR_PACKET_SIZE = struct.calcsize(SENSOR_PACKET_FMT)   # = 7 bytes

# Initialize local TFLite engine
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

# =======================================================
# THREAD 1: ASYNCHRONOUS USB SERIAL HARVESTER (CORE 1)
# =======================================================
def find_esp32_serial_port():
    """Auto-detect the ESP32 serial port from common Linux device paths."""
    candidates = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
    if candidates:
        port = sorted(candidates)[0]
        print(f"[SYSTEM] Auto-detected ESP32 serial port: {port}", flush=True)
        return port
    # Fallback to the configured default
    return SERIAL_INTERFACE

def serial_harvester_worker():
    os.sched_setaffinity(0, {1})
    print(f"[SYSTEM] Serial Harvester pinned to CPU Core {os.sched_getaffinity(0)}", flush=True)

    try:
        ser = serial.Serial(find_esp32_serial_port(), 115200, timeout=1)
        ser.reset_input_buffer()
    except Exception as hardware_err:
        print(f"[FATAL] USB Serial unreadable at {find_esp32_serial_port()}: {hardware_err}", flush=True)
        sys.exit(1)

    global shared_state
    while True:
        try:
            # Read exactly 7 bytes — the size of one packed SensorPacket
            raw_bytes = ser.read(SENSOR_PACKET_SIZE)
            if len(raw_bytes) != SENSOR_PACKET_SIZE:
                continue  # Incomplete read (e.g. timeout), skip silently

            pir_val, ldr_val, dist_val = struct.unpack(SENSOR_PACKET_FMT, raw_bytes)

            with state_mutex:
                shared_state["pir"] = pir_val
                if dist_val > 0:
                    shared_state["distance"] = dist_val
                shared_state["ldr"] = ldr_val
                if pir_val == 1:
                    shared_state["last_motion_epoch"] = time.time()

        except Exception as loop_fault:
            print(f"[SERIAL ALERT] Intermittent telemetry loss: {loop_fault}", flush=True)
            time.sleep(0.2)

# =======================================================
# THREAD 2: ADAPTIVE INFERENCE ENGINE (CORES 2 & 3)
# =======================================================
def adaptive_vision_streamer():
    os.sched_setaffinity(0, {2, 3})
    print(f"[SYSTEM] Vision processing core engine pinned to CPU Cores {os.sched_getaffinity(0)}", flush=True)
    
    # Establish persistent telemetry socket link to Laptop using UDP
    network_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    camera_feed = cv2.VideoCapture(0)
    camera_feed.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera_feed.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    
    # Unified Packed Binary Output Definition: Exactly 40 Bytes structural space
    udp_format = "<B f f f H f B 4B 4f"
    
    while True:
        with state_mutex:
            pir = shared_state["pir"]
            distance = shared_state["distance"]
            ldr = shared_state["ldr"]
            last_motion = shared_state["last_motion_epoch"]
            
        # A. Evaluate PIR Master Transmission Gate
        if not ((pir == 1) or (time.time() - last_motion < PIR_COOLDOWN_WINDOW)):
            cpu_pct, cpu_c = get_pi_hardware_metrics()
            # Mode 0 = Standby profile configuration
            standby_payload = struct.pack(
                udp_format, 
                0, cpu_pct, cpu_c, distance, 0, 0.0, 
                0, 255, 255, 255, 255, 0.0, 0.0, 0.0, 0.0
            )
            try:
                network_socket.sendto(standby_payload, (LAPTOP_SERVER_IP, NETWORK_PORT))
            except Exception: pass
            time.sleep(1.0) # Deep power conservation sleep
            continue
            
        success, raw_frame = camera_feed.read()
        if not success or raw_frame is None:
            continue
            
        # B. Evaluate LDR Preprocessing Gate
        if ldr < LDR_DARK_CUTOFF:
            yuv_buffer = cv2.cvtColor(raw_frame, cv2.COLOR_BGR2YUV)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
            yuv_buffer[:, :, 0] = clahe.apply(yuv_buffer[:, :, 0])
            processed_frame = cv2.cvtColor(yuv_buffer, cv2.COLOR_YUV2BGR)
        else:
            processed_frame = raw_frame
            
        # C. Evaluate Ultrasonic Proximity Resolution Gate
        img_inference_size = 320 if distance < DISTANCE_NEAR_GATE else 640
            
        # D. Run Local TFLite Inference & Measure Performance Time
        start_inference = time.time()
        inference_results = yolo_model(processed_frame, imgsz=img_inference_size, verbose=False)
        inference_duration_ms = (time.time() - start_inference) * 1000.0
        
        # E. Extract and Clean Detected Object Labels
        detected_classes = []
        detected_confidences = []
        
        for result in inference_results:
            if result.boxes is not None and len(result.boxes) > 0:
                # Fixed Tensor Space Bug: safely using .cpu().int() and .float()
                tensor_classes = result.boxes.cls.cpu().int().tolist()
                tensor_confs = result.boxes.conf.cpu().float().tolist()
                
                detected_classes.extend(tensor_classes)
                detected_confidences.extend(tensor_confs)
                    
        # F. Pull Hardware Diagnostics and Transmit Log Telemetry
        detection_count = min(len(detected_classes), 4)
        
        padded_classes = [255, 255, 255, 255]
        padded_confidences = [0.0, 0.0, 0.0, 0.0]
        
        for i in range(detection_count):
            padded_classes[i] = detected_classes[i]
            padded_confidences[i] = detected_confidences[i]
            
        cpu_pct, cpu_c = get_pi_hardware_metrics() # get cpu_usage and cpu_temp for telemetry
        
        # Fixed Struct Packing Bug: safely unpacking inline arrays using * operator lists
        active_payload = struct.pack(
            udp_format,
            1, cpu_pct, cpu_c, distance, img_inference_size, inference_duration_ms,
            detection_count, *padded_classes, *padded_confidences
        )
        
        try:
            network_socket.sendto(active_payload, (LAPTOP_SERVER_IP, NETWORK_PORT))
        except Exception as tx_err:
            print(f"[NET ERROR] Log frame dropped: {tx_err}", flush=True)
            
        # Dynamic Loop Pacing based on target proximity
        if distance > DISTANCE_FAR_GATE:
            time.sleep(0.4)   # Target far away: Run slow pacing to save energy
        elif distance < DISTANCE_NEAR_GATE:
            time.sleep(0.01)  # Target very close: Run responsive full pacing
        else:
            time.sleep(0.1)   # Intermediate adaptive spacing

if __name__ == "__main__":
    print("[INIT] Launching Adaptive Intelligent Video Node Architecture...", flush=True)
    harvester_thread = threading.Thread(target=serial_harvester_worker, daemon=True)
    vision_thread = threading.Thread(target=adaptive_vision_streamer, daemon=True)
    
    harvester_thread.start()
    vision_thread.start()
    
    while True:
        time.sleep(1)