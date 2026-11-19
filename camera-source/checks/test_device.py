"""Controller regression with a fake ADB peer; never opens a device."""
import copy
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from device import Device, Session, birth, validate_ipc


def stat(stamp):
    return '101 (name with spaces) S '+' '.join(['0']*18+[str(stamp)])


class FakeDevice:
    def __init__(self):
        self.boot, self.stamp = 'test-boot', 555
        self.events = []
        self.camera = self.shim = self.daemon = None
        self.injections = 0
        self.tracking = 'FT algorithm is not started\nET algorithm is not started\nThere is 0 clients'
        self.stock_sink = False  # Official 2.4.2 has no picoet_set_stock_sink export.
        self.stock_input_identity = True

    def adb(self, *args):
        self.events.append(args)
        return ''

    def root(self, script):
        self.events.append(script)
        if script == 'cat /proc/sys/kernel/random/boot_id':
            return self.boot
        if script == 'pidof pxreyetrackingservice':
            return '101'
        if script == 'cat /proc/101/stat':
            return stat(self.stamp)
        if script == 'cat /proc/202/stat':
            return stat(666)
        if script == 'cat /proc/101/maps':
            return ''
        if script == 'dumpsys pxreyetrackingservice':
            return self.tracking
        if '--run-locked' in script:
            self.injections += 1
            self.camera = dict(pid=101, start_ticks=555, owner_id='0000000000000abc', state='idle',
                               hook_status=0, command_id=0, command_result=0,
                               camera=dict(armed=0, in_flight=0), video=dict(session_id=0, running=0, queue_in_use=0),
                               stock=dict(sink_registered=False, available=False, association_ready=False, state='inactive'))
        if script.startswith('nohup '):
            session = 123 if not self.daemon else self.daemon['session']+1
            self.daemon = dict(session=session, daemon_pid=202, daemon_birth=666, state=1, client_pid=101,
                               client_birth=555, attach_ack=1, attach_ack_session=session,
                               root_nonce=[10, 20], client_nonce=[30, 40], attach_root_nonce=[10, 20],
                               attach_client_nonce=[30, 40], stop_ack=0, in_use=0,
                               stop_ack_session=session, stop_client_nonce=[30, 40], stock=dict(enqueued=0))
        if 'camera-control.pending' in script:
            owner, number, verb = shlex.split(script)[2].split()
            assert owner == self.camera['owner_id']
            self.camera.update(command_id=int(number), state='armed' if verb == 'start' else 'stopped', hook_status=1)
            self.camera['camera']['armed'] = int(verb == 'start')
            self.camera['video'].update(session_id=self.daemon['session'], running=int(verb == 'start'))
            active_stock = self.stock_sink and verb == 'start'
            self.camera['stock'].update(sink_registered=active_stock, available=active_stock,
                                       association_ready=active_stock and self.stock_input_identity,
                                       state='association_ready_not_device_verified' if active_stock and self.stock_input_identity else
                                             'available_unassociated_missing_input_identity' if active_stock else
                                             'jpeg_only_missing_main_payload_sink' if verb == 'start' else 'inactive')
            self.shim = {k: self.daemon[k] for k in ('session', 'daemon_pid', 'daemon_birth', 'client_birth')}
            self.shim.update(pid=101, state='attached' if verb == 'start' else 'stopped_acknowledged',
                             retained_mapping=False, root_nonce='000000000000000a0000000000000014',
                             client_nonce='000000000000001e0000000000000028')
            self.daemon.update(state=1 if verb == 'start' else 3, stop_ack=int(verb == 'stop'))
        return ''

    def read_json(self, stage, name):
        return copy.deepcopy({'camera-status.json': self.camera, 'ipc-shim-status.json': self.shim,
                              'ipc-daemon-status.json': self.daemon}[name])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.peer = FakeDevice()
        self.session = Session(self.peer, self.root/'session.json', 'test-device')
        for name in ('libptj_source.so', 'libpicoet_camera_jpeg.so', 'ptj_ipc_daemon', 'picoet-inject'):
            (self.root/name).write_bytes(b'fixture')

    def start(self):
        return self.session.start(self.root)

    def test_stat_parser_preserves_comm_spaces(self):
        self.assertEqual(birth(stat(1234)), 1234)

    def test_fresh_start_saves_recovery_handle_and_uses_owned_stage(self):
        value = self.start()
        self.assertEqual(value['camera-status']['camera']['armed'], 1)
        saved = json.loads(self.session.file.read_text())
        self.assertEqual(saved['daemon']['session'], 123)
        self.assertTrue(saved['stage'].startswith('/data/local/tmp/picoet-tongue-'))
        self.assertEqual(self.peer.injections, 1)
        self.assertFalse(saved['stopped'])

    def test_existing_live_session_does_not_reinject(self):
        self.start()
        with self.assertRaisesRegex(RuntimeError, 'not stopped'):
            self.start()
        self.assertEqual(self.peer.injections, 1)

    def test_official_payload_is_jpeg_only_and_is_not_automatically_replaced(self):
        value = self.start()
        self.assertTrue(value['capabilities']['jpeg_available'])
        self.assertFalse(value['capabilities']['stock_labels_available'])
        self.assertEqual(value['capabilities']['stock_state'], 'jpeg_only_missing_main_payload_sink')
        self.assertFalse(value['capabilities']['stock_observed_in_ipc'])
        self.assertFalse(any('libpicoet_hook.so' in str(event) for event in self.peer.events))

    def test_matching_sink_availability_does_not_claim_received_stock(self):
        self.peer.stock_sink = True
        value = self.start()
        self.assertTrue(value['capabilities']['stock_labels_available'])
        self.assertTrue(value['capabilities']['stock_association_ready'])
        self.assertFalse(value['capabilities']['stock_device_verified'])
        self.assertFalse(value['capabilities']['stock_observed_in_ipc'])
        self.peer.daemon['stock']['enqueued'] = 3
        self.assertTrue(self.session.status()['capabilities']['stock_observed_in_ipc'])
        stopped = self.session.stop()
        self.assertFalse(stopped['capabilities']['stock_labels_available'])
        self.assertFalse(stopped['capabilities']['stock_observed_in_ipc'])

    def test_legacy_adapter_status_is_unknown_not_stock_capable(self):
        self.start()
        self.peer.camera.pop('stock')
        value = self.session.status()['capabilities']
        self.assertTrue(value['jpeg_available'])
        self.assertFalse(value['stock_labels_available'])
        self.assertEqual(value['stock_state'], 'unknown_adapter_capability')

    def test_previous_label_payload_without_input_identity_stays_unassociated(self):
        self.peer.stock_sink = True
        self.peer.stock_input_identity = False
        value = self.start()['capabilities']
        self.assertTrue(value['stock_labels_available'])
        self.assertFalse(value['stock_association_ready'])
        self.assertEqual(value['stock_state'], 'available_unassociated_missing_input_identity')

    def test_stop_and_restart_use_same_adapter_with_new_daemon_session(self):
        self.start()
        self.session.stop()
        self.assertTrue(self.session.state['stopped'])
        self.start()
        self.assertEqual(self.peer.injections, 1)
        self.assertEqual(self.session.state['daemon']['session'], 124)

    def test_boot_and_service_birth_changes_never_send_commands(self):
        self.start()
        for key, changed in [('boot', 'other-boot'), ('stamp', 888)]:
            original = getattr(self.peer, key)
            setattr(self.peer, key, changed)
            start = len(self.peer.events)
            with self.assertRaises(RuntimeError):
                self.session.stop()
            self.assertFalse(any('camera-control.pending' in str(x) for x in self.peer.events[start:]))
            setattr(self.peer, key, original)

    def test_different_serial_never_uses_saved_session(self):
        self.start()
        self.session.serial = 'other-device'
        with self.assertRaises(RuntimeError):
            self.session.status()

    def test_camera_owner_mismatch_never_writes_control(self):
        self.start()
        self.peer.camera['owner_id'] = 'other'
        with self.assertRaisesRegex(RuntimeError, 'owner changed'):
            self.session.command('stop')

    def test_active_clients_prevent_upload_and_injection(self):
        self.peer.tracking = 'FT algorithm is started\nThere is 1 clients'
        with self.assertRaisesRegex(RuntimeError, 'Stop all'):
            self.start()
        self.assertFalse(self.session.file.exists())
        self.assertFalse(any(isinstance(e, tuple) for e in self.peer.events))

    def test_missing_artifact_prevents_all_device_calls(self):
        (self.root/'libptj_source.so').unlink()
        with self.assertRaises(FileNotFoundError):
            self.start()
        self.assertEqual(self.peer.events, [])

    def test_attach_nonce_and_owner_mismatch_are_rejected(self):
        live = self.start()
        for section, key, bad in [('ipc-shim-status', 'session', 9), ('ipc-shim-status', 'client_nonce', 'wrong'),
                                  ('ipc-daemon-status', 'attach_ack', 0), ('ipc-daemon-status', 'client_birth', 0)]:
            wrong = copy.deepcopy(live)
            wrong[section][key] = bad
            with self.assertRaises(RuntimeError):
                validate_ipc(wrong, self.session.state, False)

    def test_stop_ack_retention_queue_and_nonce_are_mandatory(self):
        self.start()
        stopped = self.session.stop()
        for section, key, bad in [('ipc-shim-status', 'retained_mapping', True), ('ipc-daemon-status', 'stop_ack', 0),
                                  ('ipc-daemon-status', 'in_use', 1), ('ipc-daemon-status', 'stop_client_nonce', [9, 9])]:
            wrong = copy.deepcopy(stopped)
            wrong[section][key] = bad
            with self.assertRaises(RuntimeError):
                validate_ipc(wrong, self.session.state, True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
