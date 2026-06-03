import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# =========================================================================
# SECTION 1: DATA INGESTION & RELATIVE TIMELINE PARSING
# =========================================================================

ADAPTIVE_CSV = "host_laptop/pi_optimization_logs.csv"
BASELINE_CSV = "test/baseline_optimization_logs.csv"

# Verify metrics databanks exist before initiating math operations
if not os.path.exists(ADAPTIVE_CSV) or not os.path.exists(BASELINE_CSV):
    raise FileNotFoundError(
        f"[ERROR] Telemetry source files missing. Ensure logs exist at:\n"
        f"  1. {ADAPTIVE_CSV}\n  2. {BASELINE_CSV}"
    )

# Read continuous logging datasets into Pandas DataFrames
df_adapt = pd.read_csv(ADAPTIVE_CSV)
df_base = pd.read_csv(BASELINE_CSV)

# Parse absolute string timestamps into datetime objects with sub-second precision
df_adapt["DateTime"] = pd.to_datetime(df_adapt["Timestamp"], format="%Y-%m-%d %H:%M:%S")
df_base["DateTime"] = pd.to_datetime(df_base["Timestamp"], format="%Y-%m-%d %H:%M:%S")

# Compute elapsed runtime profiles in seconds to normalize the X-axis for line graphs
df_adapt["Elapsed_Sec"] = (df_adapt["DateTime"] - df_adapt["DateTime"].iloc[0]).dt.total_seconds()
df_base["Elapsed_Sec"] = (df_base["DateTime"] - df_base["DateTime"].iloc[0]).dt.total_seconds()


# =========================================================================
# SECTION 2: GRAPH 1 REPLACEMENT - DYNAMIC SCENE TABLE PARSING (FIRST 6 ROWS)
# =========================================================================

# Quantize exactly the first 6 logging frames to map out Scenes 1 through 6
num_scenes = min(6, len(df_adapt), len(df_base))
table_rows_data = []

for i in range(num_scenes):
    base_row = df_base.iloc[i]
    adapt_row = df_adapt.iloc[i]
    
    def extract_scene_string(row):
        """Helper to safely extract labels and append percentage symbols back to strings."""
        raw_objs = str(row["Objects Tracked"])
        raw_confs = str(row["Confidence Scores"])
        
        # Split tokens, strip spacing, and clean null placeholders
        objs = [x.strip() for x in raw_objs.split(",") if x.strip() and x.strip() != "None"]
        confs = [x.strip() for x in raw_confs.split(",") if x.strip() and x.strip() != "0"]
        
        if not objs:
            return "None (Dropped Frame)"
            
        paired_strings = []
        for o, c in zip(objs, confs):
            paired_strings.append(f"{o} ({c}%)")
        return ", ".join(paired_strings)

    scene_baseline_text = extract_scene_string(base_row)
    scene_adaptive_text = extract_scene_string(adapt_row)
    
    # Store clean row matrix string assets
    table_rows_data.append([f"Scene {i+1}", scene_baseline_text, scene_adaptive_text])


# =========================================================================
# SECTION 3: CANVAS DESIGN & COLOR PROFILE INITIALIZATION
# =========================================================================

# Build wide, presentation-ready 4-panel vertical stack layout canvas
fig, axes = plt.subplots(4, 1, figsize=(14, 24))
fig.suptitle("Edge System Evaluation Dashboard:\nAdaptive Sensor-Guided vs. Unoptimized Baseline Performance", fontsize=16, fontweight='bold', y=0.98)

# Unified line graph color configuration profiles
COLOR_WITHOUT_LINE = '#D32F2F'  # Crimson Red 
COLOR_WITH_SYSTEM = '#1f77b4'   # Vibrant Blue


# =========================================================================
# PANEL 1: MATPLOTLIB HARDWARE TELEMETRY COMPARISON DATA TABLE
# =========================================================================

# Clear axis borders and line ticks to dedicate Panel 1 entirely to table graphics
axes[0].axis('off')

# Explicitly define column descriptors
col_labels = [
    "Experimental Milestone Scene", 
    "Baseline Detections (Without System - Constant 640x640 Resolution)", 
    "Adaptive Detections (With System - Sensor-Gated Dynamic Resolution)"
]

# Generate embedded structural vector graphics table component
metrics_table = axes[0].table(
    cellText=table_rows_data, 
    colLabels=col_labels, 
    loc='center', 
    cellLoc='left'
)

