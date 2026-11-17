"""User-operated capture lifecycle, with an explicitly supplied headset source."""
import threading
import time

from face_presets import capture_preset
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
    def __init__(self, runtime, source, *, display_viewer=None):
        self.runtime, self.source = runtime, source
        self.display_viewer = display_viewer
        self.state = 'idle'
        self.error = ''
        self.thread = None
        self.stop_event = threading.Event()
        self.cancelled = False

    def status(self):
        return dict(self.runtime.status(), managed_capture=True,
                    capture_control=dict(state=self.state, error=self.error, viewer=self.display_viewer))

    def recording(self, action, **options):
        if action == 'begin':
            if options.get('confirmed') is not True or not options.get('person_id', '').strip():
                raise ValueError('Confirm local recording and enter an anonymous recording name')
            if self.thread is not None and self.thread.is_alive():
                raise ValueError('A capture session is already active')
            reuse_input = self.runtime.running and self.runtime.mode == 'live' and self.runtime.config['model_kind'] == 'face50'
            if self.runtime.running and not reuse_input:
                raise ValueError('Close the current input before starting managed capture')
            if options.get('guide', True):
                capture_preset(options.get('preset_id', 'complete'))
            self.stop_event.clear()
            self.cancelled, self.error, self.state = False, '', 'starting'
            self.thread = threading.Thread(target=self._run, args=(options, reuse_input), name='guided-capture', daemon=False)
            self.thread.start()
            return self.status()
        if action in ('stop', 'cancel'):
            self.cancelled = action == 'cancel'
            self.stop()
            return self.status()
        return self.runtime.recording(action)

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
            self.state = 'error' if self.error else 'cancelled' if self.cancelled else 'completed'

    def close(self):
        self.stop()
        if self.thread is not None:
            self.thread.join()
