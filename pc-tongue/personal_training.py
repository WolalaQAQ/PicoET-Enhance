"""CPU-portable personal mask-head training; standard files are never overwritten."""
from collections import deque
import json
import math
from pathlib import Path

import numpy as np
import torch
from torch.nn import functional as F

from model import Segment, TongueModel, geometry, to_input
from personalization import calibration_from_samples


class TrainingCancelled(RuntimeError):
    pass


def check_cancel(cancelled):
    if cancelled.is_set():
        raise TrainingCancelled('Personal training was cancelled')


def largest_blob(mask):
    remaining = set(np.flatnonzero(mask).tolist())
    best = []
    while remaining:
        first = remaining.pop()
        queue, component = deque([first]), [first]
        while queue:
            index = queue.popleft()
            y, x = divmod(index, 56)
            for adjacent in (index-56 if y else -1, index+56 if y < 55 else -1,
                             index-1 if x else -1, index+1 if x < 55 else -1):
                if adjacent in remaining:
                    remaining.remove(adjacent)
                    queue.append(adjacent)
                    component.append(adjacent)
        if len(component) > len(best):
            best = component
    result = np.zeros(56*56, dtype=bool)
    result[best] = True
    return result.reshape(56, 56)


def automatic_mask(sample, open_reference):
    frame = sample['frame'].astype(np.float32)
    changed = (frame < 235) & (frame-open_reference > 20)
    integral = np.pad(changed.astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    bounds = np.arange(57)*200//56
    y0, y1 = bounds[:-1, None], bounds[1:, None]
    x0, x1 = bounds[None, :-1], bounds[None, 1:]
    hits = integral[y1, x1]-integral[y0, x1]-integral[y1, x0]+integral[y0, x0]
    cells = (y1-y0)*(x1-x0)
    return largest_blob((hits*2 > cells) | sample['mask'])


def split_samples(samples):
    train, held = [], []
    seen = {label: 0 for label in range(1, 6)}
    for sample in samples:
        label = sample['label']
        (held if seen[label] % 4 == 3 else train).append(sample)
        seen[label] += 1
    return train, held


def loss_function(logits, mask):
    probability = logits.sigmoid()
    bce = F.binary_cross_entropy_with_logits(logits, mask, pos_weight=logits.new_tensor(3.))
    dice = 1-(2*(probability*mask).sum()+1)/(probability.sum()+mask.sum()+1)
    return bce+dice


def load_network(checkpoint):
    network = Segment().cpu().eval()
    network.load_state_dict(torch.load(checkpoint, map_location='cpu', weights_only=True))
    for parameter in network.encoder.parameters():
        parameter.requires_grad_(False)
    return network


@torch.inference_mode()
def evaluate(network, samples, cancelled):
    positive = negative = true_positive = false_positive = 0
    angles = {label: [] for label in (3, 4, 5)}
    rows = []
    for sample in samples:
        check_cancel(cancelled)
        logits = network(to_input(torch.from_numpy(sample['frame'])[None]))[0, 0]
        result = geometry(sample['frame'], logits.sigmoid().numpy())
        row = dict(sample, raw_angle=result['raw_angle'], area=result['mask_area'])
        rows.append(row)
        if sample['label'] <= 2:
            negative += 1
            false_positive += result['present']
        else:
            positive += 1
            true_positive += result['present']
            if result['raw_angle'] is not None:
                angles[sample['label']].append(result['raw_angle'])
    tp, fp = true_positive/max(1, positive), false_positive/max(1, negative)
    separated = False
    if all(len(values) >= 3 for values in angles.values()):
        center, left, right = (float(np.median(angles[label])) for label in (3, 4, 5))
        separated = abs(left-center) >= .08 and abs(right-center) >= .08 and (left-center)*(right-center) < 0
    return dict(tp=tp, fp=fp, score=tp-2*fp, direction=separated,
                positive=positive, negative=negative), rows


def better_than(before, after):
    return bool(after['direction'] and after['score'] >= before['score']+.05 and after['fp'] <= before['fp']+.02)


def export_candidate(network, checkpoint, held, cancelled):
    check_cancel(cancelled)
    torch.save(network.state_dict(), checkpoint)
    with torch.inference_mode():
        torch.onnx.export(network, torch.zeros(1, 1, 224, 224), str(checkpoint.with_suffix('.onnx')),
                          input_names=['image'], output_names=['logits'], opset_version=17, dynamo=False)
    import onnx
    onnx.checker.check_model(str(checkpoint.with_suffix('.onnx')))
    check_cancel(cancelled)
    candidate = TongueModel(str(checkpoint), 'cpu')
    for sample in held:
        check_cancel(cancelled)
        with torch.inference_mode():
            expected = network(to_input(torch.from_numpy(sample['frame'])[None]))[0, 0].numpy()
        actual, logits, _ = candidate._infer200(sample['frame'])
        np.testing.assert_allclose(logits, expected, atol=.001, rtol=.0001)
        if not np.array_equal(actual['mask'], expected > 0):
            raise ValueError('Exported personal model changed the validation mask')


def train_head(samples, standard_checkpoint, current_checkpoint, job, cancelled, progress, steps=900):
    """Keep inference portable: train this small head on CPU, deploy the exported ONNX to the selected EP."""
    job = Path(job)
    torch.set_num_threads(1)
    check_cancel(cancelled)
    np.savez_compressed(job/'recording.npz', frames=np.stack([s['frame'] for s in samples]),
                        masks=np.stack([s['mask'] for s in samples]), labels=np.array([s['label'] for s in samples], np.uint8),
                        session_id=np.array([s['session_id'] for s in samples]),
                        source_seq=np.array([s['source_seq'] for s in samples]),
                        sensor_ns=np.array([s['sensor_ns'] for s in samples]))
    opened = [s['frame'].astype(np.float32) for s in samples if s['label'] == 2]
    if len(opened) < 10:
        raise ValueError('Not enough open-mouth reference samples for training')
    open_reference = np.stack(opened).mean(axis=0)
    train, held = split_samples(samples)
    usable = []
    for sample in train:
        check_cancel(cancelled)
        mask = automatic_mask(sample, open_reference) if sample['label'] >= 3 else np.zeros((56, 56), bool)
        if sample['label'] >= 3 and int(mask.sum()) < 60:
            continue
        usable.append((sample, torch.from_numpy(mask.astype(np.float32))[None, None]))
    positive = [row for row in usable if row[0]['label'] >= 3]
    negative = [row for row in usable if row[0]['label'] <= 2]
    if len(positive) < 20 or len(negative) < 20 or any(sum(s['label'] == label for s in held) < 3 for label in range(1, 6)):
        raise ValueError('Not enough usable positive, negative or held-out samples for training')
    progress(.08)
    current = load_network(current_checkpoint)
    before, _ = evaluate(current, held, cancelled)
    del current
    network = load_network(standard_checkpoint)
    optimizer = torch.optim.Adam(network.head.parameters(), lr=.001)
    rng = np.random.default_rng(12345)
    last_loss = 0.
    for step in range(steps):
        check_cancel(cancelled)
        optimizer.zero_grad()
        for pool in (positive, negative):
            sample, mask = pool[int(rng.integers(len(pool)))]
            gamma = float(np.exp(rng.uniform(-.25, .25)))
            frame = torch.from_numpy(sample['frame']).float()[None, None]/255
            x = F.interpolate(frame.pow(gamma), (224, 224), mode='bilinear', align_corners=False)
            with torch.no_grad():
                features = network.features(x)
            loss = loss_function(network.head(features), mask)
            loss.backward()
            last_loss = float(loss.detach())
        optimizer.step()
        if not math.isfinite(last_loss):
            raise ValueError('Personal training produced a non-finite loss')
        if step % 10 == 0:
            progress(.1+.75*(step+1)/steps)
    after, _ = evaluate(network, held, cancelled)
    accepted = better_than(before, after)
    metrics = dict(before=before, after=after, accepted=accepted, steps=steps, training_device='cpu',
                   train_samples=len(usable), held_out_samples=len(held), last_loss=last_loss,
                   validation_scope='every fourth frame of each recorded pose; not independent-session generalization')
    (job/'training.json').write_text(json.dumps(metrics, indent=2, allow_nan=False), encoding='utf-8')
    if not accepted:
        return dict(accepted=False, metrics=metrics)
    _, rows = evaluate(network, samples, cancelled)
    calibration = calibration_from_samples(rows)
    checkpoint = job/'personal.pt'
    progress(.9)
    export_candidate(network, checkpoint, held, cancelled)
    progress(.97)
    return dict(accepted=True, metrics=metrics, calibration=calibration, checkpoint=str(checkpoint.resolve()))
