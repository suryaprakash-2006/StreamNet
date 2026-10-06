"""
StreamNet Network Degradation Conditions and Parameters.

Defines configurable network impairment parameters:
  - Latency (propagation delay in milliseconds)
  - Jitter (delay variation in milliseconds)
  - Packet Loss (drop rate in percentage)
  - Bandwidth (traffic throttling in Megabits per second)
  - Burst Loss (Gilbert-Elliott Markov state transition probabilities)
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class NetworkCondition:
    """
    Specification of network impairment parameters.
    """
    name: str = "Custom"
    latency_ms: float = 0.0          # Base one-way latency in milliseconds
    jitter_ms: float = 0.0           # Latency jitter (stddev of Gaussian delay noise) in ms
    loss_percent: float = 0.0        # Statistical packet drop rate (0.0 to 100.0%)
    bandwidth_mbps: Optional[float] = None  # Bandwidth cap in Mbps (None or <= 0 for unlimited)
    burst_loss_prob: float = 0.0     # Conditional probability of burst loss (0.0 for independent Bernoulli)

    def is_ideal(self) -> bool:
        """Return True if network has no degradation."""
        return (
            self.latency_ms <= 0.0
            and self.jitter_ms <= 0.0
            and self.loss_percent <= 0.0
            and (self.bandwidth_mbps is None or self.bandwidth_mbps <= 0.0)
        )
