"""Local-only whole-mouth configuration, consented recording and training API."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import threading
from urllib.parse import urlsplit, parse_qs

from runtime import DEFAULT_CONFIG, ROOT, Runtime
from discovery import discover_sources
from file_dialog import select_model
from face_presets import capture_presets


def make_guide_server(store, port=57102):
    """No Runtime or data routes: safe to forward only this display port to the headset."""
    web = ROOT/'web'

    class GuideHandler(BaseHTTPRequestHandler):
        def respond(self, code, value, content_type='application/json; charset=utf-8'):
            data = json.dumps(value, ensure_ascii=False).encode('utf-8') if isinstance(value, dict) else value
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/api/guide':
                return self.respond(200, store.status())
            if path == '/api/presets':
                return self.respond(200, dict(presets=capture_presets(), default='complete'))
            if path == '/api/status':
                return self.respond(200, dict(read_only=True, running=False, connected=False,
                    config=dict(DEFAULT_CONFIG), recording=dict(state='idle'),
                    training=dict(state='idle', running=False, operations={}),
                    external_guide=store.status()))
            if path == '/api/config':
                return self.respond(200, dict(DEFAULT_CONFIG))
            if path == '/api/discovery':
                return self.respond(200, dict(devices=[]))
            relative = {'/': 'index.html', '/static/app.mjs': 'app.mjs',
                        '/static/i18n.mjs': 'i18n.mjs', '/static/styles.css': 'styles.css'}.get(path)
            if path.startswith('/static/guide-reference/'):
                relative = path[len('/static/'):]
            file = (web/relative).resolve() if relative else None
            if not file or not file.is_relative_to(web.resolve()) or not file.is_file():
                return self.respond(404, {'error': 'Display route not found'})
            return self.respond(200, file.read_bytes(),
                                mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

        def do_POST(self):
            if urlsplit(self.path).path != '/api/display':
                return self.respond(403, {'error': 'Display receipts only; no capture controls'})
            origin = self.headers.get('Origin')
            if origin and origin not in {f'http://127.0.0.1:{self.server.server_port}',
                                          f'http://localhost:{self.server.server_port}'}:
                return self.respond(403, {'error': 'Same origin required'})
            try:
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('JSON receipt required')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024:
                    raise ValueError('Invalid receipt size')
                receipt = store.receipt(json.loads(self.rfile.read(size)))
                return self.respond(200, receipt)
            except (ValueError, KeyError) as error:
                if str(error) == 'Obsolete display receipt':
                    return self.respond(200, {'accepted': False})
                return self.respond(400, {'error': str(error)})

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(('127.0.0.1', port), GuideHandler)


def make_server(runtime, host='127.0.0.1', port=8787, *, discover=discover_sources, model_picker=select_model,
                capture=None):
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
            request = urlsplit(self.path)
            path = request.path
            query = parse_qs(request.query)
            if path == '/api/presets':
                return self.respond(200, dict(presets=capture_presets(), default='complete'))
            if path in ('/api/recording/review', '/api/recording/image'):
                try:
                    if path.endswith('/image'):
                        return self.respond(200, runtime.review_image(query.get('session', [''])[0],
                                                                     query.get('sample', [''])[0]), 'image/jpeg')
                    return self.respond(200, runtime.review_samples(int(query.get('offset', ['0'])[0]),
                                                                    int(query.get('limit', ['24'])[0]),
                                                                    query.get('all', ['false'])[0] == 'true'))
                except (ValueError, OSError) as error:
                    return self.respond(400, {'error': str(error)})
            if path == '/api/status':
                return self.respond(200, (capture or runtime).status())
            if path == '/api/discovery':
                try:
                    return self.respond(200, discover())
                except OSError as error:
                    return self.respond(400, {'error': str(error)})
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
                if (capture and capture.state in ('starting', 'recording', 'stopping') and
                        path not in ('/api/recording', '/api/recording/display', '/api/stop')):
                    raise ValueError('Finish the capture session before changing input or training')
                if path == '/api/model/select':
                    return self.respond(200, model_picker(body.get('initial', '')))
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
                if path == '/api/recording':
                    with control:
                        result = (capture or runtime).recording(body.get('action'), person_id=body.get('person_id', ''),
                                                   confirmed=body.get('confirmed', False), guide=body.get('guide', True),
                                                   stage=body.get('stage', 'raw_only'),
                                                    role=body.get('role'), evaluation_only=body.get('evaluation_only'),
                                                     preset_id=body.get('preset_id', 'complete'),
                                                     automatic=body.get('automatic', False))
                    return self.respond(200, result)
                if path == '/api/recording/display':
                    if capture and capture.display_viewer and body.get('viewer') != capture.display_viewer:
                        return self.respond(200, dict(accepted=False))
                    recorder = runtime.recorder
                    if recorder is None:
                        return self.respond(200, dict(accepted=False))
                    return self.respond(200, recorder.display(body.get('session_id'), body.get('frame'),
                        body.get('phase'), visible=body.get('visible') is True,
                        media_ready=body.get('media_ready') is True))
                if path == '/api/recording/review':
                    with control:
                        result = runtime.review(body.get('session_id'), body.get('sample_ids'), body.get('targets'),
                                                confirmed=body.get('confirmed', False), reviewer=body.get('reviewer', ''),
                                                observation=body.get('observation', 'visible'))
                    return self.respond(200, result)
                if path == '/api/training':
                    with control:
                        result = runtime.train(body.get('action'), body.get('operation'), body.get('args'))
                    return self.respond(200, result)
                if path == '/api/stop':
                    with control:
                        (capture or runtime).stop()
                    return self.respond(200, (capture or runtime).status())
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
    print(f'Mouth control: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.train('cancel')
        runtime.stop()
        runtime.udp.close()
        server.server_close()
