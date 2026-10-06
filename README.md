# StreamNet
### TCP vs. UDP for Real-Time Video Streaming Under Network Degradation

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/pytest-18%20passed-brightgreen.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Architecture: Socket-Level Impairment](https://img.shields.io/badge/Network-Socket--Level%20Impairment%20Proxy-cyan.svg)]()

---

## 1. Project Overview & Motivation

In real-time multimedia communication (such as video conferencing, cloud gaming, and live streaming), choosing between **Transmission Control Protocol (TCP)** and **User Datagram Protocol (UDP)** represents a fundamental networking trade-off between **reliability** and **timeliness**:

- **TCP** guarantees 100% reliable, in-order byte delivery via sequence numbers, acknowledgments, retransmissions, and congestion control (AIMD/CUBIC/BBR). However, under packet loss or jitter, TCP experiences **Head-of-Line (HoL) Blocking** and buffer buildup, which can cause severe playout stalls and latency spikes.
- **UDP** provides connectionless, lightweight datagram delivery without retransmission delays or artificial transport-level flow control. However, datagram drops or out-of-order delivery directly impact video frame reconstruction.

**StreamNet** is an experimental evaluation platform designed to answer the core networking question:
> *"How do TCP and UDP differ in real-time video streaming performance under varying levels of network degradation, and under what network conditions does each transport protocol provide better performance?"*

StreamNet streams the exact same synthetic or recorded video workload over native TCP and UDP sockets while introducing controlled network degradations (latency, packet loss, bandwidth bottlenecks, and jitter) through a socket-level impairment proxy, collecting quantitative performance telemetry in real time.

---

## 2. Computer Networks Concepts Demonstrated

| Networking Concept | Implementation in StreamNet |
| :--- | :--- |
| **Socket Programming** | Low-level POSIX/Berkeley sockets (`socket.SOCK_STREAM` for TCP, `socket.SOCK_DGRAM` for UDP). |
| **Stream Framing** | Fixed 24-byte binary application header with exact-byte parsing loop (`recv_exact`). |
| **Datagram Packetization** | Fixed 32-byte header with MTU fragmentation (1400B chunks), frame indexing, and global sequence numbers. |
| **Reliability & Head-of-Line Blocking** | Experimental observation of TCP stall duration during loss/bottlenecks vs UDP immediate frame drop. |
| **RFC 3550 Jitter** | Standardized RTP interarrival jitter 1st-order smoothing filter calculation. |
| **Token-Bucket Traffic Shaping** | Simulated router bottleneck queue with token replenishment and queue delay. |
| **Playout Buffer State Machine** | Real-time buffer tracking states: `INITIAL_BUFFERING` $\to$ `PLAYING` $\to$ `REBUFFERING` $\to$ `FINISHED`. |
| **Statistical Loss Modeling** | Bernoulli independent packet drop and Gilbert-Elliott burst Markov loss simulation. |

---

## 3. System Architecture

```
                          ┌────────────────────────────────────────────────────────┐
                          │                   STREAMNET SERVER                     │
                          │  ┌──────────────────┐    ┌──────────────────────────┐  │
                          │  │   Video Source   │───▶│ Framing / Packetization  │  │
                          │  │ (Synthetic / File│    │  (TCP Stream / UDP Chunks│  │
                          │  └──────────────────┘    └────────────┬─────────────┘  │
                          └───────────────────────────────────────┼────────────────┘
                                                                  │
                                                                  ▼
                          ┌────────────────────────────────────────────────────────┐
                          │         SOCKET-LEVEL NETWORK IMPAIRMENT PROXY          │
                          │  ┌──────────────────────────────────────────────────┐  │
                          │  │  • Latency Injection (Pipelined Priority Queue)  │  │
                          │  │  • Jitter Engine (Gaussian Delay Variation)      │  │
                          │  │  • Packet Loss (Bernoulli Drop Generator)        │  │
                          │  │  • Bandwidth Limiter (Token-Bucket Rate Limiter) │  │
                          │  └────────────────────────────┬─────────────────────┘  │
                          └───────────────────────────────┼────────────────────────┘
                                                          │
                                                          ▼
                          ┌────────────────────────────────────────────────────────┐
                          │                   STREAMNET CLIENT                     │
                          │  ┌──────────────────────────┐   ┌───────────────────┐  │
                          │  │  Reassembly / Reception  │──▶│   Jitter Buffer   │  │
                          │  └────────────┬─────────────┘   └─────────┬─────────┘  │
                          │               │                           │            │
                          │               ▼                           ▼            │
                          │  ┌──────────────────────────┐   ┌───────────────────┐  │
                          │  │ Metric Telemetry Engine  │   │ Video HUD Display │  │
                          │  └──────────────────────────┘   └───────────────────┘  │
                          └────────────────────────────────────────────────────────┘
                                                          │
                                                          ▼
                          ┌────────────────────────────────────────────────────────┐
                          │              ANALYSIS & EXPERIMENT ENGINE              │
                          │  ┌──────────────────────────┐   ┌───────────────────┐  │
                          │  │ Matrix Benchmark Sweeps  │──▶│ CSV / JSON Logger │  │
                          │  └──────────────────────────┘   └─────────┬─────────┘  │
                          │                                           │            │
                          │                                           ▼            │
                          │                         ┌───────────────────────────┐  │
                          │                         │ FastAPI / Web Dashboard   │  │
                          │                         └───────────────────────────┘  │
                          └────────────────────────────────────────────────────────┘
```

> [!NOTE]
> **Network Emulation Scope**: StreamNet operates as a user-space **socket-level impairment layer** to guarantee cross-platform execution without requiring administrative root privileges or OS kernel drivers (such as Linux `tc/netem` or Windows WinDivert). For UDP, datagram-level dropping and delaying have identical fidelity to network router drop/queuing. For TCP, stream-level rate limiting and delayed byte delivery model bottleneck delay and throughput throttling.

---

## 4. Binary Protocol Specifications

All multi-byte integer fields are encoded in **Network Byte Order (Big-Endian)**.

### A. TCP Application Header (24 Bytes)
`!2sBBB3sIQI`
- `magic (2B)`: `0x53 0x4E` (`b'SN'`)
- `version (1B)`: `0x01`
- `msg_type (1B)`: `1=DATA_FRAME, 3=START, 4=END`
- `flags (1B)`: `0x01=KEYFRAME, 0x04=EOS`
- `reserved (3B)`: `0x00 0x00 0x00`
- `frame_id (4B)`: Monotonic frame counter
- `timestamp_ns (8B)`: 64-bit sender monotonic timestamp (`time.perf_counter_ns()`)
- `payload_len (4B)`: 32-bit payload byte count

### B. UDP Datagram Header (32 Bytes)
`!2sBBB3sIIHHQI`
- `magic (2B)`, `version (1B)`, `msg_type (1B)`, `flags (1B)`, `reserved (3B)`
- `frame_id (4B)`: Video frame counter
- `packet_seq_num (4B)`: Global monotonic datagram sequence number for loss & out-of-order tracking
- `total_chunks (2B)`: Total fragments in this frame
- `chunk_index (2B)`: Fragment index (`0` to `total_chunks - 1`)
- `timestamp_ns (8B)`: 64-bit sender timestamp
- `payload_len (4B)`: Datagram chunk payload length (Max 1400B for safe Ethernet MTU)

---

## 5. Performance Metrics & Mathematical Formulations

1. **Throughput ($T$)**: Total bytes (header + payload) received per second across the transport socket:
   $$T = \frac{\sum \text{Bytes Received} \times 8}{(t_{\text{end}} - t_{\text{start}}) \times 10^6} \quad (\text{Mbps})$$
2. **Goodput ($G$)**: Usable video payload bytes successfully reconstructed and delivered to the decoder per second:
   $$G = \frac{\sum \text{Delivered Payload Bytes} \times 8}{(t_{\text{end}} - t_{\text{start}}) \times 10^6} \quad (\text{Mbps})$$
3. **End-to-End Latency ($L_i$)**: One-way transit delay per packet:
   $$L_i = \frac{t_{\text{client\_recv\_ns}, i} - t_{\text{server\_send\_ns}, i}}{10^6} \quad (\text{ms})$$
4. **RFC 3550 Interarrival Jitter ($J$)**:
   $$D(i-1, i) = (R_i - R_{i-1}) - (S_i - S_{i-1})$$
   $$J_i = J_{i-1} + \frac{|D(i-1, i)| - J_{i-1}}{16}$$
5. **Packet Loss Rate ($P_{\text{loss}}$)**:
   $$P_{\text{loss}} = \left( 1 - \frac{N_{\text{received\_packets}}}{N_{\text{expected\_packets}}} \right) \times 100\%$$
6. **Frame Delivery Ratio ($FDR$)**:
   $$FDR = \left( \frac{N_{\text{frames\_assembled}}}{N_{\text{frames\_sent}}} \right) \times 100\%$$

---

## 6. Installation & Dependencies

StreamNet requires **Python 3.10+**.

```bash
# Clone the repository
git clone https://github.com/your-username/StreamNet.git
cd StreamNet

# Install required dependencies
pip install -r requirements.txt
```

---

## 7. Quickstart & Usage

StreamNet includes a unified CLI (`streamnet.py`):

### A. Run Automated Benchmark Sweeps
Execute all 6 benchmark suites (28 scenarios) comparing TCP vs UDP:
```bash
python streamnet.py experiment --suite all --frames 60 --fps 30
```
Available suites: `all`, `baseline`, `latency`, `loss`, `bandwidth`, `jitter`, `profiles`.

### B. Launch Interactive Web Dashboard
```bash
python streamnet.py dashboard --port 8000
```
Open **`http://127.0.0.1:8000`** in any web browser to:
- Adjust real-time network impairment sliders (Latency, Loss, Bandwidth, Jitter).
- Trigger live paired TCP vs UDP benchmarks.
- View comparative KPI cards and dynamic Chart.js visualizations.
- Browse the publication-grade scientific figure gallery.

### C. Run Standalone Streaming with Live OpenCV HUD
**Terminal 1 (Start TCP Server):**
```bash
python streamnet.py server --protocol tcp --port 5001 --frames 300 --fps 30
```
**Terminal 2 (Start TCP Client with Video GUI & Telemetry HUD):**
```bash
python streamnet.py client --protocol tcp --port 5001 --gui
```

**For UDP Streaming:**
```bash
# Terminal 1: Client
python streamnet.py client --protocol udp --port 5002 --gui

# Terminal 2: Server
python streamnet.py server --protocol udp --target-host 127.0.0.1 --port 5002 --frames 300 --fps 30
```

### D. Run Standalone Network Impairment Proxy
Simulate a degraded 4G network between server and client:
```bash
python streamnet.py proxy --protocol udp --listen-port 6002 --target-port 5002 --latency 50 --loss 3 --bandwidth 5 --jitter 15
```

---

## 8. Real Experimental Findings & Summary

From 28 executed benchmark runs across identical video workloads:

| Scenario / Profile | Network Condition | TCP Throughput | TCP Frame Delivery | TCP Stalls | UDP Throughput | UDP Frame Delivery | UDP Loss Rate |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Baseline (Ideal)** | $0\text{ms}, 0\%, \infty\text{BW}$ | **4.74 Mbps** | **100.0%** | $1.39\text{s}$ | **4.93 Mbps** | **100.0%** | **0.0%** |
| **Latency 100ms** | $100\text{ms}, 0\%, \infty\text{BW}$ | $0.33\text{ Mbps}$ | $85.0\%$ | $33.8\text{s}$ | **4.61 Mbps** | **96.7%** | **0.0%** |
| **Loss 5%** | $10\text{ms}, 5\%, \infty\text{BW}$ | $4.78\text{ Mbps}$ | $100.0\%$ | $1.34\text{s}$ | $4.91\text{ Mbps}$ | **55.6%** | **5.4%** |
| **Loss 10%** | $10\text{ms}, 10\%, \infty\text{BW}$ | $4.76\text{ Mbps}$ | $100.0\%$ | $1.36\text{s}$ | $4.87\text{ Mbps}$ | **28.9%** | **11.2%** |
| **BW 1.0 Mbps** | $10\text{ms}, 0\%, 1\text{Mbps}$ | $1.05\text{ Mbps}$ | $24.4\%$ | $6.21\text{s}$ | $1.08\text{ Mbps}$ | **2.2%** | **0.0%** |
| **4G LTE Profile** | $45\text{ms}, 2\%, 8\text{Mbps}$ | **4.71 Mbps** | **100.0%** | $1.42\text{s}$ | **4.68 Mbps** | **91.1%** | **2.1%** |
| **Congested Wi-Fi**| $80\text{ms}, 5\%, 3\text{Mbps}$ | $2.84\text{ Mbps}$ | $97.7\%$ | $2.61\text{s}$ | $2.91\text{ Mbps}$ | **73.3%** | **5.6%** |
| **Satellite GEO** | $300\text{ms}, 3\%, 4\text{Mbps}$ | $2.31\text{ Mbps}$ | $84.4\%$ | $3.21\text{s}$ | $3.89\text{ Mbps}$ | **53.3%** | **3.2%** |

*Detailed empirical analysis available in [`docs/EXPERIMENTS.md`](file:///h:/ACADEMICS/PROJECTS/StreamNet/docs/EXPERIMENTS.md).*

---

## 9. Running the Test Suite

StreamNet includes full automated unit and integration tests with 100% pass rate:

```bash
pytest -v
```
```
============================= test session starts =============================
collected 18 items

tests/test_emulator.py::test_udp_emulator_latency_injection PASSED       [  5%]
tests/test_emulator.py::test_udp_emulator_packet_loss PASSED             [ 11%]
tests/test_emulator.py::test_tcp_emulator_bandwidth_throttling PASSED    [ 16%]
tests/test_experiments.py::test_experiment_runner_single_scenario PASSED [ 22%]
tests/test_m1_networking.py::test_tcp_streaming_loopback PASSED          [ 27%]
tests/test_m1_networking.py::test_udp_streaming_loopback PASSED          [ 33%]
tests/test_metrics.py::test_metrics_collector_basic PASSED               [ 38%]
tests/test_metrics.py::test_udp_packet_loss_and_out_of_order PASSED      [ 44%]
tests/test_metrics.py::test_rfc3550_jitter_calculation PASSED            [ 50%]
tests/test_protocol.py::test_tcp_header_pack_unpack PASSED               [ 55%]
tests/test_protocol.py::test_tcp_header_invalid_magic PASSED             [ 61%]
tests/test_protocol.py::test_tcp_header_short_buffer PASSED              [ 66%]
tests/test_protocol.py::test_udp_header_pack_unpack PASSED               [ 72%]
tests/test_protocol.py::test_udp_header_invalid_magic PASSED             [ 77%]
tests/test_video_streaming.py::test_video_source_generation PASSED       [ 83%]
tests/test_video_streaming.py::test_playout_buffer_state_transitions PASSED [ 88%]
tests/test_video_streaming.py::test_tcp_video_streaming_integration PASSED [ 94%]
tests/test_video_streaming.py::test_udp_video_streaming_integration PASSED [100%]

============================= 18 passed in 7.20s ==============================
```

---

## 10. Project Directory Structure

```
StreamNet/
├── client/                   # Streaming client subsystem
│   ├── buffer.py             # Playout buffer state machine & stall tracking
│   ├── playback.py           # Video decoder & live telemetry HUD renderer
│   ├── tcp_client.py         # Stream-oriented TCP receiver
│   └── udp_client.py         # Datagram-oriented UDP receiver & chunk reassembly
├── common/                   # Protocol definitions
│   └── protocol.py           # Binary packing for 24B TCP and 32B UDP headers
├── dashboard/                # Web evaluation dashboard
│   ├── app.py                # FastAPI backend API
│   ├── static/               # Static assets
│   └── templates/
│       └── index.html        # Interactive Tailwind + Chart.js dashboard
├── docs/                     # Detailed specifications
│   ├── ARCHITECTURE.md       # Architecture, technical trade-offs & proxy boundaries
│   ├── EXPERIMENTS.md        # Comprehensive experimental data & empirical findings
│   ├── METRICS.md            # Mathematical formulations & timing methodology
│   └── PROTOCOL.md           # Binary header layout & message structures
├── experiments/              # Benchmarking engine
│   ├── results/              # Real experiment JSON logs and master CSV
│   │   └── plots/            # Generated publication-grade comparative PNG figures
│   ├── runner.py             # Automated scenario matrix runner
│   ├── scenarios.py          # Benchmark sweep definitions
│   └── visualizer.py         # Matplotlib / Seaborn comparative plotting
├── metrics/                  # Real-time telemetry accumulation
│   └── collector.py          # RFC 3550 jitter, throughput, goodput, latency, loss
├── network/                  # Socket-level impairment proxy
│   ├── conditions.py         # Network condition data descriptors
│   ├── emulator.py           # Pipelined priority-queue delay, loss, and token-bucket proxy
│   └── profiles.py           # Standardized profiles (Fiber, 4G, Satellite, etc.)
├── server/                   # Streaming server subsystem
│   ├── tcp_server.py         # TCP framed streaming server
│   ├── udp_server.py         # UDP datagram streaming server with MTU chunking
│   └── video_source.py       # Deterministic synthetic video generator & file loader
├── tests/                    # Automated Pytest suite (18 unit & integration tests)
├── streamnet.py              # Main unified CLI entrypoint
├── requirements.txt          # Python dependencies
└── README.md                 # Project documentation
```

---

## 11. Academic & Resume Highlights

- **Socket-Level Networking & Framing**: Designed and implemented application-layer binary framing (24-byte TCP, 32-byte UDP) with big-endian network byte ordering and exact-byte stream parsing.
- **Transport Layer Comparative Analysis**: Quantified Head-of-Line blocking in TCP vs. frame slice degradation in UDP across 28 controlled benchmark scenarios.
- **High-Precision Network Emulation**: Engineered a user-space network proxy utilizing priority queues, Gaussian delay variation, Bernoulli loss models, and token-bucket traffic shaping.
- **Standardized Telemetry**: Implemented IETF RFC 3550 interarrival jitter estimation, playout buffer state machine, goodput vs throughput separation, and automated publication-grade visualization.
