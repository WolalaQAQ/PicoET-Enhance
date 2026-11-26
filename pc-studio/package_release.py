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
import zipfile

STUDIO_ROOT = Path(__file__).resolve().parent
REPO_ROOT = STUDIO_ROOT.parent
TAURI_DIR = STUDIO_ROOT / 'src-tauri'

REPO_DOCS = (
    'LICENSE',
    'THIRD_PARTY_NOTICES.md',
    'CAMERA-PROTOCOL.md',
    'CAMERA-PROTOCOL_zh.md',
    'README.md',
    'README_zh.md',
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


def build_packages(version=None, *, output_dir=None, work_dir=None, iscc=None, skip_installer=False):
    version = version or default_version()
    verify_version_parity(version)
    out_dir = Path(output_dir or (REPO_ROOT / 'artifacts' / 'pc-studio-release')).resolve()
    wk_dir = Path(work_dir).resolve() if work_dir else Path(tempfile.mkdtemp(prefix='picoet-pc-studio-build-')).resolve()
    if work_dir and wk_dir.exists():
        shutil.rmtree(wk_dir)
    wk_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = wk_dir / 'bundle'
    bundle_dir.mkdir(parents=True, exist_ok=True)

    try:
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
        shutil.rmtree(wk_dir, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Package PicoET-Enhance PC Studio portable ZIP and Windows installer')
    parser.add_argument('--version', default=default_version())
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
