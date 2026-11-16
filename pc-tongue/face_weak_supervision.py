"""Audited stock-raw weak supervision; physical-frame identity and human priority."""
from collections import Counter, defaultdict
from bisect import bisect_left
from copy import deepcopy
import json
import math
import os
from pathlib import Path

import numpy as np

from face_dataset import original_frame_identity, validate_sample
from face_schema import CHANNELS, STOCK_CHANNELS, SCHEMA

MAPPING_ID = 'stock-raw33-identity-inrange-unknown-v1'
RAW_WEIGHT = .5
REVIEW_WEIGHT = 1.


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def excluded(row):
    return (bool(row.get('review_excluded')) or
            row.get('observation') in ('uncertain', 'occluded', 'no_mouth'))


def reviewed(source):
    return isinstance(source, str) and source.startswith(
        ('human', 'manual', 'reviewed', 'prompt_reviewed', 'prompt-reviewed'))


def frame_key(row):
    """Do not allow missing identity fields to act as an overlay wildcard."""
    key = original_frame_identity(row)
    if key is None or any(value is None for value in key):
        raise ValueError('Complete original-frame identity is required')
    if any(type(key[i]) is not int or key[i] <= 0 for i in (0, 2, 3, 4)):
        raise ValueError('Original-frame owner/sequence/sensor/raw identity must be positive integers')
    if not key[1]:
        raise ValueError('Original-frame service generation is required')
    return key


def pair_rejections(row, manifest, stock_ledger):
    """Recheck the exact pair, original PSL1 ledger, PTC1 binding, validity and quality."""
    reasons = []
    if row.get('pairing_status') not in ('paired', 'paired_late'):
        return ['not_exact_paired']
    if row.get('pairing_method') != 'verified_source':
        reasons.append('unverified_pairing_method')
    if row.get('identity_conflict') is not False:
        reasons.append('identity_conflict_or_unknown')
    try:
        key = frame_key(row)
    except ValueError:
        return reasons + ['incomplete_frame_identity']
    header, stock = row.get('header', {}), row.get('stock', {})
    if (header.get('session_id'), header.get('source_seq'), header.get('mouth_sensor_ns'),
            header.get('raw_identity')) != (key[0], key[2], key[3], key[4]):
        reasons.append('header_identity_mismatch')
    binding = manifest.get('capture_bindings', {}).get(str(key[0]), {})
    evidence = binding.get('evidence', {})
    if (binding.get('service_generation') != key[1] or evidence.get('protocol') != 'PTC1' or
            evidence.get('capture_session_id') != key[0] or evidence.get('service_generation') != key[1]):
        reasons.append('missing_verified_capture_binding')
    if any(row.get(field) != manifest.get(field) for field in
           ('person_id', 'session_id', 'fit_id', 'source_group_id')):
        reasons.append('recording_group_mismatch')
    if original_frame_identity(stock) != key:
        reasons.append('stock_identity_mismatch')
    if (stock.get('association_valid') is not True or stock.get('identity_proven') is not True or
            not stock.get('identity_evidence')):
        reasons.append('unproven_stock_identity')
    if stock.get('vendor_ns') != header.get('vendor_ns'):
        reasons.append('vendor_identity_mismatch')
    if stock.get('stock_input_ns') != key[3]:
        reasons.append('stock_input_sensor_mismatch')
    entry = stock_ledger.get(row.get('stock_id'))
    if not entry or entry.get('duplicate_of') is not None:
        reasons.append('missing_or_duplicate_original_stock')
    elif any(stock.get(field) != value for field, value in entry['record'].items()):
        reasons.append('original_stock_payload_mismatch')
    if stock.get('valid') is not True or stock.get('mouth_valid') is not True:
        reasons.append('mouth_invalid_or_unknown')
    if stock.get('warmup') is not False:
        reasons.append('warmup_or_unknown')
    quality = stock.get('quality2')
    if (not isinstance(quality, list) or len(quality) != 2 or
            not all(finite(v) and 0 <= v <= 1 for v in quality)):
        reasons.append('quality2_invalid_or_unknown')
    elif quality[0] <= .1:
        reasons.append('quality0_not_above_0.1')
    raw = stock.get('raw33')
    if not isinstance(raw, list) or len(raw) != 33 or not all(finite(v) for v in raw):
        reasons.append('raw33_invalid_or_unknown')
    return reasons


def summarize(values):
    values = np.asarray(values, dtype=float)
    if not values.size:
        return dict(count=0)
    return dict(count=int(values.size), min=float(values.min()), p01=float(np.quantile(values, .01)),
                p05=float(np.quantile(values, .05)), median=float(np.median(values)),
                p95=float(np.quantile(values, .95)), p99=float(np.quantile(values, .99)),
                max=float(values.max()), mean=float(values.mean()), std=float(values.std()),
                negative=int((values < 0).sum()), above_one=int((values > 1).sum()),
                at_least_half=int((values >= .5).sum()), at_most_tenth=int((values <= .1).sum()))


