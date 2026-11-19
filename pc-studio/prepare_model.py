"""Acquire only the author's pinned checkpoint and export a PC FP32 ONNX model."""
import argparse
import hashlib
from pathlib import Path
import shutil
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
URL = 'https://github.com/Rulez777/PicoET-Enhance/releases/download/v2.4.2-tongue.1/tongue-training-data.zip'
CHECKPOINT_SHA256 = '0ad57869482f332caf9f62635c050a7dc8becf27af3e45fb856a01f32732b596'


def prepare(archive, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        checkpoint = package.read('shadow-final-v2.pt')
    if hashlib.sha256(checkpoint).hexdigest() != CHECKPOINT_SHA256:
        raise ValueError('The archive does not contain the pinned shadow-final-v2.pt')
    target = output/'shadow-final-v2.pt'
    if target.exists() and target.read_bytes() != checkpoint:
        raise FileExistsError('Keep the existing checkpoint; choose another --output directory')
    if not target.exists():
        target.write_bytes(checkpoint)
    onnx_path = target.with_suffix('.onnx')
    if onnx_path.exists():
        raise FileExistsError('An ONNX model already exists; choose another --output directory')
    import numpy as np
    import onnx
    import torch
    from model import Segment, TongueModel
    torch.set_num_threads(1)
    model = Segment().eval()
    model.load_state_dict(torch.load(target, map_location='cpu', weights_only=True))
    with torch.inference_mode():
        torch.onnx.export(model, torch.zeros(1, 1, 224, 224), str(onnx_path),
                          input_names=['image'], output_names=['logits'], opset_version=17, dynamo=False)
    onnx.checker.check_model(str(onnx_path))
    # Use deterministic full-range input, not only a black-frame shape check.
    frame = np.random.default_rng(7).integers(0, 256, (200, 200), dtype=np.uint8)
    expected = TongueModel(str(target), 'torch:cpu')._infer200(frame)
    actual = TongueModel(str(target), 'cpu')._infer200(frame)
    np.testing.assert_allclose(actual[1], expected[1], atol=.001, rtol=.0001)
    np.testing.assert_array_equal(actual[0]['mask'], expected[0]['mask'])
    print('Ready:', target.resolve(), onnx_path.resolve())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', type=Path, help='Existing author release ZIP; no network access when provided')
    parser.add_argument('--output', type=Path, default=ROOT/'assets')
    args = parser.parse_args()
    archive = args.archive
    if archive is None:
        args.output.mkdir(parents=True, exist_ok=True)
        archive = args.output/'author-model.zip'
        if not archive.exists():
            with urllib.request.urlopen(URL, timeout=120) as source, archive.open('xb') as target:
                shutil.copyfileobj(source, target)
    prepare(archive, args.output)


if __name__ == '__main__':
    main()
