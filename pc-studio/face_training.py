"""Offline whole-mouth regression. No downloads, capture, upload, or activation.

Independent implementation of the researched DINO/MobileNetV3 architecture.
Input is complete GRAY8 resized with Pillow BILINEAR, float B1HW /255.
Raw stock archives are deliberately not interpreted as sigmoid targets.
"""
from collections import Counter, defaultdict
from contextlib import nullcontext
from copy import deepcopy
import json
import importlib
import math
from pathlib import Path
import random
import shutil
import sys
import uuid

import numpy as np
from PIL import Image
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset

from face_schema import SCHEMA, CHANNELS as CHANNELS50
from face_dataset import load_dataset, assert_no_split_leakage, original_frame_identity


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def contract(meta):
    if meta.get('schema') != SCHEMA or meta.get('channels') != list(CHANNELS50):
        raise ValueError(f'Expected {SCHEMA} and its exact 50-channel ordering')


def is_soft(source):
    return isinstance(source, str) and source.startswith(('teacher', 'softteacher', 'pseudo'))


def is_reviewed(source):
    return isinstance(source, str) and source.startswith(
        ('human', 'manual', 'reviewed', 'prompt_reviewed', 'prompt-reviewed'))


def target_vector(values, field):
    if len(values) != len(CHANNELS50):
        raise ValueError(f'{field} must have 50 entries')
    result = np.array([np.nan if value is None else float(value) for value in values], np.float32)
    if np.isinf(result).any() or ((result[np.isfinite(result)] < 0) | (result[np.isfinite(result)] > 1)).any():
        raise ValueError(f'{field} is a [0,1] control target, not unscaled stock raw')
    return result


def evaluation_only(value):
    """Sticky restriction across rows, manifest merges and ordinary lineage copies."""
    if isinstance(value, dict):
        if value.get('evaluation_only') is True or value.get('observability_role') == 'C':
            return True
        return any(evaluation_only(value.get(key)) for key in
                   ('lineage', 'derived_from', 'source_manifests'))
    return isinstance(value, (list, tuple)) and any(evaluation_only(item) for item in value)


def require_development(rows, meta=None):
    if evaluation_only(meta) or evaluation_only(rows):
        raise ValueError('evaluation_only/C data cannot enter training, KD, pseudo labels or candidate selection')


def load_corpus(directory, *, allow_evaluation_only=False):
    root = Path(directory).resolve()
    datasets = [root] if (root/'manifest.json').exists() else [path.parent for path in sorted(root.glob('*/manifest.json'))]
    if not datasets:
        raise FileNotFoundError('Expected a stopped recording dataset or parent directory of recording datasets')
    rows = []
    manifests = []
    for dataset in datasets:
        # Shared capture contract is the authority for completion, IDs, paths and targets.
        meta, samples = load_dataset(dataset)
        restricted = evaluation_only(meta) or evaluation_only(samples)
        if restricted and not allow_evaluation_only:
            require_development(samples, meta)
        manifests.append(dict(path=str(dataset), label_stage=meta.get('label_stage'),
                              person_id=meta.get('person_id'), session_id=meta.get('session_id'),
                              observability_role=meta.get('observability_role'), evaluation_only=restricted))
        for row in samples:
            # Non-private row fields survive flattening, deepcopy and pseudo-label copies.
            row['evaluation_only'] = bool(restricted or evaluation_only(row))
            row.setdefault('observability_role', meta.get('observability_role'))
            if 'teacher_targets' in row:
                target_vector(row['teacher_targets'], 'teacher_targets')
            row['_image'] = str((dataset/row['image']).resolve())
            for origin in row.get('lineage', []) + row.get('derived_from', []):
                if not isinstance(origin, dict) or not origin.get('person_id') or not origin.get('session_id'):
                    raise ValueError('lineage/derived_from entries need person_id and session_id')
            rows.append(row)
    if not rows:
        raise ValueError('Dataset has no samples')
    if len(datasets) > 1:
        meta = dict(schema=SCHEMA, channels=list(CHANNELS50), samples_complete=True)
    meta = dict(meta, source_manifests=manifests,
                evaluation_only=evaluation_only(meta) or evaluation_only(rows) or evaluation_only(manifests))
    return meta, rows


def group_tokens(row, group_by):
    tokens = set()
    for origin in [row] + row.get('lineage', []) + row.get('derived_from', []):
        person, session = origin['person_id'], origin['session_id']
        value = person if group_by == 'person' else [person, session]
        tokens.add('group:'+json.dumps(value, ensure_ascii=False, separators=(',', ':')))
        if origin.get('source_group_id'):
            tokens.add('source:'+origin['source_group_id'])
        if origin.get('fit_id'):
            tokens.add('fit:'+origin['fit_id'])
        frame = original_frame_identity(origin)
        if frame is not None:
            # Use the shared physical identity, not an entire daemon lifetime.
            tokens.add('frame-source:'+json.dumps(frame[:4], ensure_ascii=False, separators=(',', ':')))
    # Identical files cannot be assigned conflicting identities/splits.
    if row.get('_image'):
        tokens.add('image:'+row['_image'])
    return tokens


