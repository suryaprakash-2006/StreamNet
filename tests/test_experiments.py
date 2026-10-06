"""
Integration tests for the Experiment Runner and Visualization Engine.
"""

import json
from pathlib import Path
import pytest

from experiments.runner import ExperimentRunner
from experiments.scenarios import ExperimentScenario
from experiments.visualizer import generate_all_figures, load_master_dataframe
from network.conditions import NetworkCondition


def test_experiment_runner_single_scenario(tmp_path: Path):
    """Verify ExperimentRunner runs a scenario, produces JSON and CSV, and computes valid metrics."""
    runner = ExperimentRunner(results_dir=tmp_path)

    scenario = ExperimentScenario(
        scenario_id="TEST_BASELINE",
        name="Test Baseline",
        category="test",
        condition=NetworkCondition(name="Ideal", latency_ms=0.0, loss_percent=0.0),
        total_frames=15,
        fps=60,
        width=160,
        height=120,
        jpeg_quality=50,
    )

    record = runner.run_scenario(scenario)

    assert "tcp_results" in record
    assert "udp_results" in record
    assert record["tcp_results"]["frames_delivered"] == 15
    assert record["udp_results"]["frames_delivered"] == 15

    # Check JSON file
    json_files = list(tmp_path.glob("*.json"))
    assert len(json_files) == 1
    with open(json_files[0], "r") as f:
        loaded = json.load(f)
    assert loaded["scenario_id"] == "TEST_BASELINE"

    # Check CSV file
    csv_file = tmp_path / "master_experiment_results.csv"
    assert csv_file.exists()
    df = load_master_dataframe(csv_file)
    assert len(df) == 2  # 1 TCP row + 1 UDP row
    assert set(df["protocol"]) == {"TCP", "UDP"}
