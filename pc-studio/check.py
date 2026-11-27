"""Run all offline Rust and browser-JS unit checks for PC Studio.

Pass --with-training to also run the optional offline PyTorch training tests.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
TAURI_DIR = ROOT / 'src-tauri'

TRAINING_TEST_FILES = (
    'test_face_dataset.py',
    'test_face_weak_supervision.py',
    'test_face_training.py',
    'test_mouth50_trainer.py',
)


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


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run PicoET PC Studio Rust and Web UI checks')
    parser.add_argument(
        '--with-training',
        action='store_true',
        help='Also run optional offline PyTorch training unit tests (requires torch/onnxruntime)',
    )
    args = parser.parse_args(argv)

    cargo = find_cargo()
    subprocess.run(
        [cargo, 'test', '--locked', '--manifest-path', str(TAURI_DIR / 'Cargo.toml')],
        cwd=TAURI_DIR,
        check=True,
    )
    for js in ('web/app.mjs', 'web/i18n.mjs'):
        subprocess.run(['node', '--check', str(ROOT / js)], cwd=ROOT, check=True)
    subprocess.run(['node', '--test', str(ROOT / 'web-checks/app.test.mjs')], cwd=ROOT, check=True)

    if args.with_training:
        for test_file in TRAINING_TEST_FILES:
            subprocess.run(
                [sys.executable, '-B', '-X', 'utf8', '-m', 'unittest', test_file, '-v'],
                cwd=ROOT,
                check=True,
            )

    print('PC Studio checks passed.')


if __name__ == '__main__':
    main()