def channel_audit(rows):
    return {name: summarize([row['stock']['raw33'][i] for row in rows])
            for i, name in enumerate(STOCK_CHANNELS)}


def choose_mapping(training_rows):
    """Training-only scope: identity units, unknown outside [0,1]; never fit held-out extrema."""
    if not training_rows:
        raise ValueError('Mapping requires eligible training-session pairs')
    sessions = sorted({r['session_id'] for r in training_rows})
    stats = channel_audit(training_rows)
    return dict(mapping_id=MAPPING_ID, selected_from_sessions=sessions,
                selection_scope='Eligible declared training-session pairs only; no held-out statistics',
                policy='Identity on [0,1]; out-of-range values unknown, not clipped or rescaled',
                channels={name: dict(mapping_id=f'{MAPPING_ID}:{name}', scale=1., offset=0.,
                                     valid_interval=[0., 1.], outside='unknown',
                                     training_statistics=stats[name],
                                     reason='Preserve raw amplitude; no control-endpoint calibration')
                          for name in STOCK_CHANNELS})


def map_raw(raw33, mapping):
    if mapping['mapping_id'] != MAPPING_ID:
        raise ValueError('Unknown stock-raw mapping')
    if len(raw33) != 33:
        raise ValueError('Expected raw33')
    return [float(value) if finite(value) and 0 <= value <= 1 else None for value in raw33] + [None]*17


def review_index(rows):
    result = {}
    for row in rows:
        if not excluded(row) and not any(v is not None and reviewed(s)
                                        for v, s in zip(row['targets'], row['target_sources'])):
            continue
        key = frame_key(row)
        if key in result:
            raise ValueError('Duplicate reviewed original-frame identity')
        result[key] = row
    return result


def derive_row(source, mapping, review=None):
    row = deepcopy(source)
    key = frame_key(source)
    if review is not None and frame_key(review) != key:
        raise ValueError('Human overlay original-frame identity mismatch')
    raw_targets = map_raw(source['stock']['raw33'], mapping)
    row.update(label_stage='stock_raw', stock_raw_targets=raw_targets,
               stock_raw_mapping_id=mapping['mapping_id'],
               stock_raw_channel_mapping_ids=[mapping['channels'][name]['mapping_id']
                                              for name in STOCK_CHANNELS] + [None]*17,
               targets=list(raw_targets),
               target_sources=['stock_raw' if v is not None else None for v in raw_targets],
               target_weights=[RAW_WEIGHT if v is not None else 0. for v in raw_targets],
               supervision_priority='reviewed > stock_raw; unknown is not zero',
               stock_raw_is_ground_truth=False)
    if review is not None:
        row['review_overlay_identity'] = list(key)
        for field in ('review_excluded', 'observation', 'review_labels'):
            if field in review:
                row[field] = deepcopy(review[field])
        for i, (value, provenance) in enumerate(zip(review['targets'], review['target_sources'])):
            if value is not None and reviewed(provenance):
                row['targets'][i], row['target_sources'][i], row['target_weights'][i] = value, provenance, REVIEW_WEIGHT
    if excluded(source) or (review is not None and excluded(review)):
        row['review_excluded'] = True
        row['targets'], row['target_sources'], row['target_weights'] = [None]*50, [None]*50, [0.]*50
    row['trainable'] = any(v is not None for v in row['targets']) and not excluded(row)
    validate_weak_row(row)
    return row


def validate_weak_row(row):
    validate_sample(row)
    weights = row['target_weights']
    if len(weights) != 50:
        raise ValueError('Expected 50 target_weights')
    for value, source, weight in zip(row['targets'], row['target_sources'], weights):
        expected = 0. if value is None else REVIEW_WEIGHT if reviewed(source) else RAW_WEIGHT if source == 'stock_raw' else None
        if expected is None or type(weight) not in (int, float) or weight != expected:
            raise ValueError('Weight must be 0 unknown, 0.5 stock_raw, or 1.0 reviewed')
    if excluded(row) and any(v is not None for v in row['targets']):
        raise ValueError('Excluded reviews cannot acquire supervision')


