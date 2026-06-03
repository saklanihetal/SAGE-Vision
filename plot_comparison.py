import os
import re
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# =========================================================================
# OPERATIONAL WARNING (CONCERN 4: THERMAL TRAILING)
# =========================================================================
# IMPORTANT: To prevent historical thermal bleeding from ruining your data, 
# shut down all scripts and allow the physical Raspberry Pi to cool down 
# to its idle room temperature for at least 5 minutes between your test runs!
# =========================================================================

# Explicitly define the local ambient room temperature baseline threshold
AMBIENT_ROOM_TEMP = 25.0  # Measured in degrees Celsius (*C)

# File system path strings mapped relative to the repository root directory
ADAPTIVE_CSV = "host_laptop/pi_optimization_logs.csv"
BASELINE_CSV = "test/baseline_optimization_logs.csv"


def parse_confidence_values(row):
    """
    CONCERN 3 FIX: Scene-Wise Accuracy Quantification Parser.
    
    Safely extracts numeric confidence scores from either a dedicated column 
    or regex-parses embedded parenthetical tokens like 'Student/Person (94%)'.
    Returns a clean list of floats scaled out of 100.0.
    """
    # If the system was in standby mode, no inference occurred; return empty defaults
    if "STANDBY" in str(row["System Mode"]):
        return []
        
    # Trackers for our clean extracted floating-point values
    extracted_scores = []
    
    # Strategy A: Check if the logger wrote confidence metrics into a dedicated column
    if "Confidence Scores" in row and pd.notna(row["Confidence Scores"]):
        raw_confs = str(row["Confidence Scores"]).strip()
        if raw_confs not in ["None", "0", ""]:
            try:
                # Split comma-separated numbers and cast them directly to floats
                return [float(x.strip()) for x in raw_confs.split(",") if x.strip()]
            except ValueError:
                pass # Fallback to Strategy B if string conversion errors occur
                
    # Strategy B: Fallback Regex engine to parse inline patterns like '(94%)' from text
    raw_objects_text = str(row["Objects Tracked"])
    if raw_objects_text and raw_objects_text != "None":
        # Regular expression isolates digits resting inside parentheses right before a % symbol
        regex_matches = re.findall(r'\((\d+(?:\.\d+)?)\s*%\)', raw_objects_text)
        if regex_matches:
            return [float(match) for match in regex_matches]
            
    return extracted_scores


def extract_table_display_string(row):
    """Parses row data to compile readable object lists for the summary table."""
    if "STANDBY" in str(row["System Mode"]):
        return "None (Standby Mode - Suppressed)"
        
    # Pull object names out of the comma-separated string array
    raw_objs = str(row["Objects Tracked"])
    objs_list = [x.strip() for x in raw_objs.split(",") if x.strip() and "None" not in x]
    
    # If regex parameters are found embedded, clean the text labels for clear presentation tables
    cleaned_objs = [re.sub(r'\s*\(\d+(?:\.\d+)?\s*%\)', '', name) for name in objs_list]
    
    # Gather matching clean confidence scores using our robust parser
    scores_list = parse_confidence_values(row)
    
    if not cleaned_objs or (len(scores_list) == 0 and "0" in str(row["Confidence Scores"])):
        return "None (No Active Detections)"
        
    # Zip names and confidence float metrics back together into a standardized layout
    formatted_pairs = []
    for idx, obj in enumerate(cleaned_objs):
        if idx < len(scores_list):
            formatted_pairs.append(f"{obj} ({scores_list[idx]:.1f}%)")
        else:
            formatted_pairs.append(f"{obj}")
            
    return ", ".join(formatted_pairs)


