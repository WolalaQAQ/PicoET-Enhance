"""Discover LAN cameras without subscribing; compatible with legacy mouth sources."""
import argparse
from contextlib import ExitStack
import http.client
import json
import select
import socket
import struct
import time

from stock import CaptureContext, read_capture_context

DISCOVER = b'PICO_MOUTH_DISCOVER_V1\n'
CAMERA_DISCOVER = b'PICO_CAMERA_DISCOVER_V1\n'
PORT = 45681
MULTICAST = '239.255.255.250'


def _interfaces():
    return sorted({row[4][0] for row in socket.getaddrinfo(socket.gethostname(), None,
                   socket.AF_INET, socket.SOCK_DGRAM) if not row[4][0].startswith('127.')})


def _catalog(data):
    value = json.loads(data)
    if value['service'] != 'pico-camera' or value['version'] != 1:
        raise ValueError('Unknown camera service')
    CaptureContext(int(value['session_id']), int(value['service_generation'])).pack()
    streams = value['streams']
    if [s['id'] for s in streams] != ['mouth', 'eye_left', 'eye_right']:
        raise ValueError('Unknown camera streams')
    if any(not 0 < s['port'] < 65536 or s['path'] != '/stream.mjpg' for s in streams):
        raise ValueError('Invalid camera endpoint')
    return value


def discover_sources(timeout=1.2, targets=None):
    interfaces = _interfaces() if targets is None else ['']
    targets = targets if targets is not None else [('255.255.255.255', PORT), (MULTICAST, PORT)]
    candidates = {}
    catalogs = {}
    with ExitStack() as stack:
        sockets = []
        for interface in interfaces:
            sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            sock.bind((interface, 0))
            if interface:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
            sockets.append(sock)
            for target in targets:
                sock.sendto(DISCOVER, target)
                sock.sendto(CAMERA_DISCOVER, target)
        deadline = time.monotonic() + timeout
        while sockets and time.monotonic() < deadline:
            readable, _, _ = select.select(sockets, [], [], max(.001, deadline-time.monotonic()))
            for sock in readable:
                data, endpoint = sock.recvfrom(8192)
                try:
                    if data.startswith(b'{'):
                        catalogs[endpoint] = _catalog(data)
                    else:
                        candidates[endpoint] = CaptureContext.unpack(data)
                except (ValueError, KeyError, TypeError, struct.error):
                    continue
    verified_catalogs = {}
    for (host, port), advertised in catalogs.items():
        connection = http.client.HTTPConnection(host, port, timeout=.4)
        try:
            connection.request('GET', '/cameras')
            response = connection.getresponse()
            if response.status != 200 or _catalog(response.read(8192)) != advertised:
                continue
            mouth_port = advertised['streams'][0]['port']
            key = (host, mouth_port)
            candidates[key] = CaptureContext(int(advertised['session_id']), int(advertised['service_generation']))
            verified_catalogs[key] = advertised
        except (OSError, ValueError, KeyError, TypeError, struct.error, http.client.HTTPException):
            continue
        finally:
            connection.close()
    devices = []
    for (host, port), advertised in sorted(candidates.items()):
        try:
            with socket.create_connection((host, port), timeout=.4) as sock:
                verified = read_capture_context(sock)
            if verified != advertised:
                continue
        except (OSError, ValueError, EOFError):
            continue
        device = dict(host=host, port=port, name='PICO Mouth',
                      session_id=str(verified.capture_session_id),
                      service_generation=str(verified.service_generation))
        if catalog := verified_catalogs.get((host, port)):
            device['name'] = 'PICO Cameras'
            device['streams'] = [dict(stream, url=f"http://{host}:{stream['port']}{stream['path']}")
                                 for stream in catalog['streams']]
        devices.append(device)
    return dict(devices=devices)


def discover_cameras(timeout=1.2, targets=None):
    return dict(devices=[d for d in discover_sources(timeout, targets)['devices'] if 'streams' in d])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', help='Unicast discovery target when broadcast/multicast is unavailable')
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--timeout', type=float, default=1.2)
    parser.add_argument('--cameras', action='store_true', help='Only list the three-camera service')
    args = parser.parse_args()
    discover = discover_cameras if args.cameras else discover_sources
    print(json.dumps(discover(args.timeout, [(args.host, args.port)] if args.host else None), indent=2))
