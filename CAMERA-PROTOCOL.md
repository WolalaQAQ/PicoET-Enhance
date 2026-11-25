# LAN camera service

The optional headset source exposes the mouth, wearer's left eye and wearer's right eye to independent LAN clients. Enable **LAN broadcast** in the companion **PicoET-Enhance** app to start the service on demand; sharing defaults off and remembers your choice. See [README.md](README.md#lan-broadcast-and-pc-studio) for PC Studio setup and `camera-source/device.py` for standalone developer diagnostics.

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

The bundled discovery client sends through each local IPv4 interface, then checks the HTTP catalog and the PTC1 owner before returning results. This also covers PCs whose default route points at a VPN:

```powershell
python pc-studio/discovery.py --cameras
python pc-studio/discovery.py --cameras --host <headset-ip>
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

Close the connection to unsubscribe. Eight image consumers may share each stream, mixing MJPEG and PTJ1. An additional HTTP subscription receives 503. Stream connections (`GET /stream.mjpg`, `JPEG`, `PAIR`, `LABELS`, `EYES`) are strictly **receive-only after their initial command/request**: any unexpected bytes sent by the client cause immediate disconnect. There is no application-level heartbeat on idle streams; the managed daemon uses a 1200 ms per-frame image write deadline and Linux TCP keepalive (`10s` idle, `3s`×3 probes) plus `TCP_USER_TIMEOUT` (`15s`). Each image consumer has its own writer and one pending compressed frame; slow consumers may skip frames or be disconnected after the write deadline. Other consumers continue, and their JPEG bytes come from the same single encoding of that source frame.

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

`source_seq` and `raw_identity` retain the vendor frame identity. Reopening the native camera can reset that counter to1; ordering is monotonic within each TCP subscription, while the sensor clock remains monotonic across native starts. Create a fresh ordering cursor after reconnecting, and identify recorded frames by the full `(session_id, source_seq, sensor_ns, vendor_ns, raw_identity)` tuple.

All three lanes inherit **one camera5 parent sensor timestamp**; the three named sensor fields are not separate eye exposure measurements. `vendor_ns` is retained separately. Copy/encode/emit timestamps use the headset's monotonic clock; compare them to PC time only after clock synchronization. A client joining during encoding starts with a subsequent source, not an earlier in-flight frame.

`PAIR\n` is an exclusive per-stream diagnostic mode: each source emits raw flag2 bytes and then JPEG flag1. It shares the original protected raw-slot lease through the last read and cannot coexist with JPEG/MJPEG consumers of that same stream.

Before collecting metadata, send `CONTEXT\n` on a separate control connection. Its 24-byte response is `<4sHHQQ>`: magic `PTC1`, version 1, size 24, capture session, service generation. When the daemon is not attached to a verified service instance, `CONTEXT\n` closes immediately with 0 bytes (EOF). Re-handshake after a service/session change. `PING\n` returns two uint64 monotonic nanosecond values: request received and response prepared.

The existing `PICO_MOUTH_DISCOVER_V1\n` / PTC1 discovery and mouth-only `LABELS\n` / PSL1 side channel remain available. Labels have their own subscription; they do not supply missing image identity.

PSL1 carries 33 mouth channels at four stages (raw, action-postprocessed before clamping, clamped instant, and final filtered output), two quality values, validity, classification and source identity. It does not contain eye-expression vectors; ordinary eye/face result forwarding remains the Bridge path. A JPEG or MJPEG subscription receives images, not these result records.

Each little-endian 640-byte `PSL1` record has Python layout `<4sHHII9QiI134f2I` (`camera-source/stock_record.h`):

| Offset | Type | Fields, in order |
|---:|---|---|
| 0 | 4 bytes | `PSL1` |
| 4 | 2×uint16 | `version=1`, `header_bytes=96` |
| 8 | 2×uint32 | `record_bytes=640`, `flags` (bit 0 `VALID=1`, bit 1 `ASSOCIATED=2`, bit 2 `WARMUP=4`) |
| 16 | 9×uint64 | `capture_session_id`, `service_generation`, `source_seq`, `mouth_sensor_ns`, `stock_input_ns`, `vendor_ns`, `raw_identity`, `produced_mono_ns`, `label_seq` |
| 88 | int32, uint32 | `classification`, `stages=15` |
| 96 | 33×float32 | `raw33` |
| 228 | 33×float32 | `instant33` |
| 360 | 33×float32 | `final33` |
| 492 | 2×float32 | `quality2` |
| 500 | 33×float32 | `instant_preclamp33` |
| 632 | 2×uint32 | `reserved[2]` (always `0` on wire) |

On the module-managed source, enable **Mouth** under **LAN broadcast → Result sharing** to allow `LABELS` subscriptions while broadcasting (`PSL1` records are produced by the enhancement hook, so `PicoET-Enhance` must be active in enhancement mode rather than stock `off`; if the hook attaches after camera startup, the camera payload automatically binds the stock sink on its control loop). Result sharing defaults off and remembers your choice. Turning it off rejects new label connections and closes the current one with EOF (no `END` frame) without interrupting images. One label client can subscribe at a time; a second concurrent `LABELS\n` connection closes immediately with EOF while the first is alive. Third-party clients may reconnect when it is enabled again.

PC Studio's camera preview and eye relay subscribe only to image streams and never open `LABELS` or `EYES` result connections. Python clients can read the result protocols using `read_stock` and `read_eye` in `pc-studio/stock.py`.

PSL1 and images are delivered independently. Pair only records marked associated, using the capture session, verified service generation, original frame number, sensor and vendor timestamps, and raw frame identity. Missing records or images remain unmatched. Do not pair by arrival order or nearest timestamp.

## Eye expression results (PSE1)

Enable **Both eyes** under **LAN broadcast → Result sharing** in the companion. It defaults off independently of mouth results. To receive both eyes, send `EYES\n` on a new TCP connection to the advertised `eye_left` port. Turning the switch off disconnects the result subscriber with EOF (no `END` frame) without interrupting image or mouth streams. One eye-result subscriber is supported at a time; refused or duplicate `EYES\n` connections close immediately with EOF.

Each little-endian 320-byte record has Python layout `<4sHHII6Q64f`. Its 64-byte header contains magic `PSE1`, version `1`, header size `64`, record size `320`, flags, capture session, verified service generation, result sequence, left and right inference-input timestamps in ns, and production monotonic time in ns. Flags 0/1 mark a new left/right observation; bits 2/3 mark that observation valid. The timestamp is zero for a side not updated; ignore its values.

The payload contains `raw24` (20 expression and four original quality values), `post20` (before clamping), and `final20` (after temporal filtering). Each expression vector is left then right, ten values each: brow down, inner brow up, outer brow up, blink, look down/in/out/up, squint, wide. PSE1 is not a gaze-ray or pupil-position stream. Eye timestamps describe inference inputs; they do not assert a match to individual camera images or independent exposure times. The Python reader is `pc-studio/stock.py:read_eye`.

## Source lifecycle

The adapter performs a synchronous protection copy only for subscribed lanes. Each stream has its own two-slot IPC queue, encoder and consumers. The transport daemon owns network sockets; the vendor camera callback does no JPEG encoding or network writes.

Update the main hook, adapter, shim, controller and daemon together for **PTJIPC5**. PTJ1, PTC1 and PSL1 wire layouts are unchanged. Turning the **LAN broadcast** master switch off ends all subscriptions, joins encoders/writers, releases the source's tracking lease and checks acknowledgements for every raw-slot return. Closing the companion app leaves sharing available. The standalone developer `device.py stop` performs the same shutdown for its own session; do not use its session state to control the module-managed source.
