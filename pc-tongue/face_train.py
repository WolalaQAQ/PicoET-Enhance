"""Explicit offline CLI for whole-mouth teacher/student training. --help needs stdlib only."""
import argparse
import json
from pathlib import Path


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest='command', required=True)

    def data(command):
        command.add_argument('--dataset', required=True, help='Directory with manifest.json and samples.jsonl')
        command.add_argument('--split', required=True, help='Frozen grouped split JSON')

    split = commands.add_parser('split', help='Freeze person/session and all derived lineage groups')
    data(split)
    split.add_argument('--group-by', choices=['person', 'session'], default='person')
    split.add_argument('--seed', type=int, default=42)
    split.add_argument('--validation-fraction', type=float, default=.2)
    split.add_argument('--test-fraction', type=float, default=.2)
    split.add_argument('--validation-group', action='append', default=[])
    split.add_argument('--test-group', action='append', default=[])

    for name in ('teacher-train', 'student-baseline', 'student-distill', 'local-adapt'):
        command = commands.add_parser(name)
        data(command)
        command.add_argument('--output', required=True, help='New candidate .pt; never activates runtime')
        command.add_argument('--dino-repo', help='Explicit local DINO source checkout, never torch.hub GitHub')
        command.add_argument('--initial', help='Continue a whole-mouth checkpoint, inheriting its training lineage')
        command.add_argument('--epochs', type=int, default=10)
        command.add_argument('--batch-size', type=int, default=8)
        command.add_argument('--lr', type=float, default=1e-4 if name == 'teacher-train' else 1e-3)
        command.add_argument('--encoder-lr', type=float, default=1e-5)
        command.add_argument('--accumulation', type=int, default=1)
        command.add_argument('--patience', type=int, default=3)
        command.add_argument('--max-steps', type=int)
        command.add_argument('--device', default='cpu', help='Explicit torch device, e.g. cpu or cuda:0')
        command.add_argument('--seed', type=int, default=42)
        command.add_argument('--threads', type=int, default=1)
        command.add_argument('--loss', choices=['mse', 'huber'], default='mse')
        command.add_argument('--augmentation', choices=['none', 'mild'], default='none')
        command.add_argument('--selection-channel', action='append', help='Predeclared early-stopping channels')
        command.add_argument('--balance-tongue-bins', action='store_true',
                             help='Experimental train-only Out response-bin loss weighting')
        if name == 'teacher-train':
            command.add_argument('--teacher-backbone', choices=['dinov2', 'dinov3', 'dinov3-large', 'dinov3-huge'],
                                 default='dinov2',
                                 help='DINOv2 B/14-reg (392px) or DINOv3 B/L/H+ (400px); --initial inherits its kind')
            command.add_argument('--backbone-checkpoint', help='Local official state_dict for the selected DINO backbone')
            command.add_argument('--tune', choices=['frozen', 'last-two'], default='frozen')
        else:
            command.add_argument('--student-input',type=int,choices=[160,224],
                                 help='Student input size; default 160 or inherited from --initial')
            command.add_argument('--pretrained', help='Local torchvision MobileNetV3-small state_dict')
            command.add_argument('--random-init', action='store_true', help='Explicit unpretrained baseline/test, no download')
            if name in ('student-distill', 'local-adapt'):
                command.add_argument('--teacher', help='Distributed whole-mouth teacher .pt; omit for saved pseudo corpus')
                command.add_argument('--teacher-approval', help='Accepted validate-candidate report for exact teacher model_id')
                command.add_argument('--kd-weight', type=float, default=.5)
            if name == 'local-adapt':
                command.add_argument('--tune', choices=['head', 'all'], default='head')

    pseudo = commands.add_parser('predict-pseudo', help='Teacher labels only train groups; existing targets stay unchanged')
    data(pseudo)
    pseudo.add_argument('--teacher', required=True)
    pseudo.add_argument('--teacher-approval', required=True)
    pseudo.add_argument('--dino-repo')
    pseudo.add_argument('--output', required=True, help='New local corpus directory; copies image bytes and retains source groups')
    pseudo.add_argument('--max-tta-disagreement', type=float, default=.05)
    pseudo.add_argument('--device', default='cpu')

    evaluate = commands.add_parser('evaluate')
    data(evaluate)
    evaluate.add_argument('--checkpoint', required=True)
    evaluate.add_argument('--partition', choices=['validation', 'test'], default='validation')
    evaluate.add_argument('--dino-repo')
    evaluate.add_argument('--output', required=True)
    evaluate.add_argument('--allow-evaluation-only', action='store_true',
                          help='Explicit read-only evaluation of consumed C; never candidate selection or training')

    validate = commands.add_parser('validate-candidate', help='Reviewed channel-wise validation versus baseline; no activation')
    data(validate)
    validate.add_argument('--candidate', required=True)
    validate.add_argument('--baseline', required=True)
    validate.add_argument('--dino-repo')
    validate.add_argument('--output', required=True)
    validate.add_argument('--max-regression', type=float, default=.01)
    validate.add_argument('--max-fp-increase', type=float, default=.02)
    validate.add_argument('--min-improvement', type=float, default=0.)
    validate.add_argument('--min-recall', type=float, default=.5)
    validate.add_argument('--max-fp-rate', type=float, default=.1)

    export = commands.add_parser('export', help='ONNX + adjacent JSON; verifies all outputs using CPU ORT')
    export.add_argument('--checkpoint', required=True)
    export.add_argument('--dino-repo')
    export.add_argument('--approval')
    export.add_argument('--output', required=True)
    return root