def make_split(rows, group_by='person', seed=42, validation_fraction=.2, test_fraction=.2,
               validation_groups=(), test_groups=()):
    """Group before selecting/augmenting frames; connected derivations stay together."""
    require_development(rows)
    if group_by not in ('person', 'session'):
        raise ValueError('group_by must be person or session')
    if not 0 <= validation_fraction < 1 or not 0 <= test_fraction < 1 or validation_fraction+test_fraction >= 1:
        raise ValueError('Holdout fractions must leave a training partition')
    parents = list(range(len(rows)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    owners = {}
    all_tokens = [group_tokens(row, group_by) for row in rows]
    for index, tokens in enumerate(all_tokens):
        for token in tokens:
            if token in owners:
                parents[find(index)] = find(owners[token])
            owners[token] = index
    components = defaultdict(set)
    for index, tokens in enumerate(all_tokens):
        components[find(index)].update(tokens)
    groups = sorted(components.values(), key=lambda tokens: sorted(tokens))
    random.Random(seed).shuffle(groups)

    def explicit_tokens(values):
        if group_by == 'person':
            return {'group:'+json.dumps(value, ensure_ascii=False, separators=(',', ':')) for value in values}
        return {'group:'+json.dumps(value.split('/', 1), ensure_ascii=False, separators=(',', ':')) for value in values}

    validation, test = explicit_tokens(validation_groups), explicit_tokens(test_groups)
    explicit = bool(validation or test)
    if not (validation | test).issubset(owners):
        raise ValueError('Explicit holdout group is absent; sessions use person_id/session_id')
    labels = []
    if explicit:
        for tokens in groups:
            val, tst = bool(tokens & validation), bool(tokens & test)
            if val and tst:
                raise ValueError('A derived group connects validation and test')
            labels.append('validation' if val else 'test' if tst else 'train')
    else:
        nv = max(1, round(len(groups)*validation_fraction)) if validation_fraction else 0
        nt = max(1, round(len(groups)*test_fraction)) if test_fraction else 0
        if nv+nt >= len(groups):
            raise ValueError('Not enough independent groups; need train/validation/test groups, not adjacent frames')
        labels = ['validation']*nv + ['test']*nt + ['train']*(len(groups)-nv-nt)
    if 'train' not in labels:
        raise ValueError('Split has no training group')
    token_splits = {token: label for tokens, label in zip(groups, labels) for token in tokens}
    result = dict(schema=SCHEMA, channels=list(CHANNELS50), group_by=group_by, seed=seed,
                  token_splits=token_splits, components=[dict(partition=label, tokens=sorted(tokens))
                                                       for tokens, label in zip(groups, labels)])
    result['counts'] = dict(Counter(partition_for(row, result) for row in rows))
    check_split(rows, result)
    return result


def partition_for(row, split):
    labels = {split['token_splits'][token] for token in group_tokens(row, split['group_by'])
              if token in split['token_splits']}
    if len(labels) != 1:
        raise ValueError('Sample has unknown or conflicting split lineage; recreate/review the grouped split')
    return labels.pop()


def select_rows(rows, split, partition):
    contract(split)
    check_split(rows, split)
    return [row for row in rows if partition_for(row, split) == partition]


def check_split(rows, split):
    partitions = defaultdict(list)
    for row in rows:
        partitions[partition_for(row, split)].append(row)
    by = 'person_id' if split['group_by'] == 'person' else 'session_id'
    assert_no_split_leakage(partitions, by=by)


def prepare_frame(image, width):
    if not isinstance(image, Image.Image):
        image = Image.fromarray(image)
    pixels = np.array(image.convert('L').resize((width, width), Image.Resampling.BILINEAR), copy=True)
    return torch.from_numpy(pixels).float()[None]/255.


def read_frame(row, width):
    with Image.open(row['_image']) as image:
        return prepare_frame(image, width)


def row_excluded(row):
    return (bool(row.get('review_excluded')) or row.get('observation') in ('uncertain', 'occluded', 'no_mouth') or
            bool(row.get('identity_conflict')) or row.get('pairing_status') == 'image_conflict')


def row_targets(row):
    if row_excluded(row):
        unknown = torch.full((50,), float('nan'))
        return unknown, unknown.clone()
    targets = target_vector(row['targets'], 'targets')
    soft = target_vector(row.get('teacher_targets', [None]*50), 'teacher_targets')
    for index, source in enumerate(row['target_sources']):
        if is_soft(source):
            soft[index], targets[index] = targets[index], np.nan
    # Existing supervised targets win. KD cannot pull an approved label elsewhere.
    soft[np.isfinite(targets)] = np.nan
    return torch.from_numpy(targets), torch.from_numpy(soft)


class FaceDataset(Dataset):
    def __init__(self, rows, width, teacher_width=None, augmentation='none', *, allow_evaluation_only=False):
        if not allow_evaluation_only:
            require_development(rows)
        self.rows, self.width, self.teacher_width = rows, width, teacher_width
        self.augmentation = augmentation

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        row = self.rows[index]
        targets, soft = row_targets(row)
        result = dict(frame=read_frame(row, self.width), targets=targets, soft=soft,
                      target_weights=torch.tensor(row.get('target_weights', [1.]*50), dtype=torch.float32),
                      sample_index=torch.tensor(index), kd_eligible=torch.tensor(not row_excluded(row)))
        if self.augmentation == 'mild':
            result['frame'] = augment_frame(result['frame'])
        if self.teacher_width:
            result['teacher_frame'] = read_frame(row, self.teacher_width)
        return result


def augment_frame(frame):
    """Small full-FOV IR perturbations; never mirror a single-sided camera."""
    from torchvision.transforms.functional import affine
    def uniform(low, high):
        return float(torch.empty(()).uniform_(low, high))
    width = frame.shape[-1]
    frame = affine(frame, angle=uniform(-2, 2),
                   translate=[round(uniform(-.015, .015)*width), round(uniform(-.015, .015)*width)],
                   scale=1., shear=0.)
    frame = (frame*uniform(.95, 1.05)).clamp(0, 1).pow(uniform(.95, 1.05))
    return (frame+torch.randn_like(frame)*.005).clamp(0, 1)


class GrayNormalized(nn.Module):
    def __init__(self):
        super().__init__()
        self.register_buffer('mean', torch.tensor([.485, .456, .406]).view(1, 3, 1, 1))
        self.register_buffer('std', torch.tensor([.229, .224, .225]).view(1, 3, 1, 1))

    def normalize(self, frame):
        return (frame.repeat(1, 3, 1, 1)-self.mean)/self.std


class Student50(GrayNormalized):
    width = 160
    kind = 'mnv3-small50'

    def __init__(self, pretrained=None, width=160):
        super().__init__()
        self.width = width
        from torchvision.models import mobilenet_v3_small
        backbone = mobilenet_v3_small(weights=None)
        self.initialization = dict(backbone='torchvision.mobilenet_v3_small',
                                   checkpoint=str(Path(pretrained).resolve()) if pretrained else None,
                                   random_init=not bool(pretrained))
        if pretrained:
            path = Path(pretrained)
            if not path.is_file():
                raise FileNotFoundError(f'Local torchvision MNV3 checkpoint missing: {path}; no download attempted')
            backbone.load_state_dict(torch.load(path, map_location='cpu', weights_only=True))
        self.features = backbone.features
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.head = nn.Sequential(nn.Linear(576, 128), nn.ReLU(), nn.Dropout(.1), nn.Linear(128, 50), nn.Sigmoid())

    def forward(self, frame):
        return self.head(self.pool(self.features(self.normalize(frame))).flatten(1))

    def train(self, mode=True):
        super().train(mode)
        # Frozen BN statistics are intentional even when encoder weights train.
        for module in self.features.modules():
            if isinstance(module, nn.modules.batchnorm._BatchNorm):
                module.eval()
        return self


TEACHER_BACKBONES = {
    'dinov2': dict(hub_model='dinov2_vitb14_reg', embed_dim=768, width=392, kind='dinov2-vitb14-reg50'),
    'dinov3': dict(hub_model='dinov3_vitb16', embed_dim=768, width=400, kind='dinov3-vitb16-50'),
    'dinov3-large': dict(hub_model='dinov3_vitl16', embed_dim=1024, width=400, kind='dinov3-vitl16-50'),
    'dinov3-huge': dict(hub_model='dinov3_vith16plus', embed_dim=1280, width=400, kind='dinov3-vith16plus-50'),
}


class Teacher50(GrayNormalized):
    width = 392
    kind = 'dinov2-vitb14-reg50'
    kind_v3 = 'dinov3-vitb16-50'

    def __init__(self, backbone, embed_dim=None, tune='frozen', family='dinov2'):
        super().__init__()
        spec = TEACHER_BACKBONES[family]
        embed_dim = spec['embed_dim'] if embed_dim is None else embed_dim
        self.family = family
        self.kind = spec['kind']
        self.width = spec['width']
        self.backbone = backbone
        self.initialization = getattr(backbone, 'source_metadata', dict(injected_backbone=type(backbone).__name__,
                                                                       embed_dim=embed_dim))
        self.head = nn.Sequential(nn.LayerNorm(embed_dim*2), nn.Linear(embed_dim*2, 256), nn.GELU(),
                                  nn.Dropout(.1), nn.Linear(256, 50), nn.Sigmoid())
        self.set_tune(tune)

    def set_tune(self, tune):
        if tune not in ('frozen', 'last-two'):
            raise ValueError('Teacher tune must be frozen or last-two')
        self.tune = tune
        self.backbone.requires_grad_(False)
        if tune == 'last-two':
            for block in self.backbone.blocks[-2:]:
                block.requires_grad_(True)
        self.train(self.training)

    def train(self, mode=True):
        super().train(mode)
        self.backbone.eval()
        if getattr(self, 'tune', 'frozen') == 'last-two':
            for block in self.backbone.blocks[-2:]:
                block.train(mode)
        return self

    def forward(self, frame):
        context = torch.no_grad() if self.tune == 'frozen' else nullcontext()
        with context:
            features = self.backbone.forward_features(self.normalize(frame))
            pooled = torch.cat((features['x_norm_clstoken'], features['x_norm_patchtokens'].mean(1)), dim=1)
        return self.head(pooled)


def local_dino(repo, checkpoint=None, family='dinov2'):
    if not repo or not (Path(repo)/'hubconf.py').is_file():
        raise FileNotFoundError('DINO requires explicit local --dino-repo containing hubconf.py; no download attempted')
    if checkpoint is not None and not Path(checkpoint).is_file():
        raise FileNotFoundError(f'Local DINO backbone checkpoint missing: {checkpoint}; no download attempted')
    hub_model = TEACHER_BACKBONES[family]['hub_model']
    if family.startswith('dinov3'):
        # Upstream hubconf imports unrelated segmentation/evaluation dependencies.
        # The backbone module only needs the existing torch/numpy environment.
        sys.path.insert(0, str(Path(repo).resolve()))
        try:
            backbone = getattr(importlib.import_module('dinov3.hub.backbones'), hub_model)(pretrained=False)
        finally:
            sys.path.pop(0)
    else:
        backbone = torch.hub.load(str(Path(repo).resolve()), hub_model, source='local', pretrained=False)
    if checkpoint:
        backbone.load_state_dict(torch.load(checkpoint, map_location='cpu', weights_only=True))
    backbone.source_metadata = dict(backbone=hub_model, family=family, repo=str(Path(repo).resolve()),
                                    checkpoint=str(Path(checkpoint).resolve()) if checkpoint else 'full training checkpoint')
    return backbone


def masked_mse(prediction, targets):
    """Mean per observed channel, then across channels; never compute NaN * 0."""
    mask = torch.isfinite(targets)
    safe = torch.where(mask, targets, torch.zeros_like(targets))
    error = torch.where(mask, prediction-safe, torch.zeros_like(prediction)).square()
    counts = mask.sum(0)
    if not bool(counts.any()):
        return prediction.sum()*0
    return (error.sum(0)[counts > 0]/counts[counts > 0]).mean()


def masked_objective(prediction, targets, weights=None, kind='mse'):
    """Source weights scale each observed contribution before channel balancing."""
    mask = torch.isfinite(targets)
    safe = torch.where(mask, targets, torch.zeros_like(targets))
    if kind == 'mse':
        error = (prediction-safe).square()
    elif kind == 'huber':
        error = nn.functional.smooth_l1_loss(prediction, safe, reduction='none', beta=.1)
    else:
        raise ValueError('loss_kind must be mse or huber')
    error = torch.where(mask, error, torch.zeros_like(error))
    if weights is not None:
        if not torch.isfinite(weights).all() or bool((weights < 0).any()):
            raise ValueError('target_weights must be finite and nonnegative')
        error = error*weights
        mask = mask & (weights > 0)
    counts = mask.sum(0)
    if not bool(counts.any()):
        return prediction.sum()*0
    return (error.sum(0)[counts > 0]/counts[counts > 0]).mean()


def freeze_teacher(teacher):
    teacher.eval()
    teacher.requires_grad_(False)
    return teacher


def tongue_bin_balance(rows):
    """Training-only Out response bins; never create a label for an unknown response."""
    values = []
    for row in rows:
        target = row_targets(row)[0][32]
        if torch.isfinite(target):
            values.append(float(target))
    counts = np.bincount(np.searchsorted([.1, .3, .5], values, side='right'), minlength=4)
    factors = np.divide(len(values), np.count_nonzero(counts)*counts,
                        out=np.zeros(4), where=counts > 0)
    factors = np.minimum(factors, 16.)
    mean = float(np.dot(factors, counts)/len(values)) if values else 0.
    if mean:
        factors /= mean
    return dict(channel='TongueOut', boundaries=[.1, .3, .5], counts=counts.tolist(),
                factors=factors.tolist(), pre_normalization_cap=16.,
                scope='Training targets only; response bins are not reviewed visibility grades',
                unknown_policy='No label or weight created; original source reliability weights retained')


def training_loss(student, batch, teacher=None, approved=None, kd_weight=.5, loss_kind='mse',
                  tongue_balance=None):
    prediction = student(batch['frame'])
    targets, soft = batch['targets'], batch['soft'].clone()
    eligible = batch.get('kd_eligible', torch.ones(prediction.shape[0], dtype=torch.bool, device=prediction.device))
    targets = torch.where(eligible[:, None], targets, torch.full_like(targets, float('nan')))
    if 'target_weights' in batch:
        targets = torch.where(batch['target_weights'] > 0, targets, torch.full_like(targets, float('nan')))
    allowed = torch.tensor([name in (approved or ()) for name in CHANNELS50], device=prediction.device)[None] & eligible[:, None]
    soft = torch.where(allowed, soft, torch.full_like(soft, float('nan')))
    if teacher is not None:
        freeze_teacher(teacher)
        with torch.no_grad():
            inferred = teacher(batch['teacher_frame'])
        inferred = torch.where(allowed, inferred, torch.full_like(inferred, float('nan')))
        # Explicit saved pseudo-labels take precedence over fresh model predictions.
        soft = torch.where(torch.isfinite(soft), soft, inferred)
    soft = torch.where(torch.isfinite(targets), torch.full_like(soft, float('nan')), soft)
    weights = batch.get('target_weights')
    if tongue_balance:
        weights = torch.ones_like(targets) if weights is None else weights.clone()
        bins = (targets[:, 32, None] >= torch.tensor(tongue_balance['boundaries'],
                                                    device=targets.device)).sum(1)
        factors = torch.tensor(tongue_balance['factors'], device=targets.device)
        weights[:, 32] *= factors[bins]
    supervision = masked_objective(prediction, targets, weights, loss_kind)
    kd = masked_objective(prediction, soft, kind=loss_kind)
    return supervision+kd_weight*kd, dict(supervision=float(supervision.detach()), kd=float(kd.detach())), targets, soft


def checkpoint_meta(model, trained_channels, provenance, **extra):
    return dict(schema=SCHEMA, channels=list(CHANNELS50), model_kind=model.kind,
                width=model.width, height=model.width, model_id=str(uuid.uuid4()),
                trained_channels=[name for name in CHANNELS50 if name in trained_channels],
                training=provenance, initialization=getattr(model, 'initialization', {}), **extra)


def save_checkpoint(path, model, meta):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(dict(format='pico-face-training-v1', metadata=meta, state_dict=model.state_dict()), path)


def load_checkpoint(path, dino_repo=None, device='cpu'):
    saved = torch.load(path, map_location='cpu', weights_only=True)
    if saved.get('format') != 'pico-face-training-v1':
        raise ValueError('Expected a whole-mouth training checkpoint, not an old 44-channel/segmentation model')
    meta = saved['metadata']
    contract(meta)
    if meta['model_kind'] == Student50.kind:
        model = Student50(width=meta['width'])
    elif meta['model_kind'] in {spec['kind'] for spec in TEACHER_BACKBONES.values()}:
        family = next(name for name, spec in TEACHER_BACKBONES.items() if spec['kind'] == meta['model_kind'])
        model = Teacher50(local_dino(dino_repo, family=family), tune=meta.get('tune', 'frozen'), family=family)
    else:
        raise ValueError(f'Unknown model kind {meta["model_kind"]}')
    model.load_state_dict(saved['state_dict'])
    model.initialization = meta.get('initialization', {})
    return model.to(device).eval(), meta


def training_tokens(rows, group_by):
    return sorted(set().union(*(group_tokens(row, group_by) for row in rows))) if rows else []


def assert_holdout(meta, rows):
    for provenance in meta.get('training', {}).get('runs', []):
        known = set(provenance['train_tokens'])
        for row in rows:
            if group_tokens(row, provenance['group_by']) & known:
                raise ValueError('Evaluation overlaps model training or its derived lineage; not an independent holdout')


def assert_train_pool(meta, rows):
    """Do not turn any sealed ancestor validation/test identity into training data."""
    require_development(rows)
    for provenance in meta.get('training', {}).get('runs', []):
        split = provenance['split']
        reserved = {token for token, label in split['token_splits'].items() if label != 'train'}
        for row in rows:
            if group_tokens(row, provenance['group_by']) & reserved:
                raise ValueError('Training/pseudo pool contains an ancestor validation/test identity or derived source')


def load_approval(path, teacher_meta):
    report = read_json(path)
    if (report.get('candidate_model_id') != teacher_meta['model_id'] or
            report.get('partition') != 'validation' or not report.get('accepted')):
        raise ValueError('Teacher needs an accepted validation report for this exact model_id')
    allowed = set(report['approved_channels']) & set(supported_channels(teacher_meta))
    if not allowed:
        raise ValueError('Teacher has no independently validated channels')
    return allowed


def supported_channels(meta):
    """New non-stock heads need reviewed action-positive evidence, not just a loss term."""
    positive = set(meta.get('positive_supervision_channels', []))
    return [name for index, name in enumerate(CHANNELS50) if name in meta['trained_channels'] and
            (index < 33 or name in positive)]


def label_coverage(rows, seen_supervised, seen_kd, seen_kd_positive):
    coverage = {}
    for index, name in enumerate(CHANNELS50):
        counts = dict(supervised_count=0, positive_count=0, reviewed_count=0, reviewed_positive_count=0,
                      kd_count=int(seen_kd[:, index].sum()), kd_positive_count=int(seen_kd_positive[:, index].sum()),
                      positive_threshold=.5, by_source={})
        for sample_index, row in enumerate(rows):
            if not seen_supervised[sample_index, index]:
                continue
            value, source = row['targets'][index], row['target_sources'][index] or 'unspecified'
            positive = float(value) >= .5
            counts['supervised_count'] += 1
            counts['positive_count'] += int(positive)
            counts['reviewed_count'] += int(is_reviewed(source))
            counts['reviewed_positive_count'] += int(positive and is_reviewed(source))
            per_source = counts['by_source'].setdefault(source, dict(count=0, positive_count=0))
            per_source['count'] += 1
            per_source['positive_count'] += int(positive)
        coverage[name] = counts
    return coverage


def train_model(model, rows, split, output, *, dataset_source, initial_meta=None, teacher=None,
                teacher_meta=None, approved=(), epochs=10, batch_size=8, learning_rate=1e-3,
                encoder_lr=1e-5, kd_weight=.5, accumulation=1, patience=3, max_steps=None,
                device='cpu', seed=42, progress=None, cancelled=None, mode='baseline',
                loss_kind='mse', augmentation='none', selection_channels=None, balance_tongue_bins=False):
    """Synchronous API. progress receives JSON-safe epoch reports; cancellation is callable."""
    require_development(rows)
    if Path(output).exists():
        raise FileExistsError('Candidate checkpoint exists; choose a new output')
    # The programmatic API obeys the same stopped-recording gate as the CLI.
    load_corpus(dataset_source)
    if epochs < 1 or batch_size < 1 or accumulation < 1 or kd_weight < 0:
        raise ValueError('epochs/batch_size/accumulation must be positive; kd_weight nonnegative')
    if augmentation not in ('none', 'mild'):
        raise ValueError('augmentation must be none or mild')
    torch.manual_seed(seed)
    train_rows, validation = select_rows(rows, split, 'train'), select_rows(rows, split, 'validation')
    excluded_train_count = sum(row_excluded(row) for row in train_rows)
    train_rows = [row for row in train_rows if not row_excluded(row)]
    tongue_balance = tongue_bin_balance(train_rows) if balance_tongue_bins else None
    if not train_rows or not validation:
        raise ValueError('Training requires distinct nonempty train and validation partitions')
    if initial_meta:
        assert_holdout(initial_meta, validation)
        assert_train_pool(initial_meta, train_rows)
    if teacher_meta:
        assert_holdout(teacher_meta, validation)
        assert_train_pool(teacher_meta, train_rows)
    model.to(device)
    if teacher is not None:
        freeze_teacher(teacher.to(device))
    encoder = model.backbone if isinstance(model, Teacher50) else model.features
    groups = [dict(params=[p for p in encoder.parameters() if p.requires_grad], lr=encoder_lr),
              dict(params=[p for p in model.head.parameters() if p.requires_grad], lr=learning_rate)]
    optimizer = torch.optim.AdamW(groups, weight_decay=1e-4)
    generator = torch.Generator().manual_seed(seed)
    loader = DataLoader(FaceDataset(train_rows, model.width, teacher.width if teacher else None, augmentation),
                        batch_size=batch_size, shuffle=True, generator=generator, num_workers=0)
    supervised_counts, kd_counts = torch.zeros(50, dtype=torch.long), torch.zeros(50, dtype=torch.long)
    seen_supervised = torch.zeros(len(train_rows), 50, dtype=torch.bool)
    seen_kd, seen_kd_positive = torch.zeros_like(seen_supervised), torch.zeros_like(seen_supervised)
    history, best, best_state, stale, steps, updates = [], math.inf, None, 0, 0, 0
    for epoch in range(epochs):
        model.train()
        optimizer.zero_grad(set_to_none=True)
        loss_sum = 0.
        epoch_batches = min(len(loader), max_steps-steps) if max_steps else len(loader)
        for index, batch in enumerate(loader):
            if cancelled and cancelled():
                raise RuntimeError('Whole-mouth training cancelled; active runtime model was not changed')
            batch = {key: value.to(device) for key, value in batch.items()}
            loss, _, targets, soft = training_loss(model, batch, teacher, approved, kd_weight, loss_kind, tongue_balance)
            if not torch.isfinite(loss):
                raise ValueError('Nonfinite training loss; checkpoint not exported')
            effective = torch.isfinite(targets) | (torch.isfinite(soft) if kd_weight > 0 else False)
            supervised_counts += torch.isfinite(targets).sum(0).cpu()
            row_indices = batch['sample_index'].cpu()
            seen_supervised[row_indices] |= torch.isfinite(targets).cpu()
            if kd_weight > 0:
                kd_counts += torch.isfinite(soft).sum(0).cpu()
                seen_kd[row_indices] |= torch.isfinite(soft).cpu()
                seen_kd_positive[row_indices] |= (torch.isfinite(soft) & (soft >= .5)).cpu()
            if bool(effective.any()):
                # Short final accumulation windows get their true denominator.
                window = min(accumulation, epoch_batches-(index//accumulation)*accumulation)
                (loss/window).backward()
            if (index+1) % accumulation == 0 or index+1 == len(loader):
                if any(p.grad is not None for p in model.parameters()):
                    optimizer.step()
                    updates += 1
                optimizer.zero_grad(set_to_none=True)
            loss_sum += float(loss.detach())
            steps += 1
            if max_steps and steps >= max_steps:
                # Flush an interrupted accumulation window rather than dropping its gradients.
                if any(p.grad is not None for p in model.parameters()):
                    optimizer.step()
                    updates += 1
                    optimizer.zero_grad(set_to_none=True)
                break
        if not updates:
            raise ValueError('No supervised or approved KD targets were optimized')
        metrics = evaluate_rows(model, validation)
        scores = [value['mae'] for name, value in metrics['channels'].items()
                  if value['mae'] is not None and (selection_channels is None or name in selection_channels)]
        score = float(np.mean(scores)) if scores else None
        if score is None:
            raise ValueError('Validation has no non-pseudo [0,1] targets')
        history.append(dict(epoch=epoch+1, loss=loss_sum/(index+1), validation_mae=score, steps=steps))
        if progress:
            progress(history[-1])
        if score < best:
            best, stale = score, 0
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
        else:
            stale += 1
        if stale >= patience or (max_steps and steps >= max_steps):
            break
    model.load_state_dict(best_state)
    inherited = deepcopy(initial_meta.get('training', {})) if initial_meta else {}
    runs = inherited.setdefault('runs', [])
    if teacher_meta:
        for run in teacher_meta.get('training', {}).get('runs', []):
            if run not in runs:
                runs.append(deepcopy(run))
    runs.append(dict(dataset=str(Path(dataset_source).resolve()), mode=mode, group_by=split['group_by'],
                     train_tokens=training_tokens(train_rows, split['group_by']), split=split,
                      label_sources=dict(Counter(source for row in train_rows for source in row['target_sources'] if source)),
                      loss_kind=loss_kind, augmentation=augmentation, source_weight_field='target_weights',
                      selection_channels=selection_channels,
                      tongue_bin_balance=tongue_balance,
                     teacher_model_id=teacher_meta['model_id'] if teacher_meta else None))
    trained = {name for index, name in enumerate(CHANNELS50) if supervised_counts[index]+kd_counts[index] > 0}
    if initial_meta:
        trained.update(initial_meta['trained_channels'])
    coverage = label_coverage(train_rows, seen_supervised, seen_kd, seen_kd_positive)
    positive_evidence = {name: 'reviewed-training-action-positive' for name, counts in coverage.items()
                         if counts['reviewed_positive_count'] > 0}
    if initial_meta:
        for name in initial_meta.get('positive_supervision_channels', []):
            if name in supported_channels(initial_meta):
                positive_evidence.setdefault(name, 'inherited-initial:'+initial_meta['model_id'])
    if teacher_meta:
        for name in teacher_meta.get('positive_supervision_channels', []):
            if name in approved and coverage[name]['kd_positive_count'] > 0:
                positive_evidence.setdefault(name, 'validated-teacher:'+teacher_meta['model_id'])
    touched = sorted(trained, key=CHANNELS50.index)
    eligible = {name for index, name in enumerate(CHANNELS50) if name in trained and
                (index < 33 or name in positive_evidence)}
    excluded = {name: 'new channel lacks reviewed action-positive supervision/validated positive KD coverage'
                for name in touched if name not in eligible}
    meta = checkpoint_meta(model, trained, inherited, tune=getattr(model, 'tune', 'all'),
                           mode=mode, seed=seed, history=history,
                           supervised_counts=supervised_counts.tolist(), kd_counts=kd_counts.tolist(),
                           label_coverage=coverage, positive_supervision_channels=list(positive_evidence),
                            positive_supervision_evidence=positive_evidence, optimized_channels=touched,
                            validation_eligible_channels=[name for name in CHANNELS50 if name in eligible],
                           excluded_channels=excluded, excluded_train_samples=excluded_train_count,
                           steps=steps, optimizer_updates=updates,
                           initialization_model_id=initial_meta['model_id'] if initial_meta else None)
    save_checkpoint(output, model, meta)
    write_json(Path(output).with_suffix('.training.json'), meta)
    return meta


@torch.inference_mode()
def evaluate_rows(model, rows, *, allow_evaluation_only=False):
    if not allow_evaluation_only:
        require_development(rows)
    model.eval()
    device = next(model.parameters()).device
    predictions = []
    for row in rows:
        value = model(read_frame(row, model.width)[None].to(device))[0].cpu().numpy()
        if value.shape != (50,) or not np.isfinite(value).all():
            raise ValueError('Nonfinite or incorrectly sized model output')
        predictions.append(value)
    return regression_metrics(rows, np.array(predictions))


def regression_metrics(rows, predictions):
    channels, sources, persons, sessions = {}, defaultdict(list), defaultdict(list), defaultdict(list)
    for index, name in enumerate(CHANNELS50):
        pairs = []
        reviewed = []
        for row, prediction in zip(rows, predictions):
            if row_excluded(row):
                continue
            target = row['targets'][index]
            source = row['target_sources'][index]
            if target is None or not math.isfinite(float(target)) or is_soft(source):
                continue
            target, value = float(target), float(prediction[index])
            error = abs(value-target)
            pairs.append((target, value))
            sources[source or 'unspecified'].append(error)
            persons[row['person_id']].append(error)
            sessions[row['person_id']+'/'+row['session_id']].append(error)
            if is_reviewed(source):
                reviewed.append((target, value))

        def stats(values, reviewed_levels=False):
            positive = [value >= .5 for target, value in values if target >= .5]
            negative = [value >= .5 for target, value in values if target <= .1]
            levels = {}
            for level in sorted({target for target, _ in values if target > .1}) if reviewed_levels else ():
                matching = [value for target, value in values if target == level]
                threshold = max(.1, level*.5)
                levels[str(level)] = dict(count=len(matching), activation_threshold=threshold,
                                         recall=float(np.mean(np.array(matching) >= threshold)),
                                         mae=float(np.mean(np.abs(np.array(matching)-level))))
            return dict(count=len(values), mae=float(np.mean([abs(value-target) for target, value in values])) if values else None,
                         positive_count=len(positive), negative_count=len(negative),
                         recall=float(np.mean(positive)) if positive else None,
                         false_positive_rate=float(np.mean(negative)) if negative else None,
                         positive_levels=levels)

        channels[name] = dict(stats(pairs), reviewed=stats(reviewed, reviewed_levels=True))
    observed = [value['mae'] for value in channels.values() if value['mae'] is not None]
    return dict(samples=len(rows), macro_mae=float(np.mean(observed)) if observed else None, channels=channels,
                by_source={key: dict(count=len(values), mae=float(np.mean(values))) for key, values in sources.items()},
                by_person={key: dict(count=len(values), mae=float(np.mean(values))) for key, values in persons.items()},
                by_session={key: dict(count=len(values), mae=float(np.mean(values))) for key, values in sessions.items()},
                interpretation='Label-source agreement; only reviewed labels enter candidate approval. Not anatomical accuracy.')


def validate_candidate(candidate, candidate_meta, baseline, baseline_meta, rows, split,
                       max_regression=.01, max_fp_increase=.02, min_improvement=0.,
                       min_recall=.5, max_fp_rate=.1):
    require_development(rows)
    if candidate_meta['model_id'] == baseline_meta['model_id']:
        raise ValueError('Candidate and baseline are the same model; choose a separately trained baseline')
    held = select_rows(rows, split, 'validation')
    if not held:
        raise ValueError('Candidate validation partition is empty')
    assert_holdout(candidate_meta, held)
    assert_holdout(baseline_meta, held)
    after, before = evaluate_rows(candidate, held), evaluate_rows(baseline, held)
    approved, reasons = [], {}
    for name in supported_channels(candidate_meta):
        a, b = after['channels'][name]['reviewed'], before['channels'][name]['reviewed']
        if name not in supported_channels(baseline_meta):
            reasons[name] = 'baseline channel is untrained; not a matched trained comparison'
        elif not a['positive_count'] or not a['negative_count']:
            reasons[name] = 'needs reviewed positive and difficult-negative validation examples'
        elif a['recall'] < min_recall or a['false_positive_rate'] > max_fp_rate:
            reasons[name] = 'does not meet absolute reviewed recall/false-positive thresholds'
        elif a['mae'] > b['mae']+max_regression or a['false_positive_rate'] > b['false_positive_rate']+max_fp_increase:
            reasons[name] = 'regressed on reviewed validation labels'
        elif b['mae']-a['mae'] < min_improvement:
            reasons[name] = 'did not meet required reviewed MAE improvement'
        else:
            approved.append(name)
    return dict(schema=SCHEMA, channels=list(CHANNELS50), partition='validation',
                candidate_model_id=candidate_meta['model_id'], baseline_model_id=baseline_meta['model_id'],
                accepted=bool(approved), approved_channels=approved, rejected_channels=reasons,
                candidate=after, baseline=before,
                thresholds=dict(max_regression=max_regression, max_fp_increase=max_fp_increase,
                                min_improvement=min_improvement, min_recall=min_recall, max_fp_rate=max_fp_rate),
                note='Channel-wise candidate only; no runtime activation. Test partition is untouched.')


@torch.inference_mode()
def predict_pseudo(model, meta, rows, split, output, approval, dataset_meta=None, max_tta_disagreement=.05):
    require_development(rows, dataset_meta)
    if not isinstance(model, Teacher50):
        raise ValueError('Pseudo-label teacher must be DINO, not an old frozen student')
    if not dataset_meta or dataset_meta.get('samples_complete') is not True:
        raise ValueError('Stop recording before teacher pseudo-label prediction; samples_complete must be true')
    allowed = load_approval(approval, meta)
    train_rows = select_rows(rows, split, 'train')
    assert_train_pool(meta, train_rows)
    # The validation/test identities remain archived, never pseudo-labelled for training.
    model.eval()
    model.requires_grad_(False)
    device = next(model.parameters()).device
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError('Pseudo-label output must be a new/empty directory; original data is never overwritten')
    output.mkdir(parents=True, exist_ok=True)
    (output/'images').mkdir()
    write_json(output/'manifest.json', dict(schema=SCHEMA, channels=list(CHANNELS50),
                                          samples_complete=False, evaluation_only=False))
    counts = Counter()
    excluded_samples = 0
    with (output/'samples.jsonl').open('w', encoding='utf-8') as stream:
        for sample_index, row in enumerate(rows):
            saved = {key: value for key, value in row.items() if not key.startswith('_')}
            # Shared dataset paths stay inside the corpus. Preserve JPEG bytes and source groups.
            image = Path(row['_image'])
            saved['image'] = f'images/{sample_index:09d}{image.suffix}'
            shutil.copyfile(image, output/saved['image'])
            saved['parent_image'] = str(image)
            if partition_for(row, split) != 'train' or row_excluded(row):
                # Do not carry stale pseudo targets into a sealed holdout.
                saved.pop('teacher_targets', None)
                saved.pop('teacher_sources', None)
                saved.pop('teacher_model_id', None)
                excluded_samples += int(row_excluded(row))
            else:
                frame = read_frame(row, model.width)[None].to(device)
                variants = torch.cat((frame, (frame*.97).clamp(0, 1), (frame*1.03).clamp(0, 1)))
                values = model(variants).cpu().numpy()
                if not np.isfinite(values).all():
                    raise ValueError('Teacher produced nonfinite pseudo-labels')
                disagreement = values.max(0)-values.min(0)
                soft, sources = [None]*50, [None]*50
                supervised, _ = row_targets(row)
                for index, name in enumerate(CHANNELS50):
                    if name in allowed and not torch.isfinite(supervised[index]) and disagreement[index] <= max_tta_disagreement:
                        soft[index] = float(values[0, index])
                        sources[index] = 'teacher:'+meta['model_id']
                        counts[name] += 1
                saved['teacher_targets'], saved['teacher_sources'] = soft, sources
                saved['teacher_model_id'] = meta['model_id']
            stream.write(json.dumps(saved, ensure_ascii=False, allow_nan=False)+'\n')
    manifest = dict(dataset_meta or {}, schema=SCHEMA, channels=list(CHANNELS50), samples_complete=True,
                    pseudo=dict(teacher_model_id=meta['model_id'], approved_channels=sorted(allowed),
                                partition='train', accepted_counts=dict(counts),
                                review_or_image_excluded_samples=excluded_samples,
                                teacher_training=meta.get('training', {}),
                                teacher_trained_channels=supported_channels(meta),
                                teacher_positive_supervision_channels=meta.get('positive_supervision_channels', []),
                                max_tta_disagreement=max_tta_disagreement, split=split,
                                warning='TTA agreement is a filter, not evidence of accuracy'),
                    privacy='Images, targets and weights stay on this PC; no network operation')
    write_json(output/'manifest.json', manifest)
    return manifest


def export_onnx(model, meta, output, approval=None):
    """Export a candidate; numerically verify all 50 outputs on CPU, never activate it."""
    output = Path(output)
    if output.suffix != '.onnx':
        raise ValueError('Export output must end in .onnx')
    if output.exists() or output.with_suffix('.json').exists():
        raise FileExistsError('Export needs a new ONNX/JSON pair; existing model artifacts are not overwritten')
    approved = load_approval(approval, meta) if approval else set()
    validated = [name for name in CHANNELS50 if name in approved]
    import onnx
    import onnxruntime as ort
    output.parent.mkdir(parents=True, exist_ok=True)
    model = model.cpu().eval()
    width = model.width
    with torch.inference_mode():
        torch.onnx.export(model, torch.zeros(1, 1, width, width), str(output),
                          input_names=['frame'], output_names=['blendshapes'], opset_version=17, dynamo=False)
    onnx.checker.check_model(str(output))
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(str(output), options, providers=['CPUExecutionProvider'])
    generator = torch.Generator().manual_seed(73)
    probes = [torch.zeros(1, 1, width, width), torch.rand(1, 1, width, width, generator=generator),
              torch.linspace(0, 1, width*width).reshape(1, 1, width, width)]
    errors = np.zeros(50, np.float32)
    with torch.inference_mode():
        for probe in probes:
            expected = model(probe).numpy()
            actual = session.run(['blendshapes'], {'frame': probe.numpy()})[0]
            if actual.shape != (1, 50) or not np.isfinite(actual).all():
                raise ValueError('Exported ONNX has invalid output')
            np.testing.assert_allclose(actual, expected, atol=1e-5, rtol=1e-4)
            errors = np.maximum(errors, np.abs(actual-expected)[0])
    trained = list(meta['trained_channels'])
    manifest = dict(schema=SCHEMA, channels=list(CHANNELS50), model_kind=meta['model_kind'], kind=meta['model_kind'],
                    model_id=meta['model_id'], width=width, height=width, trained_channels=trained,
                    optimized_channels=meta.get('optimized_channels', trained),
                    validation_eligible_channels=supported_channels(meta),
                    valid_mask=sum(1 << index for index, name in enumerate(CHANNELS50) if name in validated),
                    validated_channels=validated, candidate_only=True, training=meta['training'],
                    initialization=meta.get('initialization', {}),
                    label_coverage=meta.get('label_coverage', {}),
                    stock_channels=list(CHANNELS50[:33]),
                    positive_supervision_channels=meta.get('positive_supervision_channels', []),
                    positive_supervision_evidence=meta.get('positive_supervision_evidence', {}),
                    excluded_channels=meta.get('excluded_channels', {}),
                    input_name='frame', output_name='blendshapes',
                    preprocessing='Pillow BILINEAR full GRAY8 /255; B1HW; RGB repeat and ImageNet normalization internal',
                    normalization_internal=True, output_range=[0, 1],
                    numerical_check=dict(provider='CPUExecutionProvider', probes=len(probes),
                                         per_channel_max_abs=dict(zip(CHANNELS50, errors.astype(float).tolist())),
                                         max_abs=float(errors.max()), atol=1e-5, rtol=1e-4),
                    privacy='Local artifact; no images or pseudo-labels uploaded')
    write_json(output.with_suffix('.json'), manifest)
    return manifest
