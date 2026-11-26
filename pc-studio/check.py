"""Run all offline Rust and browser-JS unit checks for PC Studio."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
TAURI_DIR = ROOT / 'src-tauri'


def find_cargo():
    candidates = []
    if os.environ.get('CARGO'):
        candidates.append(Path(os.environ['CARGO']))
    which_cargo = shutil.which('cargo')
    if which_cargo:
        candidates.append(Path(which_cargo))
    exe_name = 'cargo.exe' if os.name == 'nt' else 'cargo'
    candidates.append(Path.home() / '.cargo' / 'bin' / exe_name)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError('cargo executable not found (install Rust or set CARGO)')


def main():
    cargo = find_cargo()
    subprocess.run(
        [cargo, 'test', '--manifest-path', str(TAURI_DIR / 'Cargo.toml')],
        cwd=TAURI_DIR,
        check=True,
    )
    for js in ('web/app.mjs', 'web/i18n.mjs'):
        subprocess.run(['node', '--check', str(ROOT / js)], cwd=ROOT, check=True)
    subprocess.run(['node', '--test', str(ROOT / 'web/checks/app.test.mjs')], cwd=ROOT, check=True)
    print('PC Studio checks passed.')


if __name__ == '__main__':
    main()
