"""
StreamNet Visualization and Publication-Grade Comparative Plotting.

Generates comparative analysis charts from REAL executed benchmark CSV/JSON datasets.
"""

from pathlib import Path
from typing import List, Optional
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for headless figure generation
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

RESULTS_DIR = Path(__file__).resolve().parent / "results"
PLOTS_DIR = RESULTS_DIR / "plots"
PLOTS_DIR.mkdir(parents=True, exist_ok=True)

# Set clean aesthetic styling
plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")
plt.rcParams.update({
    "font.size": 11,
    "axes.labelsize": 12,
    "axes.titlesize": 13,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "legend.fontsize": 11,
    "figure.titlesize": 14,
    "lines.linewidth": 2.2,
    "lines.markersize": 7,
})

TCP_COLOR = "#2563EB"   # Royal Blue
UDP_COLOR = "#DC2626"   # Crimson Red


def load_master_dataframe(csv_path: Optional[Path] = None) -> pd.DataFrame:
    """Load and preprocess the master results CSV."""
    path = csv_path or (RESULTS_DIR / "master_experiment_results.csv")
    if not path.exists():
        raise FileNotFoundError(f"Experiment results file not found at: {path}")
    df = pd.read_csv(path)
    return df


def plot_throughput_vs_bandwidth(df: pd.DataFrame, output_dir: Path = PLOTS_DIR) -> Path:
    """Plot Throughput & Goodput vs Bandwidth Limit."""
    bw_df = df[df["category"] == "bandwidth_sweep"].copy()
    if bw_df.empty:
        return output_dir

    bw_df = bw_df.sort_values(by=["bandwidth_mbps"])

    fig, ax = plt.subplots(figsize=(8, 5), dpi=300)
    for proto, color in [("TCP", TCP_COLOR), ("UDP", UDP_COLOR)]:
        proto_data = bw_df[bw_df["protocol"] == proto]
        ax.plot(
            proto_data["bandwidth_mbps"],
            proto_data["throughput_mbps"],
            marker="o",
            label=f"{proto} Raw Throughput",
            color=color,
        )
        ax.plot(
            proto_data["bandwidth_mbps"],
            proto_data["goodput_mbps"],
            marker="s",
            linestyle="--",
            label=f"{proto} Goodput (Video Payload)",
            color=color,
            alpha=0.75,
        )

    ax.set_xlabel("Configured Bandwidth Constraint (Mbps)")
    ax.set_ylabel("Measured Throughput / Goodput (Mbps)")
    ax.set_title("TCP vs. UDP: Throughput & Goodput vs. Bandwidth Throttling")
    ax.legend(frameon=True, loc="upper left")
    ax.grid(True, linestyle=":", alpha=0.6)

    out_file = output_dir / "fig1_throughput_vs_bandwidth.png"
    plt.tight_layout()
    plt.savefig(out_file)
    plt.close()
    return out_file


def plot_latency_vs_delay(df: pd.DataFrame, output_dir: Path = PLOTS_DIR) -> Path:
    """Plot Measured End-to-End Latency vs Injected Channel Delay."""
    lat_df = df[df["category"] == "latency_sweep"].copy()
    if lat_df.empty:
        return output_dir

    lat_df = lat_df.sort_values(by=["latency_ms"])

    fig, ax = plt.subplots(figsize=(8, 5), dpi=300)
    for proto, color in [("TCP", TCP_COLOR), ("UDP", UDP_COLOR)]:
        proto_data = lat_df[lat_df["protocol"] == proto]
        ax.plot(
            proto_data["latency_ms"],
            proto_data["latency_mean_ms"],
            marker="o",
            label=f"{proto} Mean Latency",
            color=color,
        )
        ax.fill_between(
            proto_data["latency_ms"],
            proto_data["latency_mean_ms"],
            proto_data["latency_p95_ms"],
            color=color,
            alpha=0.15,
            label=f"{proto} 95th Percentile Tail",
        )

    # Reference ideal line
    max_lat = lat_df["latency_ms"].max()
    ax.plot([0, max_lat], [0, max_lat], "k:", alpha=0.5, label="Ideal Channel Latency (1:1)")

    ax.set_xlabel("Injected Network Delay (ms)")
    ax.set_ylabel("Measured End-to-End Transit Latency (ms)")
    ax.set_title("TCP vs. UDP: Transit Latency vs. Injected Network Delay")
    ax.legend(frameon=True, loc="upper left")
    ax.grid(True, linestyle=":", alpha=0.6)

    out_file = output_dir / "fig2_latency_vs_injected_delay.png"
    plt.tight_layout()
    plt.savefig(out_file)
    plt.close()
    return out_file


