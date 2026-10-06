# StreamNet Performance Metrics & Measurement Methodology

**Document Version:** 1.0.0  
**Status:** Approved Specification  

---

## 1. Principles of Experimental Measurement

To preserve scientific integrity and computer networking rigor:
1. **Experimental Neutrality**: The system makes no prior assumptions about whether TCP or UDP is superior for any network condition. The experimental metrics dictate the conclusions.
2. **Deterministic Baseline Workload**: Both TCP and UDP are fed the exact same video frame sequence, resolution, compression quality, and frame rate.
3. **No Fabricated Telemetry**: All metrics are calculated in real time from observed packet arrival events and byte counters.

---

## 2. Timing & Latency Measurement Methodology

### Clock Synchronization & Timing Reference
Measuring true one-way latency between two physically separate hosts across a network requires sub-millisecond clock synchronization (e.g. IEEE 1588 PTP or calibrated NTP). Without hardware clock sync, clock drift and offsets can introduce significant measurement errors into one-way latency.

#### In StreamNet:
1. **Local Loopback / Shared Clock Reference**: When running experiments on the same host (client, server, and network impairment proxy), all processes share the host OS monotonic clock (`time.perf_counter_ns()`).
   - One-way transit delay is calculated directly:
     $$L_i = \frac{t_{\text{client\_recv\_ns}, i} - t_{\text{server\_send\_ns}, i}}{10^6} \quad (\text{ms})$$
   - Because `time.perf_counter_ns()` provides nanosecond resolution with zero clock skew across processes on the same machine, one-way delay is measured with high precision.
2. **Multi-Host / Distributed Reference**: When operating across distinct network endpoints, the system can utilize:
   - **Round-Trip Time (RTT) Probing**: Periodically issuing `MSG_PING` / `MSG_PONG` control frames where the round-trip duration $RTT = t_{\text{pong\_recv}} - t_{\text{ping\_send}}$ is measured entirely on the sender's clock, estimating one-way delay as $\frac{RTT}{2}$.
   - All experiment logs explicitly note the timing mode (`LOCAL_MONOTONIC_CLOCK` vs `RTT_ESTIMATED`).

---

## 3. Quantitative Metric Formulations

### A. Raw Throughput ($T$)
The total volume of transport-layer data (payload + application headers) delivered across the socket per unit time:
$$T = \frac{\sum_{i=1}^{N} \text{Bytes Received}_i \times 8}{(t_{\text{end}} - t_{\text{start}}) \times 10^6} \quad (\text{Mbps})$$

### B. Application Goodput ($G$)
The volume of usable, uncorrupted video payload successfully reconstructed and delivered to the application/decoder per unit time:
$$G = \frac{\sum_{i=1}^{M} \text{Payload Bytes (Completed Frames)}_i \times 8}{(t_{\text{end}} - t_{\text{start}}) \times 10^6} \quad (\text{Mbps})$$

- **TCP**: Goodput accounts for framing overhead and transmission stalls during retransmissions.
- **UDP**: Goodput accounts for lost chunks and frames dropped due to incomplete reassembly.

---

### C. Jitter ($J$) — Standard RFC 3550 Formulation
Jitter is the statistical variance in packet transit delay. StreamNet implements the standard IETF RFC 3550 algorithm used in RTP/RTCP:

1. For consecutive packets $i-1$ and $i$, with server transmission times $S_{i-1}, S_i$ and client reception times $R_{i-1}, R_i$:
   $$D(i-1, i) = (R_i - R_{i-1}) - (S_i - S_{i-1}) = (R_i - S_i) - (R_{i-1} - S_{i-1})$$
2. The running estimator $J_i$ is updated using a 1st-order low-pass filter (gain factor $\frac{1}{16} = 0.0625$):
   $$J_i = J_{i-1} + \frac{|D(i-1, i)| - J_{i-1}}{16}$$

This provides a stable, standardized metric for delay variation across both protocols.

---

### D. Packet Loss Rate ($P_{\text{loss}}$) — UDP
For UDP streams, every datagram carries a monotonically increasing 32-bit sequence number `packet_seq_num`:
$$\text{Expected Packets} = \text{seq}_{\text{max}} - \text{seq}_{\text{min}} + 1$$
$$\text{Lost Packets} = \text{Expected Packets} - \text{Received Packets}$$
$$P_{\text{loss}} = \left( \frac{\text{Lost Packets}}{\text{Expected Packets}} \right) \times 100\%$$

Out-of-order arrivals are detected whenever $\text{seq}_i < \text{seq}_{\text{highest\_seen}}$.

---

### E. Frame Delivery Ratio ($FDR$)
Measures the reliability of complete video frame reconstruction:
$$FDR = \left( \frac{N_{\text{frames\_assembled\_complete}}}{N_{\text{frames\_transmitted}}} \right) \times 100\%$$

---

### F. Playback Rebuffering & Stall Metrics
When a playout buffer is utilized:
- **Rebuffering Count ($N_{\text{stall}}$)**: The number of times the playout buffer was drained to zero while waiting for incoming frames.
- **Total Stall Time ($T_{\text{stall}}$)**: Cumulative time (in seconds) spent in the `REBUFFERING` state.
- **Stall Ratio**:
  $$\text{Stall Ratio} = \frac{T_{\text{stall}}}{T_{\text{playback\_total}}} \times 100\%$$

---

## 4. Telemetry Storage Format

Each experiment run persists telemetry in structured JSON and CSV formats:

```json
{
  "experiment_id": "EXP-20261006-120000-01",
  "protocol": "UDP",
  "workload": {
    "total_frames": 300,
    "fps": 30,
    "resolution": "640x480",
    "encoding": "JPEG",
    "quality": 75
  },
  "network_condition": {
    "latency_ms": 50.0,
    "loss_percent": 3.0,
    "bandwidth_mbps": 5.0,
    "jitter_ms": 10.0
  },
  "timing_mode": "LOCAL_MONOTONIC_CLOCK",
  "metrics": {
    "duration_seconds": 10.02,
    "bytes_received": 1420500,
    "throughput_mbps": 1.134,
    "goodput_mbps": 1.082,
    "packets_sent": 1050,
    "packets_received": 1018,
    "packet_loss_rate_percent": 3.048,
    "out_of_order_packets": 2,
    "mean_latency_ms": 50.42,
    "p95_latency_ms": 54.12,
    "rfc3550_jitter_ms": 3.21,
    "frames_sent": 300,
    "frames_delivered": 288,
    "frame_delivery_ratio_percent": 96.0,
    "rebuffering_events": 1,
    "total_stall_seconds": 0.08
  }
}
```
