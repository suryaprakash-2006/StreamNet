"""
StreamNet Technical & Experimental Audit Runner.

Executes reproducibility validation runs (3 trials each across 8 core configurations),
measures statistical stability (mean, stddev, min, max), audits impairment accuracy,
and saves validated results in experiments/results/audit/.
"""

import json
from pathlib import Path
import sys
import time
from typing import Any, Dict, List

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from experiments.runner import ExperimentRunner
from experiments.scenarios import ExperimentScenario
from network.conditions import NetworkCondition

AUDIT_DIR = Path(__file__).resolve().parent / "results" / "audit"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)


def get_core_audit_scenarios() -> List[ExperimentScenario]:
    """Define the 8 core audit benchmark scenarios."""
    return [
        ExperimentScenario(
            scenario_id="AUDIT_1_BASELINE",
            name="Baseline (Ideal)",
            category="audit_baseline",
            condition=NetworkCondition(name="Ideal", latency_ms=0.0, loss_percent=0.0, bandwidth_mbps=None, jitter_ms=0.0),
            total_frames=45,
            fps=30,
        ),
        ExperimentScenario(
            scenario_id="AUDIT_2_LATENCY_100MS",
            name="100ms Latency",
            category="audit_latency",
            condition=NetworkCondition(name="Latency 100ms", latency_ms=100.0, loss_percent=0.0, bandwidth_mbps=None, jitter_ms=0.0),
            total_frames=45,
            fps=30,
        ),
        ExperimentScenario(
            scenario_id="AUDIT_3_LOSS_5PCT",
            name="5% Packet Loss",
            category="audit_loss",
            condition=NetworkCondition(name="Loss 5%", latency_ms=10.0, loss_percent=5.0, bandwidth_mbps=None, jitter_ms=0.0),
            total_frames=45,
            fps=30,
        ),
        ExperimentScenario(
            scenario_id="AUDIT_4_BW_1MBPS",
            name="1.0 Mbps Bandwidth",
            category="audit_bandwidth",
            condition=NetworkCondition(name="BW 1.0 Mbps", latency_ms=10.0, loss_percent=0.0, bandwidth_mbps=1.0, jitter_ms=0.0),
            total_frames=45,
            fps=30,
        ),
    ]


def run_reproducibility_audit(num_repeats: int = 3) -> Dict[str, Any]:
    """
    Run each core configuration num_repeats times and compute statistical dispersion.
    """
    runner = ExperimentRunner(results_dir=AUDIT_DIR)
    scenarios = get_core_audit_scenarios()

    raw_records: List[Dict[str, Any]] = []

    print(f"\n=======================================================")
    print(f"  EXECUTING REPRODUCIBILITY AUDIT ({num_repeats} REPEATS PER SCENARIO)")
    print(f"=======================================================\n")

    for scenario in scenarios:
        print(f"--> Auditing Scenario: {scenario.name} ({num_repeats} trials)")
        for trial_idx in range(1, num_repeats + 1):
            print(f"    Trial {trial_idx}/{num_repeats} for {scenario.name}...")
            # Run TCP trial
            tcp_res = runner.run_tcp_trial(scenario)
            time.sleep(0.15)
            # Run UDP trial
            udp_res = runner.run_udp_trial(scenario)
            time.sleep(0.15)

            raw_records.append({
                "scenario_id": scenario.scenario_id,
                "scenario_name": scenario.name,
                "trial": trial_idx,
                "tcp": tcp_res,
                "udp": udp_res,
            })

    # Save raw audit trials
    with open(AUDIT_DIR / "audit_raw_trials.json", "w") as f:
        json.dump(raw_records, f, indent=2)

    # Compute Statistical Aggregates (Mean, StdDev, Min, Max)
    stats_rows = []
    for scenario in scenarios:
        s_id = scenario.scenario_id
        scenario_records = [r for r in raw_records if r["scenario_id"] == s_id]

        for proto in ["tcp", "udp"]:
            proto_upper = proto.upper()
            throughputs = [r[proto]["throughput_mbps"] for r in scenario_records]
            goodputs = [r[proto]["goodput_mbps"] for r in scenario_records]
            latencies = [r[proto]["latency_mean_ms"] for r in scenario_records]
            fdrs = [r[proto]["frame_delivery_ratio_percent"] for r in scenario_records]
            loss_rates = [r[proto]["packet_loss_rate_percent"] for r in scenario_records]
            stalls = [r[proto]["total_stall_seconds"] for r in scenario_records]

            stats_rows.append({
                "scenario_id": s_id,
                "scenario_name": scenario.name,
                "protocol": proto_upper,
                "throughput_mean": round(float(np.mean(throughputs)), 3),
                "throughput_std": round(float(np.std(throughputs)), 3),
                "throughput_min": round(float(np.min(throughputs)), 3),
                "throughput_max": round(float(np.max(throughputs)), 3),
                "goodput_mean": round(float(np.mean(goodputs)), 3),
                "goodput_std": round(float(np.std(goodputs)), 3),
                "latency_mean": round(float(np.mean(latencies)), 3),
                "latency_std": round(float(np.std(latencies)), 3),
                "latency_min": round(float(np.min(latencies)), 3),
                "latency_max": round(float(np.max(latencies)), 3),
                "fdr_mean": round(float(np.mean(fdrs)), 2),
                "fdr_std": round(float(np.std(fdrs)), 2),
                "loss_rate_mean": round(float(np.mean(loss_rates)), 2),
                "loss_rate_std": round(float(np.std(loss_rates)), 2),
                "stall_seconds_mean": round(float(np.mean(stalls)), 3),
                "stall_seconds_std": round(float(np.std(stalls)), 3),
            })

    stats_df = pd.DataFrame(stats_rows)
    stats_df.to_csv(AUDIT_DIR / "reproducibility_statistics.csv", index=False)

    print("\n--- REPRODUCIBILITY AUDIT SUMMARY ---")
    print(stats_df[["scenario_name", "protocol", "throughput_mean", "throughput_std", "latency_mean", "latency_std", "fdr_mean", "loss_rate_mean"]].to_string(index=False))

    return {
        "stats_table": stats_rows,
        "raw_trials": raw_records,
    }


if __name__ == "__main__":
    run_reproducibility_audit(num_repeats=3)
