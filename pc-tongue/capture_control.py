"""User-operated capture lifecycle, with an explicitly supplied headset source."""
from pathlib import Path
import threading
import time

from face_dataset import write_json
from face_presets import SEALED_PROFILES, capture_preset, capture_presets, latest_capture_continuation
from discovery import discover_sources


class LANCameraSource:
    """Select an existing camera service; Runtime owns only this client's socket."""
    def __init__(self, runtime):
        self.runtime = runtime

    def start(self):
        host = self.runtime.config.get('headset_host', '')
        port = self.runtime.config.get('headset_port', 45681)
        devices = discover_sources(targets=[(host, port)] if host else None)['devices']
        if len(devices) != 1:
            raise ValueError('Select one available headset in connection settings')
        return dict(host=devices[0]['host'], port=devices[0]['port'])

    def stop(self):
        # Runtime.stop closes this subscription. The shared headset service stays available.
        pass


class CaptureControl:
    def __init__(self, runtime, source, *, display_viewer=None, continuation=None, sealed_preset=None):
        self.runtime, self.source = runtime, source
        self.display_viewer = display_viewer
        self.continuation = continuation
        self.sealed_preset = sealed_preset
        self.state = 'idle'
        self.error = ''
        self.thread = None
        self.stop_event = threading.Event()
        self.cancelled = False

    def status(self):
        control = dict(state=self.state, error=self.error, viewer=self.display_viewer)
        if self.sealed_preset:
            control.update(sealed_c=True, preset_id=self.sealed_preset)
        return dict(self.runtime.status(), managed_capture=True, capture_control=control)

    def presets(self):
        if self.sealed_preset:
            return dict(presets=[capture_preset(self.sealed_preset)], default=self.sealed_preset, sealed_c=True)
        presets = capture_presets()
        continuation = (self.continuation if self.state == 'idle' and self.continuation else
                        latest_capture_continuation(self.runtime.config_path.parent/'recordings'))
        if continuation:
            presets.insert(0, continuation['preset'])
            settings = {key: continuation[key] for key in ('person_id', 'label_stage', 'session_id')}
            return dict(presets=presets, default='continuation' if self.state == 'idle' and self.continuation else 'complete',
                        continuation=settings)
        return dict(presets=presets, default='complete')

    def recording(self, action, **options):
        if action == 'begin':
            continuation_session_id = options.pop('continuation_session_id', None)
            if options.get('confirmed') is not True or not options.get('person_id', '').strip():
                raise ValueError('Confirm local recording and enter an anonymous recording name')
            if self.sealed_preset:
                if self.state != 'idle':
                    raise ValueError('This sealed C attempt has already started; it cannot be restarted or continued')
                if continuation_session_id or options.get('preset_id') == 'continuation' or options.get('step_index') not in (None, 0):
                    raise ValueError('Sealed C must start at the first action of its fixed plan')
                options.update(role='C', evaluation_only=True, preset_id=self.sealed_preset, guide=True,
                               stage='raw_only', receive_results=True)
            elif options.get('preset_id') in SEALED_PROFILES.values():
                raise ValueError('Use the isolated sealed C recording mode for this plan')
            receive_results = options.get('receive_results', True)
            if type(receive_results) is not bool or (options.get('stage') == 'instant' and not receive_results):
                raise ValueError('Instant labels require receiving mouth results')
            if self.thread is not None and self.thread.is_alive():
                raise ValueError('A capture session is already active')
            reuse_input = self.runtime.running and self.runtime.mode == 'live' and self.runtime.config['model_kind'] == 'face50'
            if self.runtime.running and not reuse_input:
                raise ValueError('Close the current input before starting managed capture')
            if options.get('preset_id') == 'continuation':
                continuation = (self.continuation if self.state == 'idle' and self.continuation else
                                latest_capture_continuation(self.runtime.config_path.parent/'recordings'))
                if (not continuation or not options.get('guide') or
                        continuation_session_id not in (None, continuation['session_id'])):
                    raise ValueError('Select the available guided continuation')
                if (options['person_id'] != continuation['person_id'] or
                        options.get('stage') != continuation['label_stage'] or options.get('evaluation_only')):
                    raise ValueError('Use the previous recording name and label stage for continuation')
                options.update(continuation=continuation, role=continuation['role'])
            elif options.get('guide', True):
                capture_preset(options.get('preset_id', 'complete'))
            from face_capture import MouthGuide
            if options.get('guide', True):
                MouthGuide(options.get('preset_id', 'complete'), continuation=options.get('continuation'),
                           step_index=options.get('step_index') if options.get('step_index') is not None else 0,
                           prepare_seconds=options.get('prepare_seconds', 3.))
            self.stop_event.clear()
            self.cancelled, self.error, self.state = False, '', 'starting'
            self.thread = threading.Thread(target=self._run, args=(options, reuse_input), name='guided-capture', daemon=False)
            self.thread.start()
            return self.status()
        if action in ('stop', 'cancel'):
            self.cancelled = action == 'cancel'
            self.stop()
            return self.status()
        return self.runtime.recording(action, **{key: options[key] for key in ('step_index', 'guide_frame') if key in options})

    def stop(self):
        if self.thread is not None and self.thread.is_alive():
            self.state = 'stopping'
            self.stop_event.set()
        else:
            self.runtime.stop()
        return self.status()

    def _run(self, options, reuse_input):
        try:
            if not reuse_input:
                endpoint = self.source.start()
                if self.stop_event.is_set():
                    return
                self.runtime.configure(dict(headset_host=endpoint['host'], headset_port=endpoint['port'],
                                            model_kind='face50'))
                self.runtime.start('capture')
            deadline = time.monotonic()+10
            while not self.runtime.connected:
                if self.stop_event.wait(.02):
                    return
                if self.runtime.last_error or time.monotonic() >= deadline:
                    raise RuntimeError(self.runtime.last_error or 'Capture input did not connect')
            self.runtime.recording('begin', **options)
            self.state = 'recording'
            while not self.stop_event.wait(.1):
                record = self.runtime.record_status
                if record.get('error') or self.runtime.last_error:
                    raise RuntimeError(record.get('error') or self.runtime.last_error)
                guide = record.get('guide') or {}
                if self.sealed_preset and not self.runtime.connected and guide.get('phase') != 'finished':
                    raise RuntimeError('Camera stream ended before the sealed guide finished')
                if guide.get('phase') == 'finished' or record.get('state') in ('stopped', 'cancelled'):
                    break
        except Exception as error:
            self.error = str(error)
        finally:
            self.state = 'stopping'
            try:
                self.source.stop()
            except Exception as error:
                self.error = str(error) if not self.error else self.error+'; '+str(error)
            try:
                self.runtime.stop(cancel_recording=self.cancelled)
            except Exception as error:
                self.error = str(error) if not self.error else self.error+'; '+str(error)
            if self.sealed_preset:
                record = self.runtime.record_status
                guide = record.get('guide') or {}
                directory = Path(record['path']) if record.get('path') else self.runtime.config_path.parent
                try:
                    write_json(directory/'capture-outcome.json', dict(
                        session_id=record.get('session_id'), archive_state=record.get('state'),
                        complete=not self.error and record.get('state') == 'stopped'
                        and guide.get('phase') == 'finished' and not guide.get('skipped_indices'),
                        error=self.error or None, step_index=guide.get('index'),
                        skipped_indices=guide.get('skipped_indices', [])))
                except OSError as error:
                    self.error = '; '.join(filter(None, (self.error, f'Could not save sealed outcome: {error}')))
            self.state = 'error' if self.error else 'cancelled' if self.cancelled else 'completed'

    def close(self):
        self.stop()
        if self.thread is not None:
            self.thread.join()
