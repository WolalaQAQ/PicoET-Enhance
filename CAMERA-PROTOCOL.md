# LAN camera service

The optional headset source exposes the mouth, wearer's left eye and wearer's right eye to independent LAN clients. The PC workbench uses the same service as other clients. Build and start the source using [TONGUE.md](TONGUE.md#2-build-and-start-the-headset-source).

| Stream ID | Camera5 lane | Default TCP port | HTTP stream |
|---|---:|---:|---|
| `mouth` | 2 | 45681 | `/stream.mjpg` |
| `eye_left` | 0 | 45682 | `/stream.mjpg` |
| `eye_right` | 1 | 45683 | `/stream.mjpg` |

All images are 400×400 grayscale JPEG, quality 95, with the original orientation. Use the advertised ports: a custom daemon base port moves all three together.

Use a trusted private LAN. Discovery and image transport are unauthenticated and unencrypted.

## Discover cameras

Send the exact ASCII datagram `PICO_CAMERA_DISCOVER_V1\n` to UDP45681 using broadcast (`255.255.255.255`), multicast (`239.255.255.250`, TTL 1), or a known headset address. The source replies by unicast to the sender's UDP endpoint.

The response is a JSON object:

```json
{
  "service": "pico-camera",
  "version": 1,
  "session_id": "123456789",
  "service_generation": "987654",
  "timestamp_semantics": "camera5_parent_sensor_ns",
  "independent_eye_exposures": false,
  "protocols": ["ptj1", "http-mjpeg"],
  "max_clients_per_stream": 8,
  "streams": [
    {"id":"mouth","source_lane":2,"port":45681,"path":"/stream.mjpg","width":400,"height":400,"pixel_format":"gray8","encoding":"jpeg","quality":95},
    {"id":"eye_left","source_lane":0,"port":45682,"path":"/stream.mjpg","width":400,"height":400,"pixel_format":"gray8","encoding":"jpeg","quality":95},
    {"id":"eye_right","source_lane":1,"port":45683,"path":"/stream.mjpg","width":400,"height":400,"pixel_format":"gray8","encoding":"jpeg","quality":95}
  ]
}
```

Combine the reply's source IP with each stream's port and path. IDs and service generations are decimal strings to preserve 64-bit values in JavaScript.

`GET /cameras` and `GET /` return the same catalog on any of the three TCP endpoints. An unattached source returns HTTP503 and stays silent on UDP discovery.

The bundled discovery client checks the HTTP catalog and the PTC1 owner before returning results:

```powershell
python pc-tongue/discovery.py --cameras
python pc-tongue/discovery.py --cameras --host <headset-ip>
```

Discovery, catalog requests, `CONTEXT` and `PING` do not subscribe or start tracking.

## Subscribe with an ordinary MJPEG client

Open an advertised URL in VLC, FFmpeg, a browser image element, or another MJPEG reader:

```text
http://<headset-ip>:45681/stream.mjpg
http://<headset-ip>:45682/stream.mjpg
http://<headset-ip>:45683/stream.mjpg
```

The response uses `multipart/x-mixed-replace; boundary=pico-camera`. Each part contains `Content-Type: image/jpeg`, `Content-Length`, `X-Session-ID`, `X-Source-Sequence` and `X-Sensor-Nanoseconds`, followed by JPEG bytes and CRLF. Metadata values are decimal integers.

For example, save one mouth frame with Python's standard library:

```python
from urllib.request import urlopen

with urlopen("http://<headset-ip>:45681/stream.mjpg", timeout=10) as stream:
    assert stream.readline().strip() == b"--pico-camera"
    headers = {}
    while (line := stream.readline()) != b"\r\n":
        key, value = line.decode("ascii").strip().split(": ", 1)
        headers[key.lower()] = value
    jpeg = stream.read(int(headers["content-length"]))
    with open("mouth.jpg", "wb") as output:
        output.write(jpeg)
```

Close the connection to unsubscribe. Eight image consumers may share each stream, mixing MJPEG and PTJ1. An additional HTTP subscription receives503. Each consumer has its own writer and one pending compressed frame; slow consumers may skip frames or be disconnected after a write deadline. Other consumers continue, and their JPEG bytes come from the same single encoding of that source frame.

On Android, the first image or label subscription acquires one native tracking lease for this service. The last subscription releases that lease. A client does not need ADB or a separate tracking application to receive frames from an already-started source. The headset's normal camera/IR availability still applies; a connected socket alone does not establish that a fresh image is available.

## PTJ1 metadata stream

PTJ1 remains available on the same TCP endpoints. On a fresh connection send `JPEG\n`; then read complete 128-byte headers followed by `payload_bytes` JPEG bytes. There is no HTTP envelope.

All integers are little-endian. Python's header format is `<4sHHII12Q4I`.

| Offset | Type | Fields, in order |
|---:|---|---|
| 0 | 4 bytes | `PTJ1` |
| 4 | 2×uint16 | `version=1`, `header_bytes=128` |
| 8 | 2×uint32 | `payload_bytes`, `flags` |
| 16 | 4×uint64 | `session_id`, `source_seq`, `sensor_ns`, `vendor_ns` |
| 48 | 4×uint64 | `copy_mono_ns`, `encode_begin_ns`, `encode_end_ns`, `emit_mono_ns` |
| 80 | 4×uint64 | `eye0_sensor_ns`, `eye1_sensor_ns`, `mouth_sensor_ns`, `raw_identity` |
| 112 | 4×uint32 | `width`, `height`, `raw_crc32`, `quality` |

JPEG frames have flag1. A normal stop sends a best-effort flag4 END header with no payload, then closes. A failed partial write closes without appending END. Flag8 marks replay input when used by a developer fixture.

`source_seq` and `raw_identity` retain the vendor frame identity. All three lanes inherit **one camera5 parent sensor timestamp**; the three named sensor fields are not separate eye exposure measurements. `vendor_ns` is retained separately. Copy/encode/emit timestamps use the headset's monotonic clock; compare them to PC time only after clock synchronization. A client joining during encoding starts with a subsequent source, not an earlier in-flight frame.

`PAIR\n` is an exclusive per-stream diagnostic mode: each source emits raw flag2 bytes and then JPEG flag1. It shares the original protected raw-slot lease through the last read and cannot coexist with JPEG/MJPEG consumers of that same stream.

Before collecting metadata, send `CONTEXT\n` on a separate control connection. Its 24-byte response is `<4sHHQQ>`: magic `PTC1`, version1, size24, capture session, service generation. Re-handshake after a service/session change. `PING\n` returns two uint64 monotonic nanosecond values: request received and response prepared.

The existing `PICO_MOUTH_DISCOVER_V1\n` / PTC1 discovery and mouth-only `LABELS\n` / PSL1 side channel remain available. Labels have their own subscription; they do not supply missing image identity.

## Source lifecycle

The adapter performs a synchronous protection copy only for subscribed lanes. Each stream has its own two-slot IPC queue, encoder and consumers. The transport daemon owns network sockets; the vendor camera callback does no JPEG encoding or network writes.

The adapter, shim and daemon must be rebuilt together for **PTJIPC4**. PTJ1, PTC1 and PSL1 wire layouts are unchanged. The developer `device.py stop` ends all three streams, joins encoders/writers, releases its tracking lease and acknowledges all raw-slot returns.
