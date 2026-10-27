"""Local-only configuration UI and control API for the tongue prototype."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import threading
from urllib.parse import urlsplit

from runtime import ROOT, Runtime


def make_server(runtime, host='127.0.0.1', port=8787):
    web = ROOT/'web'
    control = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, data=b'', content_type='application/json; charset=utf-8'):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/api/status':
                return self.respond(200, runtime.status())
            if path == '/api/config':
                return self.respond(200, dict(runtime.config))
            if path == '/api/preview.jpg':
                with runtime.lock:
                    preview = runtime.preview
                return self.respond(200, preview, 'image/jpeg') if preview else self.respond(204)
            relative = 'index.html' if path == '/' else path.removeprefix('/static/').lstrip('/')
            file = (web/relative).resolve()
            if not file.is_relative_to(web.resolve()) or not file.is_file():
                return self.respond(404, {'error': 'Not found'})
            content_type = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
            if content_type.startswith('text/') or content_type == 'application/javascript':
                content_type += '; charset=utf-8'
            return self.respond(200, file.read_bytes(), content_type)

        def do_POST(self):
            origin = self.headers.get('Origin')
            allowed = {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}
            if origin and origin not in allowed:
                return self.respond(403, {'error': 'Local same-origin requests only'})
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.respond(415, {'error': 'Use application/json'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size > 65536:
                    raise ValueError('Configuration request too large')
                body = json.loads(self.rfile.read(size) or b'{}')
                path = urlsplit(self.path).path
                if path == '/api/config':
                    with control:
                        result = runtime.configure(body)
                    return self.respond(200, result)
                if path == '/api/start':
                    with control:
                        result = runtime.start(body.get('mode', 'live'), body.get('path'))
                    return self.respond(200, result)
                if path == '/api/prepare':
                    with control:
                        result = runtime.prepare()
                    return self.respond(200, result)
                if path == '/api/personalization':
                    with control:
                        result = runtime.personalize(body.get('action'), body.get('confirmed', False))
                    return self.respond(200, result)
                if path == '/api/stop':
                    with control:
                        runtime.stop()
                    return self.respond(200, runtime.status())
                return self.respond(404, {'error': 'Not found'})
            except (ValueError, OSError, RuntimeError) as error:
                return self.respond(400, {'error': str(error)})

        def log_message(self, format, *args):
            if self.path.startswith('/api/status') or self.path.startswith('/api/preview.jpg'):
                return
            super().log_message(format, *args)

    return ThreadingHTTPServer((host, port), Handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--device', default='auto')
    parser.add_argument('--record-input', action='store_true', help='Keep complete live JPEG frames in the owned run directory')
    parser.add_argument('--runs', type=Path)
    args = parser.parse_args()
    runtime = Runtime(args.config, run_root=args.runs, record_input=args.record_input)
    if args.model:
        runtime.configure({'model_path': args.model, 'device': args.device})
    server = make_server(runtime, port=args.port)
    print(f'Tongue control: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
        runtime.udp.close()
        server.server_close()
