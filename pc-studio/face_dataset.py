"""Local mouth50 dataset contract shared by capture and offline consumers."""
from collections import defaultdict
from dataclasses import asdict, is_dataclass
import json
import math
import os
from pathlib import Path

from face_schema import CHANNELS, SCHEMA


def json_value(value):
    """Keep non-finite raw values losslessly named, never emit nonstandard JSON NaN."""
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return 'NaN' if math.isnan(value) else ('Infinity' if value > 0 else '-Infinity')
    return value


def append_json(path, row):
    with Path(path).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(json_value(row), ensure_ascii=False, allow_nan=False)+'\n')


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix+'.new')
    temporary.write_text(json.dumps(json_value(value), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def write_samples(path, samples):
    path = Path(path)
    temporary = path.with_suffix(path.suffix+'.new')
    with temporary.open('w', encoding='utf-8') as stream:
        for row in samples:
            validate_sample(row)
            stream.write(json.dumps(json_value(row), ensure_ascii=False, allow_nan=False)+'\n')
    os.replace(temporary, path)


def validate_sample(row):
    for name in ('person_id', 'session_id', 'fit_id', 'source_group_id'):
        if not isinstance(row[name], str) or not row[name]:
            raise ValueError(f'{name} must be a nonempty string')
    for name in ('capture_session_id', 'source_seq', 'mouth_sensor_ns'):
        if type(row[name]) is not int:
            raise ValueError(f'{name} must be an integer')
    image = Path(row['image'])
    if image.is_absolute() or '..' in image.parts:
        raise ValueError('image must be relative to the dataset directory')
    targets, sources = row['targets'], row['target_sources']
    if len(targets) != len(CHANNELS) or len(sources) != len(CHANNELS):
        raise ValueError('Expected 50 targets and 50 target_sources')
    for value, source in zip(targets, sources):
        if value is None:
            if source is not None:
                raise ValueError('Unknown target must have null source')
        elif not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1 or not isinstance(source, str) or not source:
            raise ValueError('Known targets must be finite [0,1] values with a source')
    if 'target_weights' in row:
        weights = row['target_weights']
        if len(weights) != len(CHANNELS) or any(not math.isfinite(w) or w < 0 for w in weights):
            raise ValueError('target_weights must have 50 finite nonnegative values')


def load_dataset(root):
    root = Path(root)
    manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest['schema'] != SCHEMA or manifest['channels'] != list(CHANNELS):
        raise ValueError('Dataset schema/channel order mismatch')
    if manifest.get('samples_complete') is not True:
        raise ValueError('Stop recording before loading its training manifest')
    samples = [json.loads(line) for line in (root/'samples.jsonl').read_text(encoding='utf-8').splitlines() if line]
    for row in samples:
        validate_sample(row)
    return manifest, samples


def split_groups(samples, *, by='session_id'):
    """Return indivisible groups, not a random frame split or automatic allocation."""
    if by not in ('person_id', 'session_id', 'fit_id', 'source_group_id'):
        raise ValueError('Split by person, recording session, fit or original source group')
    result = defaultdict(list)
    for row in samples:
        result[row[by]].append(row)
    return dict(result)


def original_frame_identity(row):
    """Physical source identity, independent of recording IDs or derived filenames."""
    header = row.get('header', {})
    capture = row.get('capture_session_id', header.get('session_id'))
    seq = row.get('source_seq', header.get('source_seq'))
    sensor = row.get('mouth_sensor_ns', header.get('mouth_sensor_ns'))
    if any(value is None for value in (capture, seq, sensor)):
        return None  # A lineage entry may identify a recording without naming a frame.
    identity = row.get('source_identity', row.get('raw_identity', header.get('raw_identity')))
    return capture, row.get('service_generation'), seq, sensor, identity


def assert_no_split_leakage(splits, *, by='session_id'):
    """Keep recordings/fits/derivatives whole, and reject repeated physical frames."""
    owners = {}
    for name, rows in splits.items():
        for row in rows:
            for origin in [row]+row.get('lineage', [])+row.get('derived_from', []):
                keys = [(field, origin[field]) for field in {by, 'session_id', 'fit_id', 'source_group_id'} if field in origin]
                frame = original_frame_identity(origin)
                if frame is not None:
                    keys.append(('original_frame', frame))
                    # Same owner/seq/time with missing or conflicting raw identity is
                    # not an independent sample either; metadata loss cannot unlock it.
                    keys.append(('original_frame_source', frame[:4]))
                for key in keys:
                    if key in owners and owners[key] != name:
                        raise ValueError(f'{key[0]} crosses splits: {key[1]}')
                    owners[key] = name
