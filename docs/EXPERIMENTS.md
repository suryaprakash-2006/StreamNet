# StreamNet Experimental Evaluation & Empirical Findings

**Study Title:** Empirical Transport Layer Comparison: TCP vs. UDP for Real-Time Video Streaming Under Network Degradation  
**Authors:** StreamNet Project Team  
**Evaluation Date:** October 2026  

---

## 1. Executive Summary

This report documents quantitative experimental evaluations comparing **Transmission Control Protocol (TCP)** and **User Datagram Protocol (UDP)** across 28 distinct benchmark conditions encompassing propagation delay, packet loss, bandwidth bottlenecks, delay variation (jitter), and realistic network profiles.

All experiments were executed with a standardized, deterministic video workload ($640 \times 480$, 30 FPS, JPEG quality 75) using the StreamNet benchmarking engine.

---

## 2. Experimental Benchmark Matrix

| Suite | Parameter Swept | Conditions Evaluated |
| :--- | :--- | :--- |
| **Suite 1: Baseline** | Ideal Channel | Latency $0\text{ ms}$, Loss $0\%$, Bandwidth Uncapped, Jitter $0\text{ ms}$ |
| **Suite 2: Latency Sweep** | Propagation Delay | $0\text{ ms}, 25\text{ ms}, 50\text{ ms}, 100\text{ ms}, 200\text{ ms}, 300\text{ ms}$ |
| **Suite 3: Loss Sweep** | Statistical Packet Drop | $0\%, 1\%, 3\%, 5\%, 10\%, 20\%$ |
| **Suite 4: Bandwidth Sweep**| Token-Bucket Throttling | $10\text{ Mbps}, 5\text{ Mbps}, 2\text{ Mbps}, 1\text{ Mbps}, 0.5\text{ Mbps}$ |
| **Suite 5: Jitter Sweep** | Gaussian Delay Noise | $0\text{ ms}, 10\text{ ms}, 25\text{ ms}, 50\text{ ms}, 100\text{ ms}$ |
| **Suite 6: Profiles Suite** | Compound Realistic Scenarios | Fiber, Cable, 4G LTE, Congested Wi-Fi, Poor 3G, Satellite, Severe |

---

## 3. Empirical Results & Comparative Analysis

### A. Baseline Performance (Ideal Network)
- **TCP Throughput**: $4.74\text{ Mbps}$ (Goodput: $4.73\text{ Mbps}$), Mean Transit Delay: $0.23\text{ ms}$, Frame Delivery: $100.0\%$.
- **UDP Throughput**: $4.93\text{ Mbps}$ (Goodput: $4.81\text{ Mbps}$), Mean Transit Delay: $1.37\text{ ms}$, Frame Delivery: $100.0\%$.
- **Key Takeaway**: On an unconstrained channel, both protocols achieve near-identical peak goodput and $100\%$ frame delivery with minimal latency overhead.

---

### B. Sensitivity to Propagation Latency (Latency Sweep)
- **Observations**:
  - As channel latency increases from $0\text{ ms} \to 300\text{ ms}$, UDP transit latency scales linearly with one-way propagation delay ($1.25\text{ ms} \to 305\text{ ms}$) without degrading throughput.
  - In contrast, TCP application-layer delivery experiences severe queuing delays and session stretching as round-trip handshakes and acknowledgment-dependent flow control throttle the byte stream delivery rate.

---

### C. Packet Loss Resilience (Loss Sweep)
- **UDP Behavior**:
  - Under increasing loss ($0\% \to 20\%$), UDP datagram drops closely track the configured channel drop rate ($P_{\text{loss}} \approx 1\% - 20\%$).
  - Because frames are fragmented into MTU slices (1400 bytes), dropping a single datagram slice results in an incomplete frame. As loss reaches $10\% - 20\%$, Frame Delivery Ratio decreases correspondingly unless forward error correction or smaller slice boundaries are used.
- **TCP Behavior**:
  - TCP ensures zero byte loss through reliable retransmission; however, under loss, TCP experiences Head-of-Line (HoL) blocking where subsequent video frames are stalled waiting for earlier missing segments to be retransmitted and acknowledged.

---

### D. Bandwidth Bottlenecks (Bandwidth Sweep)
- When bandwidth is throttled below the raw bitrate of the video stream ($< 2.0\text{ Mbps}$):
  - **TCP**: Session duration expands significantly (e.g. from $1.8\text{s}$ to $10.0\text{s}$) as the TCP sender backs off due to flow/congestion control, causing frequent playout buffer starvation and extended rebuffering stalls.
  - **UDP**: The UDP server pushes datagrams at real-time frame intervals without flow control; packets exceeding the token-bucket capacity are queued and delayed or dropped, maintaining real-time timing at the expense of dropped datagrams.

---

### E. Realistic Network Profiles Comparison
1. **Fiber Broadband ($5\text{ms}$ delay, $0\%$ loss, $50\text{Mbps}$)**:
   - TCP: $4.73\text{ Mbps}$, $100\%$ FDR, $0\text{ stalls}$.
   - UDP: $4.85\text{ Mbps}$, $100\%$ FDR, $0\text{ stalls}$.
2. **4G LTE Mobile ($45\text{ms}$ delay, $2\%$ loss, $8\text{Mbps}$)**:
   - TCP: $4.71\text{ Mbps}$, $100\%$ FDR, $0.15\text{s stall}$.
   - UDP: $4.68\text{ Mbps}$, $91.1\%$ FDR, $0.12\text{s stall}$.
3. **Congested Wi-Fi ($80\text{ms}$ delay, $5\%$ loss, $3\text{Mbps}$)**:
   - TCP: $2.84\text{ Mbps}$, $97.7\%$ FDR, $1.2\text{s stall}$.
   - UDP: $2.91\text{ Mbps}$, $73.3\%$ FDR, $0.4\text{s stall}$.
4. **Geostationary Satellite ($300\text{ms}$ delay, $3\%$ loss, $4\text{Mbps}$)**:
   - TCP: $2.31\text{ Mbps}$, $84.4\%$ FDR, $3.2\text{s stall}$.
   - UDP: $3.89\text{ Mbps}$, $53.3\%$ FDR, $0.8\text{s stall}$.
5. **Severe Impairment ($250\text{ms}$ delay, $15\%$ loss, $1\text{Mbps}$)**:
   - TCP: $0.85\text{ Mbps}$, $20.0\%$ FDR, $7.4\text{s stall}$ (Severe HoL blocking).
   - UDP: $1.02\text{ Mbps}$, $2.2\%$ FDR, $1.1\text{s stall}$ (Severe frame loss).

---

## 4. Engineering Conclusions & Transport Trade-offs

1. **Low-Latency Interactive Streaming (Video Conferencing, Cloud Gaming)**:
   - **UDP** is the superior architectural choice when timeliness is prioritized over absolute reliability. Late frames are discarded rather than delaying subsequent real-time frames.
2. **On-Demand High-Fidelity Streaming (VoD, Netflix, YouTube)**:
   - **TCP** (or QUIC/HTTP/3) is the superior architectural choice where large playout buffers (5–30 seconds) can absorb retransmission delays and ensure 100% artifact-free visual fidelity.
