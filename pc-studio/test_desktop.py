"""Local discovery, native path selection and HTTP wiring; no headset access."""
from contextlib import ExitStack
import errno
import json
from pathlib import Path
import socket
import socketserver
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import make_server
from discovery import CAMERA_DISCOVER, DISCOVER, discover_sources
from file_dialog import select_model
from runtime import Runtime, ROOT
from stock import CaptureContext


def bind_discovery_loopback(server_type, handler):
    """Reserve UDP first: Windows TCP may allocate a port excluded for UDP."""
    for attempt in range(10):
        with ExitStack() as cleanup:
            udp = cleanup.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            udp.bind(('127.0.0.1', 0))
            try:
                tcp = server_type(udp.getsockname(), handler)
            except OSError as exc:
                retryable = exc.errno in (errno.EACCES, errno.EADDRINUSE, 10013, 10048)
                if not retryable or attempt == 9:
                    raise
                continue
            udp.settimeout(.1)
            cleanup.pop_all()
            return tcp, udp


class Source:
    def __init__(self, context, advertised=None, duplicate=False):
        self.commands = []
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                command = self.request.recv(64)
                owner.commands.append(command)
                if command == b'CONTEXT\n':
                    self.request.sendall(context)

        self.tcp, self.udp = bind_discovery_loopback(socketserver.TCPServer, Handler)
        self.endpoint = self.tcp.server_address
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.tcp.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()

        def answer():
            while not self.stopped.is_set():
                try:
                    data, peer = self.udp.recvfrom(256)
                except socket.timeout:
                    continue
                self.commands.append(data)
                if data == DISCOVER:
                    self.udp.sendto(advertised if advertised is not None else context, peer)
                    if duplicate:
                        self.udp.sendto(advertised if advertised is not None else context, peer)

        self.udp_thread = threading.Thread(target=answer)
        self.udp_thread.start()

    def close(self):
        self.stopped.set(); self.udp_thread.join(1); self.udp.close()
        self.tcp.shutdown(); self.thread.join(1); self.tcp.server_close()


class DesktopTests(unittest.TestCase):
    def test_discovery_enumerates_distinct_ipv4_interfaces_instead_of_only_vpn_default(self):
        from discovery import _interfaces
        rows = [(socket.AF_INET, socket.SOCK_DGRAM, 0, '', (ip, 0))
                for ip in ['198.18.0.1', '192.0.2.4', '192.0.2.4', '127.0.0.1']]
        with patch('discovery.socket.getaddrinfo', return_value=rows):
            self.assertEqual(_interfaces(), ['192.0.2.4', '198.18.0.1'])
    def source(self, context=None, **kwargs):
        source = Source(context if context is not None else CaptureContext(123, 456).pack(), **kwargs)
        self.addCleanup(source.close)
        return source

    def test_discovery_requires_udp_and_tcp_context_to_agree_and_deduplicates(self):
        source = self.source(duplicate=True)
        result = discover_sources(.05, [source.endpoint])
        self.assertEqual(result['devices'], [dict(name='PICO Mouth', host='127.0.0.1',
                                                  port=source.endpoint[1], session_id='123', service_generation='456')])
        self.assertEqual(source.commands.count(b'CONTEXT\n'), 1)
        self.assertTrue(all(command in (DISCOVER, CAMERA_DISCOVER, b'CONTEXT\n') for command in source.commands))

    def test_wrong_generation_and_malformed_replies_are_not_devices(self):
        for advertised in [CaptureContext(123, 999).pack(), b'PTC1', bytes(24)]:
            with self.subTest(advertised=advertised):
                source = self.source(advertised=advertised)
                self.assertEqual(discover_sources(.03, [source.endpoint])['devices'], [])

    def test_no_source_returns_an_empty_result(self):
        self.assertEqual(discover_sources(.01, [])['devices'], [])

    def test_picker_cancel_does_not_return_a_fake_path(self):
        with patch('file_dialog.subprocess.run', return_value=subprocess.CompletedProcess([], 0, b'""')) as run:
            self.assertEqual(select_model('old.onnx'), dict(cancelled=True))
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_picker_accepts_spaces_and_unicode_but_requires_the_adjacent_manifest(self):
        work = ROOT/'tests-work'; work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as folder:
            path = Path(folder)/'my \u6a21\u578b.onnx'; path.write_bytes(b'fixture')
            result = subprocess.CompletedProcess([], 0, json.dumps(str(path)).encode())
            with patch('file_dialog.subprocess.run', return_value=result):
                with self.assertRaisesRegex(ValueError, 'manifest'):
                    select_model()
                path.with_suffix('.json').write_text('{}')
                self.assertEqual(select_model()['path'], str(path.resolve()))

    def test_http_discovery_and_picker_do_not_start_or_configure_runtime(self):
        work = ROOT/'tests-work'; work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as folder:
            runtime = Runtime(Path(folder)/'config.json')
            discover = Mock(return_value={'devices': []})
            picker = Mock(return_value={'cancelled': True})
            server = make_server(runtime, port=0, discover=discover, model_picker=picker)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}); thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                with urlopen(base+'/api/discovery') as response:
                    self.assertEqual(json.load(response), {'devices': []})
                request = Request(base+'/api/model/select', b'{"initial":"old.onnx"}',
                                  {'Content-Type': 'application/json'})
                with urlopen(request) as response:
                    self.assertEqual(json.load(response), {'cancelled': True})
                picker.assert_called_once_with('old.onnx')
                self.assertFalse(runtime.running); self.assertFalse(runtime.connected)
                self.assertFalse(runtime.config_path.exists())
                request.add_header('Origin', 'http://foreign.example')
                with self.assertRaises(HTTPError) as error:
                    urlopen(request)
                self.assertEqual(error.exception.code, 403); error.exception.close()
                picker.assert_called_once()
            finally:
                server.shutdown(); thread.join(2); server.server_close(); runtime.stop(); runtime.udp.close()


if __name__ == '__main__':
    unittest.main()
