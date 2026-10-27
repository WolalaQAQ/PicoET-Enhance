"""Offline Python and bilingual UI regression suite; no model download or headset access."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
commands = [
    [sys.executable, '-B', '-X', 'utf8', '-m', 'unittest', 'discover', '-p', 'test_*.py', '-v'],
    ['node', '--check', 'web/app.mjs'],
    ['node', '--check', 'web/i18n.mjs'],
    ['node', '--test', 'web/checks/app.test.mjs'],
]
if __name__ == '__main__':
    for command in commands:
        subprocess.run(command, cwd=ROOT, check=True)
