import socket
import struct
import csv
from datetime import datetime

NETWORK_PORT = 8080
LOG_FILE_NAME = "pi_optimization_logs.csv"

# Comprehensive COCO dataset index mapping optimized for CS Lab environments
COCO_LABELS = {
    0: "Student/Person",
    1: "Bicycle",
    2: "Car",
    3: "Motorcycle",
    4: "Airplane",
    5: "Bus",
    6: "Train",
    7: "Truck",
    9: "Traffic Light",
    11: "Fire Hydrant",
    13: "Stop Sign",
    14: "Parking Meter",
    15: "Bench",
    16: "Bird",
    17: "Cat",
    18: "Dog",
    19: "Horse",
    20: "Sheep",
    21: "Cow",
    22: "Elephant",
    23: "Bear",
    24: "Backpack",
    25: "Umbrella",
    26: "Handbag",
    27: "Tie",
    28: "Suitcase",
    32: "Sports Ball",
    39: "Water Bottle",
    41: "Coffee Cup/Mug",
    42: "Fork",
    43: "Knife",
    44: "Spoon",
    45: "Bowl",
    46: "Banana",
    47: "Apple",
    48: "Sandwich",
    56: "Chair",
    57: "Couch/Sofa",
    58: "Potted Plant",
    59: "Bed",
    60: "Dining Table",
    62: "Lab Monitor/TV",
    63: "Laptop",
    64: "Computer Mouse",
    65: "Remote Control",
    66: "Keyboard",
    67: "Cell Phone",
    68: "Microwave",
    69: "Oven",
    71: "Sink",
    72: "Refrigerator",
    73: "Book/Notebook",
    74: "Clock",
    75: "Vase",
    76: "Scissors",
    77: "Teddy Bear",
    78: "Hair Drier",
    79: "Toothbrush",
    255: "None"
}

csv_headers = [
    "Timestamp", 
    "System Mode", 
    "CPU Usage %", 
    "CPU Temp C", 
    "Sensor Distance cm", 
    "Inference Image Res", 
    "Latency ms", 
    "Objects Tracked", 
    "Confidence Scores"
]

# Initialize data logging schema headers
with open(LOG_FILE_NAME, mode="w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(csv_headers)

server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server_socket.bind(("0.0.0.0", NETWORK_PORT))

print("="*110)
print(f" LAPTOP EMBEDDED DASHBOARD ONLINE | LISTENING ON BINARY PORT {NETWORK_PORT} ")
print(f" Saving parsed structural telemetries to spreadsheet: {LOG_FILE_NAME} ")
print("="*110)

print(f"{'Time':<12} | {'Pipeline Status':<22} | {'CPU%':<6} | {'Temp°C':<7} | {'Dist(cm)':<8} | {'Res':<5} | {'Latency':<8} | {'Detections (Confidence)'}")
print("-" * 130)

# 40-byte structural protocol mask mapping layout
udp_format = "<B f f f H f B 4B 4f"
packet_size = struct.calcsize(udp_format)

while True:
    try:
        raw_bytes, sender_address = server_socket.recvfrom(1024)
        if len(raw_bytes) != packet_size:
            continue 
            
        unpacked_data = struct.unpack(udp_format, raw_bytes)
        
        mode_id = unpacked_data[0]
        cpu_p = unpacked_data[1]
        cpu_t = unpacked_data[2]
        distance = unpacked_data[3]
        img_res = unpacked_data[4]
        latency = unpacked_data[5]
        count = unpacked_data[6]
        
        # Pull raw Class IDs and Float Confidence scores up to active count boundary
        raw_classes = unpacked_data[7:11][:count]
        raw_confs = unpacked_data[11:15][:count]
        
        current_time = datetime.now().strftime("%H:%M:%S")
        timestamp_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        mode_str = "ACTIVE (Inference)" if mode_id == 1 else "STANDBY (Suppressed)"
        
        detected_objects = []
        detected_confs = []
        terminal_display_list = []
        
        for cid, conf in zip(raw_classes, raw_confs):
            if cid != 255:
                label = COCO_LABELS.get(cid, f"Class_{cid}")
                detected_objects.append(label)
                
                # REFACTORED: Appending clean rounded floats as plain string numbers for seamless graph plotting
                detected_confs.append(f"{round(conf * 100, 1)}")
                
                # Maintain the percentage symbol on the live terminal console output for look and readability
                terminal_display_list.append(f"{label} ({conf*100:.1f}%)")
                
        # Format strings for CSV cell storage
        objects_str = ", ".join(detected_objects) if detected_objects else "None"
        
        # REFACTORED: Explicitly defaults to a numeric "0" if no objects are found in the frame
        confs_str = ", ".join(detected_confs) if detected_confs else "0"
        terminal_str = ", ".join(terminal_display_list) if terminal_display_list else "None"
        
        # 1. Output live unified telemetry metrics dashboard straight to standard terminal stream
        print(f"{current_time:<12} | {mode_str:<22} | {cpu_p:<6.1f} | {cpu_t:<7.1f} | {distance:<8.1f} | {img_res:<5} | {latency:<8.1f} | {terminal_str}")
        
        # 2. Appending object labels and confidence values into completely separate CSV columns
        with open(LOG_FILE_NAME, mode="a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                timestamp_full, 
                mode_str, 
                round(cpu_p, 1), 
                round(cpu_t, 1), 
                round(distance, 1), 
                img_res, 
                round(latency, 2), 
                objects_str, 
                confs_str
            ])
            
    except KeyboardInterrupt:
        print("\n[INFO] Laptop terminal logging manual shutdown complete.")
        break
    except Exception as parse_err:
        print(f"\n[ERROR] Telemetry extraction fault: {parse_err}")