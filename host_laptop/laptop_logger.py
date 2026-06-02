import socket
import struct
import csv
from datetime import datetime

NETWORK_PORT = 8080
LOG_FILE_NAME = "pi_optimization_logs.csv"

# Pre-defined mapping dictionary for standard COCO index definitions (expand as needed)
COCO_LABELS = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 16: "dog", 255: "None"}

csv_headers = ["Timestamp", "System Mode", "CPU Usage %", "CPU Temp C", "Sensor Distance cm", "Inference Image Res", "Latency ms", "Objects Tracked"]

with open(LOG_FILE_NAME, mode="w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(csv_headers)

server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server_socket.bind(("0.0.0.0", NETWORK_PORT))

print("="*90)
print(f" LAPTOP EMBEDDED DASHBOARD ONLINE | LISTENING ON BINARY PORT {NETWORK_PORT} ")
print(f" Saving parsed structural telemetries to spreadsheet: {LOG_FILE_NAME} ")
print("="*90)

print(f"{'Time':<12} | {'Pipeline Status':<25} | {'CPU%':<6} | {'Temp°C':<7} | {'Dist(cm)':<8} | {'Res':<5} | {'Latency':<8} | {'Detections'}")
print("-" * 115)

# Target packed structure mask decoder
udp_format = "<B f f f H f B 4B"
packet_size = struct.calcsize(udp_format)

while True:
    try:
        raw_bytes, sender_address = server_socket.recvfrom(1024)
        if len(raw_bytes) != packet_size:
            continue # Drop packet mismatch anomalies
            
        # Extract binary primitives directly from network buffer arrays
        (mode_id, cpu_p, cpu_t, distance, img_res, latency, 
         count, c1, c2, c3, c4) = struct.unpack(udp_format, raw_bytes)
        
        current_time = datetime.now().strftime("%H:%M:%S")
        timestamp_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Translate mode IDs and enum tokens to human-readable text labels
        mode_str = "ACTIVE (Inference)" if mode_id == 1 else "STANDBY (Suppressed)"
        
        raw_classes = [c1, c2, c3, c4][:count]
        detected_objects = [COCO_LABELS.get(cid, f"Class_{cid}") for cid in raw_classes if cid != 255]
        objects_str = ", ".join(detected_objects) if detected_objects else "None"
        
        # 1. Output the live dashboard metrics directly to the terminal interface
        print(f"{current_time:<12} | {mode_str:<25} | {cpu_p:<6.1f} | {cpu_t:<7.1f} | {distance:<8.1f} | {img_res:<5} | {latency:<8.1f} | {objects_str}")
        
        # 2. Append metrics to the CSV file
        with open(LOG_FILE_NAME, mode="a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([timestamp_full, mode_str, round(cpu_p, 1), round(cpu_t, 1), round(distance, 1), img_res, round(latency, 2), objects_str])
            
    except KeyboardInterrupt:
        print("\n[INFO] Dashboard termination complete.")
        break
    except Exception as parse_err:
        print(f"\n[ERROR] Telemetry conversion error: {parse_err}")