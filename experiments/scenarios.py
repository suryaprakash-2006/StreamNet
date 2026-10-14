"""
StreamNet Experiment Scenarios and Benchmark Matrix Definitions.

Defines reproducible benchmark suites:
  - Baseline (Ideal channel)
  - Latency Sweep
  - Packet Loss Sweep
  - Bandwidth Constraint Sweep
  - Jitter Sweep
  - Compound Realistic Profiles Suite
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from network.conditions import NetworkCondition
from network.profiles import PROFILES


@dataclass
class ExperimentScenario:
    """Configuration for a single comparative experiment run."""
    scenario_id: str
    name: str
    category: str
    condition: NetworkCondition
    total_frames: int = 150
    fps: int = 30
    width: int = 640
    height: int = 480
    jpeg_quality: int = 75
    trials: int = 1


def build_baseline_suite(total_frames: int = 150, fps: int = 30) -> List[ExperimentScenario]:
    """Baseline / Ideal network condition."""
    return [
        ExperimentScenario(
            scenario_id="EXP1_BASELINE",
            name="Baseline (Ideal Network)",
            category="baseline",
            condition=NetworkCondition(name="Ideal", latency_ms=0.0, loss_percent=0.0, bandwidth_mbps=None, jitter_ms=0.0),
            total_frames=total_frames,
            fps=fps,
        )
    ]


def build_latency_sweep(total_frames: int = 150, fps: int = 30) -> List[ExperimentScenario]:
    """Latency sweep: 0ms, 25ms, 50ms, 100ms, 200ms, 300ms."""
    delays = [0.0, 25.0, 50.0, 100.0, 200.0, 300.0]
    return [
        ExperimentScenario(
            scenario_id=f"EXP2_LATENCY_{int(d)}MS",
            name=f"Latency {int(d)}ms",
            category="latency_sweep",
            condition=NetworkCondition(name=f"Latency {int(d)}ms", latency_ms=d, loss_percent=0.0, bandwidth_mbps=None, jitter_ms=0.0),
            total_frames=total_frames,
            fps=fps,
        )
        for d in delays
    ]


def build_loss_sweep(total_frames: int = 150, fps: int = 30) -> List[ExperimentScenario]:
    """Packet loss sweep: 0%, 1%, 3%, 5%, 10%, 20%."""
    losses = [0.0, 1.0, 3.0, 5.0, 10.0, 20.0]
    return [
        ExperimentScenario(
            scenario_id=f"EXP3_LOSS_{int(loss)}PCT" if loss == int(loss) else f"EXP3_LOSS_{str(loss).replace('.', '_')}PCT",
            name=f"Loss {loss}%",
            category="loss_sweep",
            condition=NetworkCondition(name=f"Loss {loss}%", latency_ms=10.0, loss_percent=loss, bandwidth_mbps=None, jitter_ms=0.0),
            total_frames=total_frames,
            fps=fps,
        )
        for loss in losses
    ]


def build_bandwidth_sweep(total_frames: int = 150, fps: int = 30) -> List[ExperimentScenario]:
    """Bandwidth limitation sweep: 10Mbps, 5Mbps, 2Mbps, 1Mbps, 0.5Mbps."""
    bw_caps = [10.0, 5.0, 2.0, 1.0, 0.5]
    return [
        ExperimentScenario(
            scenario_id=f"EXP4_BW_{str(bw).replace('.', '_')}MBPS",
            name=f"Bandwidth {bw} Mbps",
            category="bandwidth_sweep",
            condition=NetworkCondition(name=f"BW {bw} Mbps", latency_ms=10.0, loss_percent=0.0, bandwidth_mbps=bw, jitter_ms=0.0),
            total_frames=total_frames,
            fps=fps,
        )
        for bw in bw_caps
    ]


def build_jitter_sweep(total_frames: int = 150, fps: int = 30) -> List[ExperimentScenario]:
    """Jitter variation sweep: 0ms, 10ms, 25ms, 50ms, 100ms."""
    jitters = [0.0, 10.0, 25.0, 50.0, 100.0]
    return [
        ExperimentScenario(
            scenario_id=f"EXP5_JITTER_{int(j)}MS",
            name=f"Jitter {int(j)}ms",
            category="jitter_sweep",
            condition=NetworkCondition(name=f"Jitter {int(j)}ms", latency_ms=50.0, loss_percent=0.0, bandwidth_mbps=None, jitter_ms=j),
            total_frames=total_frames,
            fps=fps,
        )
        for j in jitters
    ]


def build_profiles_suite(total_frames: int = 150, fps: int = 30) -> List[ExperimentScenario]:
    """Realistic network profiles suite."""
    scenarios = []
    for profile_id, condition in PROFILES.items():
        scenarios.append(
            ExperimentScenario(
                scenario_id=f"EXP6_PROFILE_{profile_id}",
                name=condition.name,
                category="profiles",
                condition=condition,
                total_frames=total_frames,
                fps=fps,
            )
        )
    return scenarios