# Polishing table styles for crisp corporate presentation slides
metrics_table.auto_set_font_size(False)
metrics_table.set_fontsize(10)
metrics_table.scale(1, 2.8)  # Expand vertical block thickness to isolate lines comfortably

# Highlight table cell header colors manually
for col_idx in range(len(col_labels)):
    cell = metrics_table[0, col_idx]
    cell.set_facecolor('#f2f2f2')
    cell.get_text().set_weight('bold')
    cell.get_text().set_horizontalalignment('center')

axes[0].set_title("Table 1: Scene-by-Scene Object Detection & Accuracy Target Integrity Comparison", fontsize=12, fontweight='bold', pad=20)


# =========================================================================
# PANEL 2: LINE CHART - COMPUTATIONAL INFERENCE LATENCY COMPARISON
# =========================================================================

axes[1].plot(df_base["Elapsed_Sec"], df_base["Latency ms"], label="Without System (Fixed 640x640 Resolution)", color=COLOR_WITHOUT_LINE, alpha=0.6, linewidth=1.5)
axes[1].plot(df_adapt["Elapsed_Sec"], df_adapt["Latency ms"], label="With System (Dynamic 320x320 / 640x640 Resolution)", color=COLOR_WITH_SYSTEM, linewidth=2)

axes[1].set_title("Graph 1: Computational Latency - Inference Math Loop Duration", fontsize=12, fontweight='bold')
axes[1].set_ylabel("Inference Latency (ms)", fontsize=10, fontweight='bold')
axes[1].set_xlabel("Time Elapsed Since Run Start (Seconds)", fontsize=10, fontweight='bold')
axes[1].spines['top'].set_visible(False)
axes[1].spines['right'].set_visible(False)
axes[1].grid(True, linestyle="--", alpha=0.5)
axes[1].legend(loc="upper right")


# =========================================================================
# PANEL 3: LINE CHART - THERMAL CORE SENSOR TRACKING
# =========================================================================

axes[2].plot(df_base["Elapsed_Sec"], df_base["CPU Temp C"], label="Without System Core Temp", color=COLOR_WITHOUT_LINE, alpha=0.6, linestyle="--", linewidth=1.5)
axes[2].plot(df_adapt["Elapsed_Sec"], df_adapt["CPU Temp C"], label="With System Core Temp", color=COLOR_WITH_SYSTEM, linewidth=2)
axes[2].axhline(80.0, color="#FF9800", linestyle=":", linewidth=2, label="Broadcom SoC Thermal Throttling Boundary (80°C)")

axes[2].set_title("Graph 2: Thermal Dynamics - CPU Core Temperature Comparison", fontsize=12, fontweight='bold')
axes[2].set_ylabel("Silicon Core Temperature (°C)", fontsize=10, fontweight='bold')
axes[2].set_xlabel("Time Elapsed Since Run Start (Seconds)", fontsize=10, fontweight='bold')
axes[2].spines['top'].set_visible(False)
axes[2].spines['right'].set_visible(False)
axes[2].grid(True, linestyle="--", alpha=0.5)
axes[2].legend(loc="lower right")


# =========================================================================
# PANEL 4: LINE CHART - CPU UTILIZATION COMPARISON
# =========================================================================

axes[3].plot(df_base["Elapsed_Sec"], df_base["CPU Usage %"], label="Without System CPU Usage", color=COLOR_WITHOUT_LINE, alpha=0.6, linewidth=1.5)
axes[3].plot(df_adapt["Elapsed_Sec"], df_adapt["CPU Usage %"], label="With System CPU Usage", color=COLOR_WITH_SYSTEM, linewidth=2)

axes[3].set_title("Graph 3: System Resource Footprint - CPU Utilization Over Time", fontsize=12, fontweight='bold')
axes[3].set_ylabel("Processor Utilization (%)", fontsize=10, fontweight='bold')
axes[3].set_xlabel("Time Elapsed Since Run Start (Seconds)", fontsize=10, fontweight='bold')
axes[3].set_ylim(-5, 105)
axes[3].spines['top'].set_visible(False)
axes[3].spines['right'].set_visible(False)
axes[3].grid(True, linestyle="--", alpha=0.5)
axes[3].legend(loc="upper right")


# =========================================================================
# SECTION 4: FILE COMPILATION & VECTOR GRAPHICS EXPORT
# =========================================================================

plt.tight_layout()
output_plot_path = "system_performance_comparison.png"
plt.savefig(output_plot_path, dpi=300, bbox_inches='tight')
print(f"[SUCCESS] 4-panel evaluation visual containing embedded table data saved as: '{output_plot_path}'")
plt.show()