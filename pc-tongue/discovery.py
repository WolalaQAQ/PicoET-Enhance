"""Find attached mouth sources without subscribing to images or labels."""
import socket
import time

from stock import CaptureContext, read_capture_context

DISCOVER = b'PICO_MOUTH_DISCOVER_V1\n'
PORT = 45681


def discover_sources(timeout=1.2, targets=None):
    targets = targets if targets is not None else [('255.255.255.255', PORT)]
    candidates = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(('', 0))
        for target in targets:
            sock.sendto(DISCOVER, target)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(.001, deadline-time.monotonic()))
            try:
                data, endpoint = sock.recvfrom(256)
            except socket.timeout:
                break
            try:
                candidates[endpoint] = CaptureContext.unpack(data)
            except ValueError:
                continue
    devices = []
    for (host, port), advertised in sorted(candidates.items()):
        try:
            with socket.create_connection((host, port), timeout=.4) as sock:
                verified = read_capture_context(sock)
            if verified != advertised:
                continue
        except (OSError, ValueError, EOFError):
            continue
        devices.append(dict(host=host, port=port, name='PICO Mouth',
                            session_id=str(verified.capture_session_id),
                            service_generation=str(verified.service_generation)))
    return dict(devices=devices)
