"""Run the OS file picker on its own UI thread, returning only a local path."""
import json
from pathlib import Path
import subprocess
import sys


def select_model(initial=''):
    result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                             initial], capture_output=True, check=True)
    selected = json.loads(result.stdout)
    if not selected:
        return dict(cancelled=True)
    path = Path(selected).resolve()
    if path.suffix.lower() != '.onnx' or not path.is_file():
        raise ValueError('Whole-mouth inference requires an ONNX model')
    if not path.with_suffix('.json').is_file():
        raise ValueError('The adjacent JSON model manifest was not found')
    return dict(cancelled=False, path=str(path), name=path.name)


if __name__ == '__main__':
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    initial = Path(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else None
    try:
        selected = filedialog.askopenfilename(
            parent=root, title='Choose the whole-mouth model (.onnx)',
            initialdir=str(initial.parent) if initial and initial.parent.is_dir() else None,
            filetypes=[('ONNX model (*.onnx)', '*.onnx')])
        print(json.dumps(selected, ensure_ascii=True), flush=True)
    finally:
        root.destroy()
