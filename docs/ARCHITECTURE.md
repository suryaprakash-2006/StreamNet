# StreamNet Architecture Specification

**Document Version:** 1.0.0  
**Status:** Approved Specification  

---

## 1. Executive Summary

**StreamNet** is an experimental evaluation platform designed to quantitatively compare **Transmission Control Protocol (TCP)** and **User Datagram Protocol (UDP)** for real-time video streaming under controlled network degradation.

---

## 2. Emulation Boundary & Technical Limitations

### Socket-Level Impairment Layer vs. Kernel Netem
StreamNet implements its network degradation engine as a user-space, **socket-level network impairment/emulation proxy** rather than modifying OS kernel queuing disciplines (such as Linux `tc-netem` or Windows kernel filter drivers):

> [!IMPORTANT]
> **Scope & Architectural Trade-offs:**
> 1. **Cross-Platform Portability**: Operating at the socket layer allows StreamNet to execute seamlessly across Windows, Linux, and macOS without requiring administrative root privileges or proprietary OS kernel drivers.
> 2. **Emulation Fidelity**:
>    - **For UDP**: The socket proxy intercepts individual datagrams, applies precise statistical drop (Bernoulli / Markov), priority-queue delay injection, jitter modeling, and token-bucket bandwidth throttling with high fidelity.
>    - **For TCP**: Because TCP congestion control (e.g. CUBIC, BBR) operates in the kernel, a user-space proxy intercepts the TCP byte stream on intermediate sockets. It throttles byte forwarding, delays read/write events, and can induce connection resets/pauses. While this accurately simulates bandwidth bottlenecks, propagation delay, and head-of-line blocking for the application layer, it does not directly inject single-packet drops into kernel TCP ACKs (which requires raw sockets or kernel-level netem).
> 3. **Experimental Consistency**: All degradation parameters are identical across comparative runs, providing fair and reproducible measurements.

---

## 3. High-Level Component Architecture

```
                                  ┌─────────────────────────────┐
                                  │      Video Workload         │
                                  │  (Deterministic Generator)  │
                                  └──────────────┬──────────────┘
                                                 │
                                                 ▼
        ┌─────────────────────────────────────────────────────────────────────────────────┐
        │                               STREAMNET SERVER                                  │
        │  ┌───────────────────────────────────┐   ┌───────────────────────────────────┐  │
        │  │       TCP Server (Port 5001)      │   │       UDP Server (Port 5002)      │  │
        │  │  - Fixed 24-byte Header Framing   │   │  - Fixed 32-byte Header Framing   │  │
        │  │  - Byte-stream transmission       │   │  - MTU Fragmentation (1400B)      │  │
        │  │  - Sequence/Timestamp metadata    │   │  - Sequence/Timestamp metadata    │  │
        │  └─────────────────┬─────────────────┘   └─────────────────┬─────────────────┘  │
        └────────────────────┼───────────────────────────────────────┼────────────────────┘
                             │                                       │
                             ▼                                       ▼
        ┌─────────────────────────────────────────────────────────────────────────────────┐
        │                     SOCKET-LEVEL NETWORK IMPAIRMENT PROXY                       │
        │  ┌───────────────────────────────────────────────────────────────────────────┐  │
        │  │  • Latency Queue: Min-heap priority queue with high-resolution scheduling │  │
        │  │  • Jitter Engine: Gaussian/Uniform delay variation generator              │  │
        │  │  • Packet Loss: Bernoulli uniform & Gilbert-Elliott burst models          │  │
        │  │  • Bandwidth Limiter: Token Bucket rate limiter with bottleneck buffer    │  │
        │  └─────────────────┬───────────────────────────────────────┬─────────────────┘  │
        └────────────────────┼───────────────────────────────────────┼────────────────────┘
                             │                                       │
                             ▼                                       ▼
        ┌─────────────────────────────────────────────────────────────────────────────────┐
        │                               STREAMNET CLIENT                                  │
        │  ┌───────────────────────────────────┐   ┌───────────────────────────────────┐  │
        │  │           TCP Receiver            │   │           UDP Receiver            │  │
        │  │  - Exact-frame stream parsing     │   │  - Datagram arrival sequencing    │  │
        │  │  - Monotonic timestamp logging    │   │  - Chunk reassembly engine        │  │
        │  │  - Head-of-line delay tracking    │   │  - Loss & out-of-order detection  │  │
        │  └─────────────────┬─────────────────┘   └─────────────────┬─────────────────┘  │
        │                    │                                       │                    │
        │                    └───────────────────┬───────────────────┘                    │
        │                                        ▼                                        │
        │                    ┌───────────────────────────────────────┐                    │
        │                    │         Playout / Jitter Buffer       │                    │
        │                    │  - State: Buffering / Playing / Stall │                    │
        │                    └───────────────────┬───────────────────┘                    │
        │                                        ▼                                        │
        │                    ┌───────────────────────────────────────┐                    │
        │                    │       Telemetry & Metrics Engine      │                    │
        │                    │  - Throughput, Goodput, RFC3550 Jitter│                    │
        │                    └───────────────────────────────────────┘                    │
        └─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Subsystem Responsibilities

1. **Protocol & Framing Layer (`common/protocol.py`)**:
   - Encapsulates binary serialization/deserialization for TCP and UDP headers using Python `struct`.
   - Ensures binary compatibility and validation (magic bytes, versioning, message types).
2. **Server Subsystem (`server/`)**:
   - `video_source.py`: Generates deterministic animated frames (with frame index, timestamps, dynamic clock, and test motion patterns) or reads standard video files.
   - `tcp_server.py`: Listens for client connections and pushes framed video packets over TCP.
   - `udp_server.py`: Transmits fragmented datagram slices with sequence numbers over UDP.
3. **Impairment Proxy (`network/`)**:
   - Transparently binds to intermediate ports and forwards traffic to the destination while applying configured loss, latency, jitter, and bandwidth shaping.
4. **Client Subsystem (`client/`)**:
   - `tcp_client.py`: Handles TCP stream reading and frame boundary extraction.
   - `udp_client.py`: Handles UDP datagram receiving, sequence checking, and chunk reassembly.
   - `buffer.py`: Manages the playback queue and tracks rebuffering events.
5. **Metrics & Telemetry (`metrics/`)**:
   - `collector.py`: Collects packet/frame timestamps, sequence numbers, and byte sizes.
   - Computes statistical summaries (mean, p95, p99, jitter, packet loss rate, goodput).
6. **Experiment Runner (`experiments/`)**:
   - Executes matrix test sweeps, repeats trials, computes aggregates, and exports JSON/CSV data.
