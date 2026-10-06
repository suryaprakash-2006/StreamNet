# StreamNet Protocol Specification (SN-P)

**Document Version:** 1.0.0  
**Status:** Approved Specification  

---

## 1. Overview

StreamNet defines a lightweight, deterministic application-layer framing protocol designed for benchmarking real-time media streams over **TCP (Transmission Control Protocol)** and **UDP (User Datagram Protocol)**.

The protocol ensures that both transport protocols transmit the identical underlying workload with byte-level fidelity while preserving the fundamental characteristics of each transport layer:
- **TCP**: Byte-stream oriented, requiring application-level framing to delimit message boundaries. Relies entirely on kernel-level TCP for reliable, ordered delivery and congestion control.
- **UDP**: Datagram oriented, requiring fragment-level sequence tracking, timestamping, and chunk indexing for application-level loss detection and frame reassembly. Does **not** implement artificial ACKs or retransmissions.

---

## 2. Common Fields & Endianness

All multi-byte integer fields in StreamNet headers are encoded in **Network Byte Order (Big-Endian)**.

### Magic Identifier
Every valid StreamNet packet starts with the 2-byte magic sequence:
```
0x53 0x4E  (ASCII: 'S', 'N')
```

### Protocol Version
The current protocol version is `0x01`.

### Message Types (`msg_type`)
| Value | Identifier | Purpose |
| :--- | :--- | :--- |
| `0x01` | `MSG_DATA_FRAME` | Complete unfragmented data frame (Standard for TCP framing). |
| `0x02` | `MSG_DATA_CHUNK` | Fragmented slice of a data/video frame (Standard for UDP datagrams). |
| `0x03` | `MSG_STREAM_START` | Session initialization handshake containing stream metadata (width, height, fps). |
| `0x04` | `MSG_STREAM_END` | Graceful end-of-stream signaling. |
| `0x05` | `MSG_PING` | Latency calibration / round-trip time (RTT) probe. |
| `0x06` | `MSG_PONG` | Latency calibration response. |

### Header Flags (`flags`)
| Bitmask | Identifier | Description |
| :--- | :--- | :--- |
| `0x01` | `FLAG_KEYFRAME` | Indicates the payload contains an independent intra/key frame. |
| `0x02` | `FLAG_LAST_CHUNK` | Indicates this is the final fragment of a fragmented frame. |
| `0x04` | `FLAG_EOS` | End of stream flag. |

---

## 3. TCP Stream Framing Specification

Since TCP delivers an unstructured stream of bytes, message boundaries must be explicitly delineated by the application layer.

### TCP Packet Header Format (24 Bytes)

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|          MAGIC (0x534E)       |    VERSION    |   MSG_TYPE    |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|     FLAGS     |                  RESERVED (3 Bytes)           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                           FRAME_ID                            |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                      TIMESTAMP_NS (64-bit)                    |
|                                                               |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                         PAYLOAD_LEN                           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                        PAYLOAD_BYTES ...                      |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

### Struct Packing Format
Python `struct` format: `!2sBBB3sIQI` (24 bytes)

```python
# Header Format:
# magic (2s), version (B), msg_type (B), flags (B), reserved (3s), frame_id (I), timestamp_ns (Q), payload_len (I)
TCP_HEADER_SIZE = 24
```

### Receiver Algorithm
1. Read exactly `TCP_HEADER_SIZE` (24) bytes from the TCP socket buffer (`recv_exact(24)`).
2. Validate `magic == b'SN'` and `version == 1`.
3. Extract `payload_len`, `frame_id`, and `timestamp_ns`.
4. Read exactly `payload_len` bytes from the TCP socket buffer (`recv_exact(payload_len)`).
5. Pass complete payload and timing metadata to the metrics and reconstruction pipeline.

---

## 4. UDP Datagram Framing Specification

UDP datagrams preserve packet boundaries but are subject to Network Maximum Transmission Unit (MTU) limits. To avoid uncontrolled IP fragmentation at the router level, payloads larger than 1400 bytes are chunked at the application layer.

### UDP Datagram Header Format (32 Bytes)

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|          MAGIC (0x534E)       |    VERSION    |   MSG_TYPE    |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|     FLAGS     |                  RESERVED (3 Bytes)           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                           FRAME_ID                            |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                        PACKET_SEQ_NUM                         |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|         TOTAL_CHUNKS          |          CHUNK_INDEX          |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                      TIMESTAMP_NS (64-bit)                    |
|                                                               |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                         PAYLOAD_LEN                           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                        CHUNK_PAYLOAD ...                      |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

### Struct Packing Format
Python `struct` format: `!2sBBB3sIIHHQI` (32 bytes)

```python
# Header Format:
# magic (2s), version (B), msg_type (B), flags (B), reserved (3s),
# frame_id (I), packet_seq_num (I), total_chunks (H), chunk_index (H),
# timestamp_ns (Q), payload_len (I)
UDP_HEADER_SIZE = 32
MAX_UDP_PAYLOAD_SIZE = 1400  # Safe for standard 1500-byte MTU
```

### Receiver Algorithm
1. Read incoming datagram (up to `UDP_HEADER_SIZE + MAX_UDP_PAYLOAD_SIZE` = 1432 bytes).
2. Validate `magic == b'SN'`.
3. Record datagram reception timestamp $t_{\text{recv}}$ immediately.
4. Update packet sequence tracker:
   - Identify missing packets via sequence gaps: $\Delta \text{seq} = \text{seq}_{\text{curr}} - \text{seq}_{\text{last}} - 1$.
   - Identify out-of-order packets: $\text{seq}_{\text{curr}} < \text{seq}_{\text{max}}$.
5. Route chunk to reassembly map keyed by `frame_id`.
6. When all `total_chunks` for a `frame_id` are gathered, the frame is assembled and marked complete.

---

## 5. Control Messages

### Stream Start Handshake (`MSG_STREAM_START`)
Sent at session initiation. Contains JSON or fixed-binary stream descriptors:
- `fps`: Target frame rate (e.g. 30 FPS).
- `width`: Video frame width (e.g. 640).
- `height`: Video frame height (e.g. 480).
- `total_frames`: Planned frame count (e.g. 300 for a 10s run).

### Stream End Handshake (`MSG_STREAM_END`)
Sent by server upon completion to initiate graceful telemetry finalization.