def time_index(rows, hz=5.):
    """One source-time bin representative; retain every human anchor, no random neighbors."""
    groups = defaultdict(list)
    for row in rows:
        if row['trainable'] and not excluded(row):
            groups[row['session_id']].append(row)
    selected, reports = [], {}
    period = int(1e9/hz)
    for session, group in sorted(groups.items()):
        group.sort(key=lambda r: (r['mouth_sensor_ns'], r['source_seq']))
        first = group[0]['mouth_sensor_ns']
        buckets = defaultdict(list)
        for row in group:
            buckets[(row['mouth_sensor_ns']-first)//period].append(row)
        picks, extra_anchors = [], 0
        for _, bucket in sorted(buckets.items()):
            anchors = [r for r in bucket if any(reviewed(s) for s in r['target_sources'])]
            picks.extend(anchors or bucket[:1])
            extra_anchors += max(0, len(anchors)-1)
        picks.sort(key=lambda r: r['mouth_sensor_ns'])
        selected.extend(picks)
        seconds = (group[-1]['mouth_sensor_ns']-first)/1e9
        reports[session] = dict(full_eligible_frames=len(group), selected=len(picks),
                                span_seconds=seconds, source_time_bins=len(buckets),
                                extra_reviewed_anchors=extra_anchors, approximately_hz=(len(picks)-1)/seconds if seconds else None,
                                min_selected_gap_seconds=min(((b['mouth_sensor_ns']-a['mouth_sensor_ns'])/1e9
                                                              for a, b in zip(picks, picks[1:])), default=None))
    return selected, dict(hz=hz, bin_ns=period, sessions=reports,
                         rule='First eligible source-time frame per 200ms bin, replaced by reviewed anchors in that bin; all reviewed anchors retained',
                         independence='Source 90Hz adjacent frames and 5Hz selections are temporally correlated, not independent trials')


def spaced_time_index(rows, hz=5.):
    """Reserve all human anchors first, then greedily select genuinely spaced frames."""
    groups = defaultdict(list)
    for row in rows:
        if row['trainable'] and not excluded(row):
            groups[row['session_id']].append(row)
    period, selected, reports = int(1e9/hz), [], {}
    for session, group in sorted(groups.items()):
        group.sort(key=lambda r: (r['mouth_sensor_ns'], r['source_seq']))
        anchors = [r for r in group if any(reviewed(s) for s in r['target_sources'])]
        anchor_times = [r['mouth_sensor_ns'] for r in anchors]
        anchor_keys = {frame_key(r) for r in anchors}
        picks, last_automatic = list(anchors), None
        for row in group:
            when = row['mouth_sensor_ns']
            if frame_key(row) in anchor_keys:
                continue
            index = bisect_left(anchor_times, when)
            neighbors = anchor_times[max(0, index-1):index+1]
            if any(abs(when-anchor) < period for anchor in neighbors):
                continue
            if last_automatic is not None and when-last_automatic < period:
                continue
            picks.append(row)
            last_automatic = when
        picks.sort(key=lambda r: r['mouth_sensor_ns'])
        close_pairs = [(a, b) for a, b in zip(picks, picks[1:])
                       if b['mouth_sensor_ns']-a['mouth_sensor_ns'] < period]
        assert all(frame_key(a) in anchor_keys and frame_key(b) in anchor_keys for a, b in close_pairs)
        selected.extend(picks)
        seconds = (group[-1]['mouth_sensor_ns']-group[0]['mouth_sensor_ns'])/1e9
        reports[session] = dict(full_eligible_frames=len(group), selected=len(picks),
                                mandatory_reviewed_anchors=len(anchors), span_seconds=seconds,
                                approximately_hz=(len(picks)-1)/seconds if seconds else None,
                                mandatory_anchor_close_pairs=len(close_pairs),
                                min_selected_gap_seconds=min(((b['mouth_sensor_ns']-a['mouth_sensor_ns'])/1e9
                                                              for a, b in zip(picks, picks[1:])), default=None))
    return selected, dict(hz=hz, minimum_gap_ns=period, sessions=reports,
                         rule='Reserve every reviewed anchor, reject automatic frames within 200ms of any anchor; greedily select remaining source-time frames at least 200ms apart',
                         exception='Only mandatory reviewed anchor-anchor pairs may be closer than 200ms',
                         independence='Temporal thinning, not independent trials; recording/fit split remains the independent grouping')


def fixed_split(rows, train_session, validation_session, diagnostic_sessions=()):
    partitions = {'train': [], 'validation': [], 'test': []}
    tokens = {}
    for row in rows:
        session = row['session_id']
        partition = ('train' if session == train_session else 'validation' if session == validation_session
                     else 'test' if session in diagnostic_sessions else None)
        if partition is None:
            raise ValueError('Unexpected recording in fixed split')
        partitions[partition].append(row)
        token = 'group:'+json.dumps([row['person_id'], session], ensure_ascii=False, separators=(',', ':'))
        tokens[token] = partition
    from face_dataset import assert_no_split_leakage
    assert_no_split_leakage(partitions)
    return dict(schema=SCHEMA, channels=list(CHANNELS), group_by='session', seed=None,
                token_splits=tokens, counts={name: len(group) for name, group in partitions.items()},
                components=[dict(partition=part, tokens=[token]) for token, part in sorted(tokens.items())],
                validation_role='Declared cross-session development data; not sealed test',
                test_role='Previously inspected legacy development diagnostic only, not sealed; do not use for early stopping',
                partition_roles=dict(train='optimization', validation='development_selection', test='diagnostic'))


def hardlink_image(source, corpus, relative):
    """A new directory entry only. Never chmod/write a link: that changes the original."""
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Derived image must be a contained relative path')
    source, target = Path(source), Path(corpus)/relative
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, target)
    if not os.path.samefile(source, target):
        raise RuntimeError('Image hardlink does not reference original file')
    return dict(source=str(source), image=relative.as_posix(), size=source.stat().st_size,
                mtime_ns=source.stat().st_mtime_ns, reference='hardlink; consumer must open read-only')