def generate_evaluation_dashboard():
    # Verify both source metrics logging databases exist before processing data frames
    if not os.path.exists(ADAPTIVE_CSV) or not os.path.exists(BASELINE_CSV):
        print(f"[ERROR] Telemetry log files missing. Run your data collection windows first.")
        return

    # Ingest historical datasets into memory using Pandas DataFrames
    df_adapt = pd.read_csv(ADAPTIVE_CSV)
    df_base = pd.read_csv(BASELINE_CSV)

    # CONCERN 2 FIX: Convert string timestamps into true cross-platform Python datetime objects
    df_adapt["DateTime"] = pd.to_datetime(df_adapt["Timestamp"], format="%Y-%m-%d %H:%M:%S")
    df_base["DateTime"] = pd.to_datetime(df_base["Timestamp"], format="%Y-%m-%d %H:%M:%S")

    # Compute high-precision relative elapsed timelines in seconds from the exact run start
    df_adapt["Elapsed_Sec"] = (df_adapt["DateTime"] - df_adapt["DateTime"].iloc[0]).dt.total_seconds()
    df_base["Elapsed_Sec"] = (df_base["DateTime"] - df_base["DateTime"].iloc[0]).dt.total_seconds()

    # =========================================================================
    # TASK 1: DYNAMIC SCENE EXTRAPOLATION VIA ELAPSED TIME CHECKPOINTS
    # =========================================================================
    # Query explicit time checkpoints to align the asynchronous logs accurately
    time_checkpoints = [10.0, 25.0, 45.0, 60.0, 75.0, 90.0]
    table_rows_data = []

    for t_mark in time_checkpoints:
        # Match the closest timestamp log entry index inside the baseline matrix
        base_idx = (df_base["Elapsed_Sec"] - t_mark).abs().idxmin()
        base_row = df_base.loc[base_idx]
        
        # Match the closest timestamp log entry index inside the adaptive matrix
        adapt_idx = (df_adapt["Elapsed_Sec"] - t_mark).abs().idxmin()
        adapt_row = df_adapt.loc[adapt_idx]
        
        # Compile standardized text layouts using our robust parsing engines
        baseline_text = extract_table_display_string(base_row)
        adaptive_text = extract_table_display_string(adapt_row)
        
        # Append data row matrix back into our presentation table register
        table_rows_data.append([f"{int(t_mark)}s Elapsed", baseline_text, adaptive_text])

    # =========================================================================
    # TASK 2: GRAPHICS CANVAS & COLOR PROFILE INITIALIZATION
    # =========================================================================
    # Instantiate wide 4-panel vertical stacked metrics presentation canvas layout
    fig, axes = plt.subplots(4, 1, figsize=(14, 24))
    fig.suptitle("Edge System Evaluation Dashboard:\nAdaptive Sensor-Guided vs. Unoptimized Baseline Performance", fontsize=16, fontweight='bold', y=0.98)

    # Define high-contrast line visualization color variables
    COLOR_WITHOUT_LINE = '#D32F2F'  # Crimson Red 
    COLOR_WITH_SYSTEM = '#1f77b4'   # Vivid Cobalt Blue

    # =========================================================================
    # PANEL 1: ALIGNED MILESTONE TIMELINE EVALUATION DATA TABLE
    # =========================================================================
    # Remove grid ticks and outside structural boundary borders for clear table layout space
    axes[0].axis('off')

    col_labels = [
        "Experimental Timeline Milestone", 
        "Baseline Detections (Without System - Constant 640x640 Resolution)", 
        "Adaptive Detections (With System - Sensor-Gated Dynamic Windows)"
    ]

    # Render embedded vector table inside the first grid axis segment
    metrics_table = axes[0].table(cellText=table_rows_data, colLabels=col_labels, loc='center', cellLoc='left')
    metrics_table.auto_set_font_size(False)
    metrics_table.set_fontsize(10)
    metrics_table.scale(1, 2.8)  # Expand vertical block height padding for readability

    # Format table header cells manually
    for col_idx in range(len(col_labels)):
        cell = metrics_table[0, col_idx]
        cell.set_facecolor('#f2f2f2')
        cell.get_text().set_weight('bold')
        cell.get_text().set_horizontalalignment('center')

    axes[0].set_title("Table 1: Aligned Milestone Timeline Object Detection Integrity Comparison", fontsize=12, fontweight='bold', pad=20)

    # =========================================================================
    # PANEL 2: LINE CHART - COMPUTATIONAL INFERENCE LATENCY COMPARISON
    # =========================================================================
    # CONCERN 1 FIX: Filter out standby latency noise (System Mode == 0 or Latency == 0)
    # This prevents skipped frames from artificially dropping the adaptive line to 0.0ms
    df_base_active = df_base[df_base["Latency ms"] > 0.0]
    df_adapt_active = df_adapt[df_adapt["System Mode"] == "ACTIVE (Inference)"]
    
    # Fallback filtering to ensure absolute safety if string tokens vary slightly
    if df_adapt_active.empty:
        df_adapt_active = df_adapt[df_adapt["Latency ms"] > 0.0]

    # Plot cleaned latency profiles against relative time measurements on the X-axis
    axes[1].plot(df_base_active["Elapsed_Sec"], df_base_active["Latency ms"], label="Without System (Fixed 640x640 Resolution)", color=COLOR_WITHOUT_LINE, alpha=0.6, linewidth=1.5)
    axes[1].plot(df_adapt_active["Elapsed_Sec"], df_adapt_active["Latency ms"], label="With System (Dynamic 320x320 / 640x640 Resolution)", color=COLOR_WITH_SYSTEM, linewidth=2)

    axes[1].set_title("Graph 1: Cleaned Computational Latency - Inference Loop Duration (Standby Noise Omitted)", fontsize=12, fontweight='bold')
    axes[1].set_ylabel("Inference Latency (ms)", fontsize=10, fontweight='bold')
    axes[1].set_xlabel("Time Elapsed Since Run Start (Seconds)", fontsize=10, fontweight='bold')
    axes[1].spines['top'].set_visible(False)
    axes[1].spines['right'].set_visible(False)
    axes[1].grid(True, linestyle="--", alpha=0.5)
    axes[1].legend(loc="upper right")

    # =========================================================================
    # PANEL 3: LINE CHART - THERMAL SENSOR GRADIENT ANALYSIS
    # =========================================================================
    # Plot core temperature vectors directly against matched timeline paths
    axes[2].plot(df_base["Elapsed_Sec"], df_base["CPU Temp C"], label="Without System Core Temp", color=COLOR_WITHOUT_LINE, alpha=0.6, linestyle="--", linewidth=1.5)
    axes[2].plot(df_adapt["Elapsed_Sec"], df_adapt["CPU Temp C"], label="With System Core Temp", color=COLOR_WITH_SYSTEM, linewidth=2)
    
    # CONCERN 4 FIX: Render an explicit horizontal marker displaying Ambient Room Temperature
    axes[2].axhline(AMBIENT_ROOM_TEMP, color="#7f8c8d", linestyle="-.", linewidth=1.5, label=f"Measured Lab Ambient Baseline ({AMBIENT_ROOM_TEMP}°C)")
    
    # Mark the official Broadcom SoC hardware throttling boundary ceiling
    axes[2].axhline(80.0, color="#FF9800", linestyle=":", linewidth=2, label="Broadcom SoC Thermal Throttling Boundary (80°C)")

    axes[2].set_title("Graph 2: Thermal Dynamics - CPU Core Temperature Flattening Curves", fontsize=12, fontweight='bold')
    axes[2].set_ylabel("Silicon Core Temperature (°C)", fontsize=10, fontweight='bold')
    axes[2].set_xlabel("Time Elapsed Since Run Start (Seconds)", fontsize=10, fontweight='bold')
    axes[2].spines['top'].set_visible(False)
    axes[2].spines['right'].set_visible(False)
    axes[2].grid(True, linestyle="--", alpha=0.5)
    axes[2].legend(loc="lower right")

    # =========================================================================
    # PANEL 4: LINE CHART - CPU UTILIZATION PROFILE COMPARISON
    # =========================================================================
    # Trace continuous scaling core utilization values across the runtime session
    axes[3].plot(df_base["Elapsed_Sec"], df_base["CPU Usage %"], label="Without System CPU Usage", color=COLOR_WITHOUT_LINE, alpha=0.6, linewidth=1.5)
    axes[3].plot(df_adapt["Elapsed_Sec"], df_adapt["CPU Usage %"], label="With System CPU Usage", color=COLOR_WITH_SYSTEM, linewidth=2)

    axes[3].set_title("Graph 3: System Resource Footprint - CPU Utilization Efficiency Curves", fontsize=12, fontweight='bold')
    axes[3].set_ylabel("Processor Utilization (%)", fontsize=10, fontweight='bold')
    axes[3].set_xlabel("Time Elapsed Since Run Start (Seconds)", fontsize=10, fontweight='bold')
    axes[3].set_ylim(-5, 105) # Secure scale alignment boundaries
    axes[3].spines['top'].set_visible(False)
    axes[3].spines['right'].set_visible(False)
    axes[3].grid(True, linestyle="--", alpha=0.5)
    axes[3].legend(loc="upper right")

    # =========================================================================
    # SECTION 4: FILE COMPILATION & EXPORT
    # =========================================================================
    plt.tight_layout()
    output_plot_path = "system_performance_comparison.png"
    plt.savefig(output_plot_path, dpi=300, bbox_inches='tight')
    print(f"[SUCCESS] Temporally aligned evaluation dashboard generated cleanly as: '{output_plot_path}'")
    plt.show()


if __name__ == "__main__":
    generate_evaluation_dashboard()