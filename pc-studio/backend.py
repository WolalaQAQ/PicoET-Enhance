"""Local HTTP workbench server for LAN camera preview and Cymple eye relay."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
import webbrowser

from cymple_relay import CympleEyeRelay
from discovery import discover_cameras, discover_sources
from runtime import DEFAULT_CONFIG, ROOT, Runtime
from updater import UpdateChecker


def _relay_config_path(runtime):
    config_path = getattr(runtime, 'config_path', None)
    return config_path.parent / 'cymple_relay.json' if isinstance(config_path, Path) else None


def _content_type(name):
    if name.endswith('.mjs') or name.endswith('.js'):
        return 'application/javascript; charset=utf-8'
    if name.endswith('.ico'):
        return 'image/x-icon'
    if name.endswith('.png'):
        return 'image/png'
    mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    return f'{mime}; charset=utf-8' if mime.startswith('text/') else mime


def _find_app_browser():
    roots = [
        os.environ.get('ProgramFiles(x86)', ''),
        os.environ.get('ProgramFiles', ''),
        os.environ.get('LOCALAPPDATA', ''),
    ]
    relatives = (
        Path('Microsoft/Edge/Application/msedge.exe'),
        Path('Google/Chrome/Application/chrome.exe'),
    )
    for base in roots:
        if not base:
            continue
        for rel in relatives:
            candidate = Path(base) / rel
            if candidate.is_file():
                return str(candidate)
    for cmd in ('msedge', 'chrome'):
        found = shutil.which(cmd)
        if found:
            return found
    return None


def open_ui(url, *, browser_tab=False):
    if not browser_tab:
        exe = _find_app_browser()
        if exe:
            try:
                subprocess.Popen([exe, f'--app={url}'], close_fds=True)
                return
            except OSError:
                pass
    webbrowser.open(url)


def is_instance_running(host, port, timeout=0.8):
    target = '127.0.0.1' if host in ('0.0.0.0', '') else host
    req = Request(f'http://{target}:{int(port)}/api/status')
    try:
        with urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode('utf-8'))
            return isinstance(data, dict) and 'version' in data and 'cymple_relay' in data
    except (URLError, OSError, ValueError):
        return False


def stop_running_instance(host, port, timeout=2.5):
    target = '127.0.0.1' if host in ('0.0.0.0', '') else host
    url = f'http://{target}:{int(port)}/api/shutdown'
    req = Request(
        url,
        data=b'{}',
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'Origin': f'http://127.0.0.1:{int(port)}',
        },
    )
    try:
        with urlopen(req, timeout=1.0) as resp:
            resp.read()
    except (URLError, OSError):
        return 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_instance_running(target, port, timeout=0.25):
            break
        time.sleep(0.1)
    return 0


def make_server(
    runtime,
    host='127.0.0.1',
    port=8787,
    *,
    discover=discover_sources,
    cymple_relay=None,
    update_checker=None,
):
    web = ROOT / 'web'
    control = threading.Lock()
    relay = cymple_relay if cymple_relay is not None else CympleEyeRelay(
        _relay_config_path(runtime), runtime=runtime, discover=discover_cameras,
    )
    checker = update_checker if update_checker is not None else UpdateChecker()

    def combined_status():
        return dict(runtime.status(), cymple_relay=relay.status())

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
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == '/api/status':
                return self.respond(200, combined_status())
            if path == '/api/config':
                return self.respond(200, dict(runtime.config))
            if path == '/api/update-check':
                force = (query.get('force', [''])[0] or '').strip().lower() in ('1', 'true', 'yes')
                return self.respond(200, checker.check(force=force))
            if path == '/api/discovery':
                try:
                    manual = (query.get('host', [''])[0] or '').strip()
                    port_str = (query.get('port', [''])[0] or '').strip()
                    target_port = int(port_str) if port_str else DEFAULT_CONFIG['headset_port']
                    if manual:
                        return self.respond(200, discover(targets=[(manual, target_port)]))
                    return self.respond(200, discover())
                except (OSError, ValueError) as error:
                    return self.respond(400, {'error': str(error)})
            if path == '/api/preview.jpg':
                with runtime.lock:
                    preview = runtime.preview
                return self.respond(200, preview, 'image/jpeg') if preview else self.respond(204)
            relative = 'index.html' if path == '/' else path.removeprefix('/static/').lstrip('/')
            file = (web / relative).resolve()
            if not file.is_relative_to(web.resolve()) or not file.is_file():
                return self.respond(404, {'error': 'Not found'})
            return self.respond(200, file.read_bytes(), _content_type(file.name))

        def do_POST(self):
            origin = self.headers.get('Origin')
            allowed = {
                f'http://127.0.0.1:{self.server.server_port}',
                f'http://localhost:{self.server.server_port}',
            }
            if origin and origin not in allowed:
                return self.respond(403, {'error': 'Local same-origin requests only'})
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.respond(415, {'error': 'Use application/json'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 0 or size > 65536:
                    raise ValueError('Configuration request too large')
                body = json.loads(self.rfile.read(size) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('JSON object required')
                path = urlsplit(self.path).path
                if path == '/api/config':
                    with control:
                        result = runtime.configure(body)
                    return self.respond(200, result)
                if path == '/api/start':
                    with control:
                        runtime.start(
                            body.get('mode', 'capture'),
                            camera=body.get('camera'),
                            headset_host=body.get('headset_host'),
                            headset_port=body.get('headset_port'),
                            eye_left_port=body.get('eye_left_port'),
                            eye_right_port=body.get('eye_right_port'),
                        )
                    return self.respond(200, combined_status())
                if path == '/api/stop':
                    with control:
                        runtime.stop()
                    return self.respond(200, combined_status())
                if path == '/api/cymple-relay':
                    with control:
                        action = body.get('action')
                        payload = {k: v for k, v in body.items() if k != 'action'}
                        if action == 'start':
                            result = relay.start(payload)
                        elif action == 'stop':
                            result = relay.stop()
                        elif action == 'configure':
                            result = relay.configure(payload)
                        else:
                            raise ValueError('Unknown Cymple relay action')
                    return self.respond(200, result)
                if path == '/api/shutdown':
                    with control:
                        relay.close()
                        runtime.close()
                    self.respond(200, {'stopping': True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                return self.respond(404, {'error': 'Not found'})
            except (ValueError, KeyError, OSError, RuntimeError) as error:
                return self.respond(400, {'error': str(error)})

        def log_message(self, format, *args):
            if self.path.startswith('/api/status') or self.path.startswith('/api/preview.jpg'):
                return
            super().log_message(format, *args)

    server = ThreadingHTTPServer((host, port), Handler)
    server.relay = relay
    server.runtime = runtime
    server.update_checker = checker
    return server


def main(argv=None):
    headless = sys.stdout is None or sys.stderr is None
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()
    parser = argparse.ArgumentParser(description='PicoET-Enhance PC Studio LAN camera forwarding and preview')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--runs', type=Path)
    parser.add_argument('--record-input', action='store_true', help='Keep complete live frames in the run directory')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--browser-tab', action='store_true', help='Open in default browser tab instead of app window')
    parser.add_argument('--stop', action='store_true', help='Stop a running instance on --host:--port and exit')
    args = parser.parse_args(argv)
    if args.stop:
        return stop_running_instance(args.host, args.port)
    if is_instance_running(args.host, args.port):
        url = f'http://{args.host}:{args.port}'
        if not args.no_browser:
            open_ui(url, browser_tab=args.browser_tab)
        return 0
    runtime = Runtime(args.config, run_root=args.runs, record_input=args.record_input)
    try:
        server = make_server(runtime, host=args.host, port=args.port)
    except OSError as error:
        message = (
            f'PicoET-Enhance PC Studio could not start on http://{args.host}:{args.port} '
            f'({error}). Another program may be using that port.'
        )
        print(message, file=sys.stderr, flush=True)
        if headless and os.name == 'nt':
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, message, 'PicoET-Enhance PC Studio', 0x10)
            except Exception:
                pass
        return 1
    url = f'http://{args.host}:{server.server_port}'
    print(f'PicoET-Enhance PC Studio: {url}', flush=True)
    if not args.no_browser:
        threading.Timer(0.3, lambda: open_ui(url, browser_tab=args.browser_tab)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.relay.close()
        runtime.close()
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