def plot_loss_resilience(df: pd.DataFrame, output_dir: Path = PLOTS_DIR) -> Path:
    """Plot Frame Delivery Ratio & Packet Loss vs Injected Loss Rate."""
    loss_df = df[df["category"] == "loss_sweep"].copy()
    if loss_df.empty:
        return output_dir

    loss_df = loss_df.sort_values(by=["loss_percent"])

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5), dpi=300)

    # Subplot 1: Frame Delivery Ratio
    for proto, color in [("TCP", TCP_COLOR), ("UDP", UDP_COLOR)]:
        proto_data = loss_df[loss_df["protocol"] == proto]
        ax1.plot(
            proto_data["loss_percent"],
            proto_data["frame_delivery_ratio_percent"],
            marker="o",
            label=f"{proto}",
            color=color,
        )
    ax1.set_xlabel("Injected Packet Loss Rate (%)")
    ax1.set_ylabel("Frame Delivery Ratio (%)")
    ax1.set_title("Frame Delivery Ratio vs. Packet Loss")
    ax1.legend(frameon=True)
    ax1.grid(True, linestyle=":", alpha=0.6)

    # Subplot 2: Playback Stalls / Rebuffering
    for proto, color in [("TCP", TCP_COLOR), ("UDP", UDP_COLOR)]:
        proto_data = loss_df[loss_df["protocol"] == proto]
        ax2.plot(
            proto_data["loss_percent"],
            proto_data["total_stall_seconds"],
            marker="s",
            label=f"{proto} Stall Duration",
            color=color,
        )
    ax2.set_xlabel("Injected Packet Loss Rate (%)")
    ax2.set_ylabel("Total Playout Stall Duration (seconds)")
    ax2.set_title("Playout Stall Duration under Packet Loss")
    ax2.legend(frameon=True)
    ax2.grid(True, linestyle=":", alpha=0.6)

    out_file = output_dir / "fig3_packet_loss_resilience.png"
    plt.tight_layout()
    plt.savefig(out_file)
    plt.close()
    return out_file


def plot_jitter_analysis(df: pd.DataFrame, output_dir: Path = PLOTS_DIR) -> Path:
    """Plot RFC 3550 Jitter vs Injected Delay Variation."""
    jit_df = df[df["category"] == "jitter_sweep"].copy()
    if jit_df.empty:
        return output_dir

    jit_df = jit_df.sort_values(by=["jitter_ms"])

    fig, ax = plt.subplots(figsize=(8, 5), dpi=300)
    for proto, color in [("TCP", TCP_COLOR), ("UDP", UDP_COLOR)]:
        proto_data = jit_df[jit_df["protocol"] == proto]
        ax.plot(
            proto_data["jitter_ms"],
            proto_data["rfc3550_jitter_ms"],
            marker="^",
            label=f"{proto} RFC 3550 Jitter",
            color=color,
        )

    ax.set_xlabel("Injected Jitter (StdDev ms)")
    ax.set_ylabel("Measured RFC 3550 Interarrival Jitter (ms)")
    ax.set_title("TCP vs. UDP: RFC 3550 Jitter Response")
    ax.legend(frameon=True, loc="upper left")
    ax.grid(True, linestyle=":", alpha=0.6)

    out_file = output_dir / "fig4_jitter_analysis.png"
    plt.tight_layout()
    plt.savefig(out_file)
    plt.close()
    return out_file


def plot_profiles_comparison_barchart(df: pd.DataFrame, output_dir: Path = PLOTS_DIR) -> Path:
    """Grouped bar chart comparing TCP and UDP across realistic network profiles."""
    prof_df = df[df["category"] == "profiles"].copy()
    if prof_df.empty:
        return output_dir

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 9), dpi=300)

    # Bar chart 1: Throughput across profiles
    sns.barplot(
        data=prof_df,
        x="condition_name",
        y="throughput_mbps",
        hue="protocol",
        palette={"TCP": TCP_COLOR, "UDP": UDP_COLOR},
        ax=ax1,
    )
    ax1.set_xlabel("")
    ax1.set_ylabel("Throughput (Mbps)")
    ax1.set_title("Throughput Comparison Across Realistic Network Profiles")
    ax1.tick_params(axis="x", rotation=15)
    ax1.grid(True, linestyle=":", alpha=0.6)

    # Bar chart 2: Latency across profiles
    sns.barplot(
        data=prof_df,
        x="condition_name",
        y="latency_mean_ms",
        hue="protocol",
        palette={"TCP": TCP_COLOR, "UDP": UDP_COLOR},
        ax=ax2,
    )
    ax2.set_xlabel("Network Profile")
    ax2.set_ylabel("Mean Latency (ms)")
    ax2.set_title("Mean End-to-End Latency Across Realistic Network Profiles")
    ax2.tick_params(axis="x", rotation=15)
    ax2.grid(True, linestyle=":", alpha=0.6)

    out_file = output_dir / "fig5_profiles_comparison.png"
    plt.tight_layout()
    plt.savefig(out_file)
    plt.close()
    return out_file


def generate_all_figures(csv_path: Optional[Path] = None, output_dir: Path = PLOTS_DIR) -> List[Path]:
    """Generate all figures from master results CSV."""
    df = load_master_dataframe(csv_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    generated = []
    generated.append(plot_throughput_vs_bandwidth(df, output_dir))
    generated.append(plot_latency_vs_delay(df, output_dir))
    generated.append(plot_loss_resilience(df, output_dir))
    generated.append(plot_jitter_analysis(df, output_dir))
    generated.append(plot_profiles_comparison_barchart(df, output_dir))
    return [p for p in generated if p.exists() and p.is_file()]


if __name__ == "__main__":
    generated = generate_all_figures()
    print(f"Successfully generated {len(generated)} comparative figures in {PLOTS_DIR}")
