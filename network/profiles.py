"""
StreamNet Predefined Network Degradation Profiles.

Standardized realistic and synthetic network degradation profiles
for reproducible comparative evaluation.
"""

from typing import Dict
from network.conditions import NetworkCondition

# Predefined standardized profiles
PROFILES: Dict[str, NetworkCondition] = {
    "IDEAL": NetworkCondition(
        name="Ideal / Local Network",
        latency_ms=0.0,
        jitter_ms=0.0,
        loss_percent=0.0,
        bandwidth_mbps=None,
    ),
    "FIBER_EXCELLENT": NetworkCondition(
        name="Fiber Broadband (Excellent)",
        latency_ms=5.0,
        jitter_ms=1.0,
        loss_percent=0.0,
        bandwidth_mbps=50.0,
    ),
    "CABLE_GOOD": NetworkCondition(
        name="Cable Broadband (Good)",
        latency_ms=25.0,
        jitter_ms=5.0,
        loss_percent=0.5,
        bandwidth_mbps=20.0,
    ),
    "MOBILE_4G": NetworkCondition(
        name="4G LTE Mobile",
        latency_ms=45.0,
        jitter_ms=15.0,
        loss_percent=2.0,
        bandwidth_mbps=8.0,
    ),
    "CONGESTED_WIFI": NetworkCondition(
        name="Congested 2.4GHz Wi-Fi",
        latency_ms=80.0,
        jitter_ms=30.0,
        loss_percent=5.0,
        bandwidth_mbps=3.0,
    ),
    "POOR_CELLULAR_3G": NetworkCondition(
        name="Poor Cellular / 3G",
        latency_ms=150.0,
        jitter_ms=45.0,
        loss_percent=8.0,
        bandwidth_mbps=1.5,
    ),
    "SATELLITE_GEO": NetworkCondition(
        name="Geostationary Satellite",
        latency_ms=300.0,
        jitter_ms=20.0,
        loss_percent=3.0,
        bandwidth_mbps=4.0,
    ),
    "SEVERE_DEGRADATION": NetworkCondition(
        name="Severe Network Impairment",
        latency_ms=250.0,
        jitter_ms=60.0,
        loss_percent=15.0,
        bandwidth_mbps=1.0,
    ),
}


def get_profile(name: str) -> NetworkCondition:
    """Retrieve predefined profile by name (case-insensitive)."""
    normalized = name.upper().strip()
    if normalized in PROFILES:
        return PROFILES[normalized]
    raise KeyError(f"Profile '{name}' not found. Available: {list(PROFILES.keys())}")
