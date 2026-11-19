"""Run all offline Python and browser-JS unit checks for PC Studio (no model deps)."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    unittest_cmd = [
        sys.executable, '-B', '-X', 'utf8',
        '-m', 'unittest', 'discover', '-s', str(ROOT), '-p', 'test_*.py', '-v',
    ]
    subprocess.run(unittest_cmd, cwd=ROOT, check=True)
    for js in ('web/app.mjs', 'web/i18n.mjs'):
        subprocess.run(['node', '--check', str(ROOT / js)], cwd=ROOT, check=True)
    subprocess.run(['node', '--test', str(ROOT / 'web/checks/app.test.mjs')], cwd=ROOT, check=True)
    print('PC Studio checks passed.')


if __name__ == '__main__':
    main()
