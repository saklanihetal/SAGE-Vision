import socket
import struct
from datetime import datetime

NETWORK_PORT = 8080

COCO_LABELS = {
    0: "Student/Person", 1: "Bicycle", 2: "Car", 3: "Motorcycle", 4: "Airplane",
    5: "Bus", 6: "Train", 7: "Truck", 9: "Traffic Light", 11: "Fire Hydrant",
    13: "Stop Sign", 14: "Parking Meter", 15: "Bench", 16: "Bird", 17: "Cat",
    18: "Dog", 19: "Horse", 20: "Sheep", 21: "Cow", 22: "Elephant", 23: "Bear",
    24: "Backpack", 25: "Umbrella", 26: "Handbag", 27: "Tie", 28: "Suitcase",
    32: "Sports Ball", 39: "Water Bottle", 41: "Coffee Cup/Mug", 42: "Fork",
    43: "Knife", 44: "Spoon", 45: "Bowl", 46: "Banana", 47: "Apple", 48: "Sandwich",
    56: "Chair", 57: "Couch/Sofa", 58: "Potted Plant", 59: "Bed", 60: "Dining Table",
    62: "Lab Monitor/TV", 63: "Laptop", 64: "Computer Mouse", 65: "Remote Control",
    66: "Keyboard", 67: "Cell Phone", 68: "Microwave", 69: "Oven", 71: "Sink",
    72: "Refrigerator", 73: "Book/Notebook", 74: "Clock", 75: "Vase", 76: "Scissors",
    77: "Teddy Bear", 78: "Hair Drier", 79: "Toothbrush", 255: "None"
}

MODE_MAPPING = {
    0: "SLEEP (Idle)",
    1: "STANDBY (Warmup)",
    2: "ACTIVE-LO (320x320)",
    3: "ACTIVE-HI (640x640)",
    4: "WATCHDOG (Failsafe)"
}

server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server_socket.bind(("0.0.0.0", NETWORK_PORT))

print("="*110)
print(f" LAPTOP EMBEDDED DASHBOARD ONLINE | LISTENING ON BINARY PORT {NETWORK_PORT} ")
print("="*110)
print(f"{'Time':<12} | {'Pipeline Status':<22} | {'CPU%':<6} | {'Temp°C':<7} | {'Dist(cm)':<8} | {'Res':<5} | {'Latency':<8} | {'Detections (4-Tuple Array)'}")
print("-" * 140)

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
        
        mode_str = MODE_MAPPING.get(mode_id, f"UNKNOWN ({mode_id})")
        current_time = datetime.now().strftime("%H:%M:%S")
        
        # Build the exact 4-tuple array structure from the packet stream
        tuples_list = []
        for i in range(4):
            cid = unpacked_data[7 + i]
            conf = unpacked_data[11 + i]
            if i < count and cid != 255:
                label = COCO_LABELS.get(cid, f"Class_{cid}")
                tuples_list.append((label, round(conf * 100, 1)))
            else:
                tuples_list.append(("None", 0.0))
                
        print(f"{current_time:<12} | {mode_str:<22} | {cpu_p:<6.1f} | {cpu_t:<7.1f} | {distance:<8.1f} | {img_res:<5} | {latency:<8.1f} | {tuples_list}")
            
    except KeyboardInterrupt:
        print("\n[INFO] Laptop terminal dashboard manual shutdown complete.")
        break
    except Exception as parse_err:
        print(f"\n[ERROR] Telemetry extraction fault: {parse_err}")