def run(args):
    import face_training as ft
    import torch
    command = args.command
    if command == 'export':
        model, meta = ft.load_checkpoint(args.checkpoint, args.dino_repo)
        return ft.export_onnx(model, meta, args.output, args.approval)
    allow_evaluation_only = command == 'evaluate' and args.allow_evaluation_only
    dataset_meta, rows = ft.load_corpus(args.dataset, allow_evaluation_only=allow_evaluation_only)
    if command == 'split':
        if Path(args.split).exists():
            raise FileExistsError('Frozen split already exists; choose a new path to review a new split')
        result = ft.make_split(rows, args.group_by, args.seed, args.validation_fraction, args.test_fraction,
                               args.validation_group, args.test_group)
        ft.write_json(args.split, result)
        return result
    split = ft.read_json(args.split)
    if command == 'evaluate':
        model, meta = ft.load_checkpoint(args.checkpoint, args.dino_repo)
        held = ft.select_rows(rows, split, args.partition)
        ft.assert_holdout(meta, held)
        result = dict(model_id=meta['model_id'], partition=args.partition,
                      metrics=ft.evaluate_rows(model, held, allow_evaluation_only=allow_evaluation_only),
                      evaluation_only=dataset_meta['evaluation_only'])
        ft.write_json(args.output, result)
        return result
    if command == 'validate-candidate':
        candidate, cm = ft.load_checkpoint(args.candidate, args.dino_repo)
        baseline, bm = ft.load_checkpoint(args.baseline, args.dino_repo)
        result = ft.validate_candidate(candidate, cm, baseline, bm, rows, split,
                                       args.max_regression, args.max_fp_increase, args.min_improvement,
                                       args.min_recall, args.max_fp_rate)
        ft.write_json(args.output, result)
        return result
    if command == 'predict-pseudo':
        teacher, meta = ft.load_checkpoint(args.teacher, args.dino_repo, args.device)
        return ft.predict_pseudo(teacher, meta, rows, split, args.output, args.teacher_approval,
                                 dataset_meta, args.max_tta_disagreement)
    torch.set_num_threads(args.threads)
    torch.manual_seed(args.seed)
    initial_meta = None
    if Path(args.output).exists():
        raise FileExistsError('Candidate checkpoint exists; choose a new output, not an active/standard checkpoint')
    if command == 'local-adapt' and not args.initial:
        raise ValueError('local-adapt requires --initial student checkpoint')
    if args.initial:
        model, initial_meta = ft.load_checkpoint(args.initial, args.dino_repo)
        if command == 'teacher-train':
            if not isinstance(model, ft.Teacher50):
                raise ValueError('Teacher continuation requires a DINO teacher checkpoint')
            model.set_tune(args.tune)
        elif not isinstance(model, ft.Student50):
            raise ValueError('Student continuation requires a 50-channel student checkpoint')
        elif args.student_input is not None:
            model.width = args.student_input
    elif command == 'teacher-train':
        if not args.backbone_checkpoint:
            raise ValueError('teacher-train requires --backbone-checkpoint or --initial; no auto-download')
        model = ft.Teacher50(ft.local_dino(args.dino_repo, args.backbone_checkpoint, family=args.teacher_backbone),
                            tune=args.tune, family=args.teacher_backbone)
    else:
        if not args.pretrained and not args.random_init:
            raise ValueError('Student needs --pretrained local checkpoint or explicit --random-init')
        model = ft.Student50(args.pretrained, width=args.student_input or 160)
    if command == 'local-adapt' and args.tune == 'head':
        model.features.requires_grad_(False)
    teacher = teacher_meta = None
    approved = ()
    kd_weight = getattr(args, 'kd_weight', 0.)
    if getattr(args, 'teacher', None):
        if not args.teacher_approval:
            raise ValueError('--teacher requires --teacher-approval from independent validation')
        teacher, teacher_meta = ft.load_checkpoint(args.teacher, args.dino_repo, args.device)
        if not isinstance(teacher, ft.Teacher50):
            raise ValueError('The online teacher must be a DINO model, not a frozen old student')
        approved = ft.load_approval(args.teacher_approval, teacher_meta)
    if command in ('student-distill', 'local-adapt') and teacher is None:
        train_rows = ft.select_rows(rows, split, 'train')
        if not any(bool(torch.isfinite(ft.row_targets(row)[1]).any()) for row in train_rows):
            raise ValueError('Distillation/local-adapt needs approved local teacher or saved teacher_targets')
        pseudo = dataset_meta.get('pseudo')
        if not pseudo:
            raise ValueError('Saved pseudo-label corpus needs manifest.pseudo provenance')
        if pseudo['split'] != split:
            raise ValueError('Pseudo-label split must remain identical; do not relabel validation/test as train')
        pseudo_meta = dict(training=pseudo['teacher_training'])
        ft.assert_train_pool(pseudo_meta, train_rows)
        ft.assert_holdout(pseudo_meta, ft.select_rows(rows, split, 'validation'))
        for row in train_rows:
            soft = ft.row_targets(row)[1]
            if any(bool(torch.isfinite(soft[index])) and name not in pseudo['approved_channels']
                   for index, name in enumerate(ft.CHANNELS50)):
                raise ValueError('Saved pseudo labels include channels not approved for this teacher')
        # Saved pseudo labels retain the distributed teacher's reviewed-positive evidence.
        teacher_meta = dict(model_id=pseudo['teacher_model_id'], training=pseudo['teacher_training'],
                            trained_channels=pseudo['teacher_trained_channels'],
                            positive_supervision_channels=pseudo['teacher_positive_supervision_channels'])
        approved = set(pseudo['approved_channels']) & set(ft.supported_channels(teacher_meta))
    return ft.train_model(model, rows, split, args.output, dataset_source=args.dataset, initial_meta=initial_meta,
                          teacher=teacher, teacher_meta=teacher_meta, approved=approved,
                          epochs=args.epochs, batch_size=args.batch_size, learning_rate=args.lr,
                          encoder_lr=args.encoder_lr, kd_weight=kd_weight, accumulation=args.accumulation,
                          patience=args.patience, max_steps=args.max_steps, device=args.device,
                           seed=args.seed, mode=command,
                           loss_kind=args.loss, augmentation=args.augmentation,
                           selection_channels=args.selection_channel,
                           balance_tongue_bins=args.balance_tongue_bins,
                          progress=lambda row: print(json.dumps(dict(event='progress', **row)), flush=True))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
    except (ValueError, FileNotFoundError, FileExistsError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(dict(event='result', result=result), ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
