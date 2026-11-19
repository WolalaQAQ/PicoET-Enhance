"""Build standalone PyInstaller bundle, portable ZIP, and Inno Setup installer for PC Studio."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import zipfile

from updater import APP_VERSION

STUDIO_ROOT = Path(__file__).resolve().parent
REPO_ROOT = STUDIO_ROOT.parent

STUDIO_SOURCES = (
    'backend.py',
    'runtime.py',
    'discovery.py',
    'stock.py',
    'wire.py',
    'cymple_relay.py',
    'updater.py',
    'package_release.py',
    'check.py',
    'test_stock.py',
    'test_runtime.py',
    'test_cymple_relay.py',
    'requirements.txt',
    'start.cmd',
    'installer/PicoET-PC-Studio.iss',
    'web/favicon.ico',
    'web/icon.png',
    'web/index.html',
    'web/styles.css',
    'web/app.mjs',
    'web/i18n.mjs',
    'web/checks/app.test.mjs',
)

REPO_DOCS = (
    'LICENSE',
    'THIRD_PARTY_NOTICES.md',
    'CAMERA-PROTOCOL.md',
    'README.md',
    'README_zh.md',
)

EXCLUDED_MODULES = (
    'torch',
    'torchvision',
    'torchaudio',
    'onnx',
    'onnxruntime',
    'numpy',
    'scipy',
    'matplotlib',
    'cv2',
    'pytest',
    'tkinter',
    'sqlite3',
    'capstone',
    'elftools',
    'psutil',
    'idapro',
)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest().lower()


def write_sha256(path):
    digest = sha256_file(path)
    sha_path = Path(f'{path}.sha256')
    sha_path.write_text(f'{digest}  {path.name}\n', encoding='ascii')
    return digest, sha_path


def find_iscc(explicit=None):
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get('ISCC_EXE'):
        candidates.append(Path(os.environ['ISCC_EXE']))
    for env_key in ('LOCALAPPDATA', 'ProgramFiles(x86)', 'ProgramFiles'):
        base = os.environ.get(env_key)
        if base:
            if env_key == 'LOCALAPPDATA':
                candidates.append(Path(base) / 'Programs' / 'Inno Setup 6' / 'ISCC.exe')
            else:
                candidates.append(Path(base) / 'Inno Setup 6' / 'ISCC.exe')
    which_iscc = shutil.which('ISCC') or shutil.which('iscc')
    if which_iscc:
        candidates.append(Path(which_iscc))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError('Inno Setup 6 compiler (ISCC.exe) not found')


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def smoke_test_executable(exe_path, expected_version):
    port = _free_port()
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg = Path(tmp_dir) / 'config.json'
        proc = subprocess.Popen(
            [str(exe_path), '--host', '127.0.0.1', '--port', str(port), '--config', str(cfg), '--no-browser'],
            cwd=exe_path.parent,
        )
        try:
            deadline = time.monotonic() + 10.0
            status_data = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f'Frozen executable exited early with code {proc.returncode}')
                try:
                    with urlopen(f'http://127.0.0.1:{port}/api/status', timeout=0.5) as resp:
                        status_data = json.loads(resp.read().decode('utf-8'))
                        break
                except OSError:
                    time.sleep(0.1)
            if not status_data:
                raise RuntimeError('Timed out waiting for frozen executable HTTP server')
            if status_data.get('version') != expected_version:
                raise RuntimeError(f"Version mismatch in frozen executable: {status_data.get('version')} != {expected_version}")
            with urlopen(f'http://127.0.0.1:{port}/', timeout=1.0) as resp:
                html = resp.read().decode('utf-8')
                if 'cymple-relay' not in html or 'brand-mark' not in html:
                    raise RuntimeError('Frozen executable did not serve index.html')
            for icon_route in ('/favicon.ico', '/static/icon.png'):
                with urlopen(f'http://127.0.0.1:{port}{icon_route}', timeout=1.0) as resp:
                    if resp.status != 200 or not resp.read():
                        raise RuntimeError(f'Frozen executable failed serving {icon_route}')
            stop_rc = subprocess.run(
                [str(exe_path), '--host', '127.0.0.1', '--port', str(port), '--stop'],
                cwd=exe_path.parent,
                timeout=10.0,
                check=False,
            ).returncode
            if stop_rc != 0:
                raise RuntimeError(f'--stop returned {stop_rc}')
            proc.wait(timeout=5.0)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()


def _set_pe_gui_subsystem(exe_path):
    """Switch PE OptionalHeader.Subsystem from CUI (3) to GUI (2) for windowless launch."""
    data = bytearray(Path(exe_path).read_bytes())
    pe_offset = int.from_bytes(data[0x3C:0x40], 'little')
    if data[pe_offset:pe_offset + 4] != b'PE\x00\x00':
        raise ValueError(f'Invalid PE signature in {exe_path}')
    subsystem_offset = pe_offset + 24 + 68
    data[subsystem_offset:subsystem_offset + 2] = (2).to_bytes(2, 'little')
    Path(exe_path).write_bytes(data)


def build_packages(version=APP_VERSION, *, output_dir=None, work_dir=None, iscc=None, skip_installer=False):
    out_dir = Path(output_dir or (REPO_ROOT / 'artifacts' / 'pc-studio-release')).resolve()
    wk_dir = Path(work_dir).resolve() if work_dir else Path(tempfile.mkdtemp(prefix='picoet-pc-studio-build-')).resolve()
    if work_dir and wk_dir.exists():
        shutil.rmtree(wk_dir)
    wk_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    pyi_build = wk_dir / 'pyi-build'
    pyi_dist = wk_dir / 'pyi-dist'
    pyi_spec = wk_dir / 'pyi-spec'
    bundle_dir = wk_dir / 'bundle'

    env = dict(os.environ)
    conda_bin = Path(sys.base_prefix) / 'Library' / 'bin'
    if conda_bin.is_dir():
        env['PATH'] = f"{conda_bin}{os.pathsep}{env.get('PATH', '')}"

    cmd = [
        sys.executable, '-B', '-X', 'utf8', '-m', 'PyInstaller',
        '--noconfirm', '--clean', '--onedir', '--console',
        '--name', 'PicoET-PC-Studio',
        '--icon', str(STUDIO_ROOT / 'web' / 'favicon.ico'),
        '--distpath', str(pyi_dist),
        '--workpath', str(pyi_build),
        '--specpath', str(pyi_spec),
        '--paths', str(STUDIO_ROOT),
        '--add-data', f"{STUDIO_ROOT / 'web'}{os.pathsep}web",
    ]
    for mod in EXCLUDED_MODULES:
        cmd.extend(['--exclude-module', mod])
    cmd.append(str(STUDIO_ROOT / 'backend.py'))
    subprocess.run(cmd, cwd=STUDIO_ROOT, env=env, check=True)

    built_app_dir = pyi_dist / 'PicoET-PC-Studio'
    exe_in_dist = built_app_dir / 'PicoET-PC-Studio.exe'
    if not exe_in_dist.is_file():
        raise FileNotFoundError(f'PyInstaller output missing: {exe_in_dist}')

    # Strip legacy Win7 UCRT stub DLLs if bundled from a Conda base environment.
    internal_dir = built_app_dir / '_internal'
    if internal_dir.is_dir():
        for stub in list(internal_dir.glob('api-ms-win-*.dll')) + list(internal_dir.glob('ucrtbase.dll')):
            stub.unlink(missing_ok=True)
    _set_pe_gui_subsystem(exe_in_dist)

    shutil.copytree(built_app_dir, bundle_dir)
    for rel in STUDIO_SOURCES:
        src = STUDIO_ROOT / rel
        dst = bundle_dir / 'pc-studio' / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for doc in REPO_DOCS:
        src = REPO_ROOT / doc
        if src.is_file():
            shutil.copy2(src, bundle_dir / doc)

    exe_path = bundle_dir / 'PicoET-PC-Studio.exe'
    smoke_test_executable(exe_path, version)

    portable_zip = out_dir / f'picoet-pc-studio-v{version}.zip'
    if portable_zip.exists():
        portable_zip.unlink()
    with zipfile.ZipFile(portable_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(bundle_dir.rglob('*')):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(bundle_dir).as_posix())
    portable_sha, _ = write_sha256(portable_zip)

    result = {
        'version': version,
        'portable_zip': str(portable_zip),
        'portable_sha256': portable_sha,
        'installer_exe': None,
        'installer_sha256': None,
    }

    if not skip_installer:
        iscc_exe = find_iscc(iscc)
        iss_path = STUDIO_ROOT / 'installer' / 'PicoET-PC-Studio.iss'
        subprocess.run(
            [
                str(iscc_exe),
                f'/DBundleDir={bundle_dir}',
                f'/DOutputDir={out_dir}',
                f'/DAppVersion={version}',
                str(iss_path),
            ],
            cwd=STUDIO_ROOT,
            check=True,
        )
        installer_exe = out_dir / f'PicoET-PC-Studio-Setup-v{version}.exe'
        if not installer_exe.is_file():
            raise FileNotFoundError(f'Expected installer missing: {installer_exe}')
        installer_sha, _ = write_sha256(installer_exe)
        result['installer_exe'] = str(installer_exe)
        result['installer_sha256'] = installer_sha

    shutil.rmtree(wk_dir, ignore_errors=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Package PicoET-Enhance PC Studio portable ZIP and Windows installer')
    parser.add_argument('--version', default=APP_VERSION)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--iscc', type=Path, help='Path to Inno Setup 6 ISCC.exe')
    parser.add_argument('--skip-installer', action='store_true')
    args = parser.parse_args(argv)
    info = build_packages(
        args.version,
        output_dir=args.output_dir,
        work_dir=args.work_dir,
        iscc=args.iscc,
        skip_installer=args.skip_installer,
    )
    print(json.dumps(info, indent=2))


if __name__ == '__main__':
    main()
