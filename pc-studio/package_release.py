"""Build standalone Rust/Tauri PC Studio binary, portable ZIP, and Inno Setup installer."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

STUDIO_ROOT = Path(__file__).resolve().parent
REPO_ROOT = STUDIO_ROOT.parent
TAURI_DIR = STUDIO_ROOT / 'src-tauri'

DEFAULT_ORT_NUPKG_VERSION = '1.24.4'
DEFAULT_ORT_NUPKG_URL = (
    'https://api.nuget.org/v3-flatcontainer/microsoft.ml.onnxruntime.directml/'
    f'{DEFAULT_ORT_NUPKG_VERSION}/'
    f'microsoft.ml.onnxruntime.directml.{DEFAULT_ORT_NUPKG_VERSION}.nupkg'
)
DEFAULT_ORT_NUPKG_SHA256 = '57e9f11b73437bef7a309496135d4c1f96b1a8e9ddba60013fa27bfc1d788681'
DEFAULT_ORT_NUPKG_ENTRIES = (
    ('runtimes/win-x64/native/onnxruntime.dll', 'onnxruntime.dll'),
    ('runtimes/win-x64/native/onnxruntime_providers_shared.dll', 'onnxruntime_providers_shared.dll'),
    ('LICENSE', 'ONNXRUNTIME-LICENSE.txt'),
    ('ThirdPartyNotices.txt', 'ONNXRUNTIME-THIRD-PARTY-NOTICES.txt'),
)

REPO_DOCS = (
    'LICENSE',
    'THIRD_PARTY_NOTICES.md',
    'CAMERA-PROTOCOL.md',
    'README.md',
    'README_zh.md',
    'TONGUE.md',
    'TONGUE_zh.md',
)


def _read_module_prop_version(prop_path):
    if not prop_path.is_file():
        raise FileNotFoundError(f'Missing module.prop: {prop_path}')
    text = prop_path.read_text(encoding='utf-8')
    match = re.search(r'(?m)^version=(.+)$', text)
    if match and match.group(1).strip():
        return match.group(1).strip()
    raise ValueError(f'Missing version in {prop_path}')


def _read_cargo_package_version(cargo_toml_path):
    if not cargo_toml_path.is_file():
        raise FileNotFoundError(f'Missing Cargo.toml: {cargo_toml_path}')
    text = cargo_toml_path.read_text(encoding='utf-8')
    try:
        import tomllib
        data = tomllib.loads(text)
        version = data.get('package', {}).get('version', '')
        if isinstance(version, str) and version.strip():
            return version.strip()
    except ImportError:
        pass
    section = re.search(r'(?ms)^\[package\]\r?\n(.*?)(?=^\[|\Z)', text)
    if section:
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', section.group(1))
        if match and match.group(1).strip():
            return match.group(1).strip()
    raise ValueError(f'Missing [package].version in {cargo_toml_path}')


def _read_tauri_conf_version(conf_path):
    if not conf_path.is_file():
        raise FileNotFoundError(f'Missing tauri.conf.json: {conf_path}')
    data = json.loads(conf_path.read_text(encoding='utf-8'))
    version = data.get('version', '')
    if isinstance(version, str) and version.strip():
        return version.strip()
    raise ValueError(f'Missing version in {conf_path}')


def verify_version_parity(expected_version):
    versions = {
        'magisk-picoet-enhance/module.prop': _read_module_prop_version(
            REPO_ROOT / 'magisk-picoet-enhance' / 'module.prop'
        ),
        'pc-studio/src-tauri/Cargo.toml': _read_cargo_package_version(TAURI_DIR / 'Cargo.toml'),
        'pc-studio/src-tauri/tauri.conf.json': _read_tauri_conf_version(TAURI_DIR / 'tauri.conf.json'),
    }
    mismatches = {k: v for k, v in versions.items() if v != expected_version}
    if mismatches:
        detail = ', '.join(f'{k}={v!r}' for k, v in versions.items())
        raise ValueError(
            f'Version mismatch (expected {expected_version!r}): {detail}. '
            'Keep module.prop, Cargo.toml, and tauri.conf.json in sync.'
        )


def default_version():
    prop_path = REPO_ROOT / 'magisk-picoet-enhance' / 'module.prop'
    if prop_path.is_file():
        return _read_module_prop_version(prop_path)
    cargo_toml = TAURI_DIR / 'Cargo.toml'
    if cargo_toml.is_file():
        return _read_cargo_package_version(cargo_toml)
    return '2.7.0'


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


def find_cargo(explicit=None):
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get('CARGO'):
        candidates.append(Path(os.environ['CARGO']))
    which_cargo = shutil.which('cargo')
    if which_cargo:
        candidates.append(Path(which_cargo))
    exe_name = 'cargo.exe' if os.name == 'nt' else 'cargo'
    candidates.append(Path.home() / '.cargo' / 'bin' / exe_name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError('Rust cargo executable not found (install Rust or set CARGO)')


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


def _is_ort_companion_dll(name):
    lower = name.lower()
    return (
        lower == 'directml.dll'
        or (lower.startswith('onnxruntime_providers_') and lower.endswith('.dll'))
    )


def collect_ort_dlls(ort_dll=None, ort_dir=None):
    """Resolve optional ONNX Runtime DLL(s) to copy beside PicoET-PC-Studio.exe.

    Returns a deterministic list of ``(source_path, dest_filename)`` tuples.
    Returns ``[]`` when neither CLI flags nor environment variables request bundling.
    """
    if ort_dll is not None and ort_dir is not None:
        raise ValueError('Specify at most one of --ort-dll or --ort-dir')

    if ort_dir is None and ort_dll is None:
        env_dir = (os.environ.get('PICOET_ORT_DIR') or '').strip()
        env_dll = (os.environ.get('PICOET_ORT_DLL') or '').strip()
        if env_dir and env_dll:
            raise ValueError('Specify at most one of PICOET_ORT_DLL or PICOET_ORT_DIR')
        if env_dir:
            ort_dir = Path(env_dir)
        elif env_dll:
            ort_dll = Path(env_dll)

    if ort_dir is not None:
        directory = Path(ort_dir).resolve()
        if not directory.is_dir():
            raise FileNotFoundError(f'ONNX Runtime directory not found: {directory}')
        primary = None
        companions = []
        for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file():
                continue
            lower = entry.name.lower()
            if lower == 'onnxruntime.dll':
                primary = (entry, 'onnxruntime.dll')
            elif _is_ort_companion_dll(entry.name):
                companions.append((entry, entry.name))
        if primary is None:
            raise FileNotFoundError(f'Missing onnxruntime.dll in ONNX Runtime directory: {directory}')
        return [primary, *companions]

    if ort_dll is not None:
        dll_path = Path(ort_dll).resolve()
        if not dll_path.is_file():
            raise FileNotFoundError(f'ONNX Runtime DLL not found: {dll_path}')
        return [(dll_path, 'onnxruntime.dll')]

    return []


def fetch_default_ort_directml(extract_dir, cache_dir=None):
    """Download (or reuse cached), SHA-256 verify, and extract official ORT DirectML DLLs."""
    cache_root = Path(cache_dir) if cache_dir is not None else (Path(tempfile.gettempdir()) / 'picoet-ort-cache')
    cache_root.mkdir(parents=True, exist_ok=True)
    nupkg_path = cache_root / f'microsoft.ml.onnxruntime.directml.{DEFAULT_ORT_NUPKG_VERSION}.nupkg'

    if not nupkg_path.is_file() or sha256_file(nupkg_path) != DEFAULT_ORT_NUPKG_SHA256:
        tmp_path = nupkg_path.with_suffix('.nupkg.tmp')
        try:
            with urllib.request.urlopen(DEFAULT_ORT_NUPKG_URL, timeout=60) as resp, open(tmp_path, 'wb') as out:
                shutil.copyfileobj(resp, out)
            actual = sha256_file(tmp_path)
            if actual != DEFAULT_ORT_NUPKG_SHA256:
                raise RuntimeError(
                    f'ONNX Runtime DirectML {DEFAULT_ORT_NUPKG_VERSION} SHA-256 mismatch: '
                    f'expected {DEFAULT_ORT_NUPKG_SHA256}, got {actual}'
                )
            os.replace(tmp_path, nupkg_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    extract_root = Path(extract_dir)
    extract_root.mkdir(parents=True, exist_ok=True)
    extracted = []
    with zipfile.ZipFile(nupkg_path, 'r') as zf:
        for member, dest_name in DEFAULT_ORT_NUPKG_ENTRIES:
            if member not in zf.namelist():
                raise FileNotFoundError(
                    f'Missing {member!r} inside Microsoft.ML.OnnxRuntime.DirectML {DEFAULT_ORT_NUPKG_VERSION}'
                )
            dest_path = extract_root / dest_name
            with zf.open(member, 'r') as src, open(dest_path, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            extracted.append((dest_path, dest_name))
    return extracted


def smoke_test_executable(exe_path, expected_version):
    ver_proc = subprocess.run(
        [str(exe_path), '--version'],
        cwd=exe_path.parent,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )
    if ver_proc.returncode != 0:
        raise RuntimeError(
            f'--version returned {ver_proc.returncode}: '
            f'stdout={ver_proc.stdout.strip()!r} stderr={ver_proc.stderr.strip()!r}'
        )
    got_version = ver_proc.stdout.strip()
    if got_version != expected_version and got_version != f'v{expected_version}':
        raise RuntimeError(
            f'Version mismatch from --version: {got_version!r} != {expected_version!r}'
        )

    stop_proc = subprocess.run(
        [str(exe_path), '--stop'],
        cwd=exe_path.parent,
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )
    if stop_proc.returncode != 0:
        raise RuntimeError(
            f'--stop returned {stop_proc.returncode}: '
            f'stdout={stop_proc.stdout.strip()!r} stderr={stop_proc.stderr.strip()!r}'
        )


def build_packages(
    version=None,
    *,
    output_dir=None,
    work_dir=None,
    iscc=None,
    skip_installer=False,
    ort_dll=None,
    ort_dir=None,
    no_ort=False,
):
    version = version or default_version()
    verify_version_parity(version)
    if no_ort and (ort_dll is not None or ort_dir is not None):
        raise ValueError('Cannot combine --no-ort with --ort-dll or --ort-dir')
    ort_entries = [] if no_ort else collect_ort_dlls(ort_dll=ort_dll, ort_dir=ort_dir)
    ort_source = 'none' if no_ort else ('local' if ort_entries else f'default-directml-{DEFAULT_ORT_NUPKG_VERSION}')

    out_dir = Path(output_dir or (REPO_ROOT / 'artifacts' / 'pc-studio-release')).resolve()
    auto_work_dir = work_dir is None
    wk_dir = Path(tempfile.mkdtemp(prefix='picoet-pc-studio-build-')).resolve() if auto_work_dir else Path(work_dir).resolve()
    if not auto_work_dir and wk_dir.exists():
        shutil.rmtree(wk_dir)
    wk_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = wk_dir / 'bundle'
    bundle_dir.mkdir(parents=True, exist_ok=True)

    try:
        if not no_ort and not ort_entries:
            ort_entries = fetch_default_ort_directml(wk_dir / 'ort-directml')

        cargo_exe = find_cargo()
        env = dict(os.environ)
        env['PICOET_STUDIO_VERSION'] = version
        subprocess.run(
            [str(cargo_exe), 'build', '--release', '--locked', '--manifest-path', str(TAURI_DIR / 'Cargo.toml')],
            cwd=TAURI_DIR,
            env=env,
            check=True,
        )

        target_dir = (TAURI_DIR / env['CARGO_TARGET_DIR']).resolve() if env.get('CARGO_TARGET_DIR') else (TAURI_DIR / 'target')
        built_exe = target_dir / 'release' / 'PicoET-PC-Studio.exe'
        if not built_exe.is_file():
            raise FileNotFoundError(f'Cargo release binary missing: {built_exe}')

        exe_path = bundle_dir / 'PicoET-PC-Studio.exe'
        shutil.copy2(built_exe, exe_path)

        ort_files = []
        for src_dll, dest_name in ort_entries:
            shutil.copy2(src_dll, bundle_dir / dest_name)
            ort_files.append(dest_name)

        for doc in REPO_DOCS:
            src = REPO_ROOT / doc
            if not src.is_file():
                raise FileNotFoundError(f'Missing required release document: {src}')
            shutil.copy2(src, bundle_dir / doc)

        smoke_test_executable(exe_path, version)

        portable_zip = out_dir / f'picoet-pc-studio-v{version}.zip'
        if portable_zip.exists():
            portable_zip.unlink()
        with zipfile.ZipFile(portable_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(bundle_dir.rglob('*')):
                if file_path.is_file():
                    zf.write(file_path, file_path.relative_to(bundle_dir).as_posix())
            zf.writestr('user-data/.portable', '')
        portable_sha, _ = write_sha256(portable_zip)

        result = {
            'version': version,
            'portable_zip': str(portable_zip),
            'portable_sha256': portable_sha,
            'installer_exe': None,
            'installer_sha256': None,
            'ort_dll_bundled': bool(ort_files),
            'ort_source': ort_source,
            'ort_files': ort_files,
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

        return result
    finally:
        if auto_work_dir:
            shutil.rmtree(wk_dir, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Package PicoET-Enhance PC Studio portable ZIP and Windows installer')
    parser.add_argument('--version', default=default_version())
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--iscc', type=Path, help='Path to Inno Setup 6 ISCC.exe')
    parser.add_argument('--skip-installer', action='store_true')
    parser.add_argument(
        '--ort-dll',
        type=Path,
        help='Optional path to onnxruntime.dll to copy beside PicoET-PC-Studio.exe (or set PICOET_ORT_DLL)',
    )
    parser.add_argument(
        '--ort-dir',
        type=Path,
        help='Optional directory containing onnxruntime.dll and sibling DirectML.dll / onnxruntime_providers_*.dll (or set PICOET_ORT_DIR)',
    )
    parser.add_argument(
        '--no-ort',
        action='store_true',
        help='Skip bundling ONNX Runtime (default downloads and verifies official Microsoft.ML.OnnxRuntime.DirectML)',
    )
    args = parser.parse_args(argv)
    info = build_packages(
        args.version,
        output_dir=args.output_dir,
        work_dir=args.work_dir,
        iscc=args.iscc,
        skip_installer=args.skip_installer,
        ort_dll=args.ort_dll,
        ort_dir=args.ort_dir,
        no_ort=args.no_ort,
    )
    print(json.dumps(info, indent=2))


if __name__ == '__main__':
    main()
