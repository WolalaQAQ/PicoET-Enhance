import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from backend import make_guide_server
from visual_guide import GuideStore, VisualSequence, atomic_json, open_shared, read_json

class VisualGuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.case = self.root/'round-A'
        self.case.mkdir()
        self.now = 100.
        self.steps = [dict(action='neutral', phrase='neutral', text='Relax', hold_seconds=3,
                           visual=dict(demo_prompt_ids=['neutral'], note='')),
                      dict(action='roll', phrase='roll', text='Roll', hold_seconds=3,
                           visual=dict(demo_prompt_ids=['TongueRoll'], note=''))]
        (self.case/'actions.json').write_text(json.dumps(self.steps))
        (self.case/'round.json').write_text(json.dumps(dict(role='A')))
        self.store = GuideStore(self.root/'viewer', lambda: self.now)
        self.store.initialize(self.case)
        self.config = dict(state=str(self.root/'viewer'), viewer='headset', imitate_seconds=2.5,
                           display_timeout_seconds=1.5, heartbeat_seconds=1., hold_phrase='hold')
        self.events = []
        self.sequence = VisualSequence(self.config, self.events.append, lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def receipt(self, **changes):
        state = self.store.status()
        return self.store.receipt(dict(viewer='headset', frame=state['frame'], index=state['index'],
                                       visible=True, media_ready=True, **changes))

    def test_no_headset_receipt_no_preflight(self):
        with self.assertRaises(RuntimeError):
            self.sequence.preflight(self.case)
        self.receipt()
        self.sequence.preflight(self.case)

    def test_desktop_receipt_never_unlocks_headset(self):
        state = self.store.status()
        self.store.receipt(dict(viewer='desktop', frame=state['frame'], index=0, visible=True, media_ready=True))
        self.assertFalse(self.store.ready('headset'))

    def test_stale_hidden_or_failed_media_never_ready(self):
        for visible, media in ((False, True), (True, False)):
            state = self.store.status()
            self.store.receipt(dict(viewer='headset', frame=state['frame'], index=0, visible=visible, media_ready=media))
            self.assertFalse(self.store.ready())
        self.receipt()
        self.now += 1.
        self.assertFalse(self.store.ready())

    def test_old_frame_or_index_receipt_rejected(self):
        state = self.store.status()
        for frame, index in (('old', 0), (state['frame'], 1)):
            with self.assertRaises(ValueError):
                self.store.receipt(dict(viewer='headset', frame=frame, index=index))

    def test_show_receipt_mimic_then_caller_supplied_hold(self):
        self.assertFalse(self.sequence.tick(0))
        self.assertEqual(self.store.status()['phase'], 'display')
        self.receipt()
        self.assertFalse(self.sequence.tick(0))
        displayed = self.now
        self.assertEqual(self.store.status()['phase'], 'imitate')
        self.now += 2.49
        self.receipt()
        self.assertFalse(self.sequence.tick(0))
        self.now += .02
        self.assertTrue(self.sequence.tick(0))
        self.assertGreaterEqual(self.now-displayed, 2.5)
        until = self.now+3.
        self.sequence.hold(until)
        self.assertEqual(self.store.status()['hold_until_pc_s'], until)
        self.now = until-.01
        self.receipt()
        self.assertFalse(self.sequence.tick(0))
        self.assertEqual(self.store.status()['index'], 0)
        self.sequence.next()
        self.assertFalse(self.sequence.tick(1))
        self.assertEqual(self.store.status()['index'], 1)
        self.assertFalse(self.store.ready())
        self.assertEqual(len([e for e in self.events if e['event'] == 'visual_display_ready']), 1)

    def test_missing_display_expires_without_an_action_window(self):
        self.sequence.tick(0)
        self.now += 1.51
        with self.assertRaises(RuntimeError):
            self.sequence.tick(0)
        self.sequence.stop()
        self.assertFalse(any(e['event'] == 'visual_display_ready' for e in self.events))
        self.assertEqual(self.store.status()['phase'], 'paused')

    def test_lost_viewer_during_mimic_stops_before_hold(self):
        self.sequence.tick(0)
        self.receipt()
        self.sequence.tick(0)
        self.now += 1.01
        with self.assertRaises(RuntimeError):
            self.sequence.tick(0)
        self.assertEqual(self.store.status()['phase'], 'imitate')

    def test_runner_loss_is_not_live_status(self):
        self.store.publish(running=True, phase='hold')
        self.now += 3.01
        self.assertEqual(self.store.status()['phase'], 'interrupted')

    def test_caller_completes_sequence_with_done_state(self):
        for index in range(2):
            self.sequence.tick(index)
            self.receipt()
            self.sequence.tick(index)
            self.now += 2.51
            self.receipt()
            self.assertTrue(self.sequence.tick(index))
            self.sequence.hold(self.now+3.)
            self.assertTrue(self.store.status()['running'])
            self.sequence.next()
        self.sequence.stop(complete=True)
        self.assertEqual(self.store.status()['phase'], 'done')
        self.assertFalse(self.store.status()['running'])
        self.assertEqual(len([e for e in self.events if e['event'] == 'visual_display_ready']), 2)

    def test_viewer_initialization_cannot_overwrite_existing_session(self):
        with self.assertRaises(AssertionError):
            self.store.initialize(self.case)


class DisplayServerTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows replacement sharing semantics')
    def test_transient_Windows_replace_conflict_is_retried_not_dropped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            busy = PermissionError('Windows target temporarily in use')
            busy.winerror = 5
            real_replace = __import__('os').replace
            calls = []
            def replace(source, target):
                calls.append(target)
                if len(calls) <= 2:
                    raise busy
                return real_replace(source, target)
            with patch('visual_guide.os.replace', side_effect=replace), self.assertLogs(level='WARNING'):
                atomic_json(path, dict(revision=1))
            self.assertEqual(len(calls), 3)
            self.assertEqual(read_json(path), dict(revision=1))

    @unittest.skipUnless(os.name == 'nt', 'Windows replacement sharing semantics')
    def test_persistent_replace_conflict_is_bounded_and_remains_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            busy = PermissionError('Persistent conflict')
            busy.winerror = 5
            with patch('visual_guide.os.replace', side_effect=busy), \
                 patch('visual_guide.time.monotonic', side_effect=[100., 101.]) as clock:
                with self.assertRaises(PermissionError):
                    atomic_json(path, dict(revision=1))
            self.assertEqual(clock.call_count, 2)
            self.assertFalse(path.exists())

    def test_atomic_writer_waits_for_open_reader_then_publishes_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            atomic_json(path, dict(revision=1))
            completed = threading.Event()
            def writer():
                atomic_json(path, dict(revision=2))
                completed.set()
            with open_shared(path) as old:
                thread = threading.Thread(target=writer)
                thread.start()
                self.assertEqual(json.load(old), dict(revision=1))
            thread.join(timeout=5)
            self.assertTrue(completed.is_set())
            self.assertEqual(read_json(path), dict(revision=2))

    def test_parallel_readers_and_atomic_writers_keep_complete_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            atomic_json(path, dict(revision=0))
            errors = []
            done = threading.Event()
            def reader():
                try:
                    while not done.is_set():
                        self.assertIsInstance(read_json(path)['revision'], int)
                except BaseException as error:
                    errors.append(error)
            readers = [threading.Thread(target=reader) for _ in range(3)]
            for thread in readers:
                thread.start()
            try:
                for revision in range(100):
                    atomic_json(path, dict(revision=revision))
            finally:
                done.set()
                for thread in readers:
                    thread.join()
            self.assertEqual(errors, [])

    def test_display_server_has_no_camera_config_recording_training_or_files(self):
        with tempfile.TemporaryDirectory() as folder:
            store = GuideStore(folder)
            atomic_json(store.path, dict(frame='one', index=0, running=False, phase='practice'))
            server = make_guide_server(store, port=0)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base+'/') as response:
                    self.assertEqual(response.read(), (Path(__file__).parent/'web/index.html').read_bytes())
                with urlopen(base+'/api/status') as response:
                    self.assertTrue(json.load(response)['read_only'])
                with urlopen(base+'/static/guide-reference/TongueRoll-motion.webp') as response:
                    self.assertGreater(len(response.read()), 1000)
                for path in ('/api/recording/image', '/api/preview.jpg', '/api/training', '/static/../backend.py', '/static/guide-viewer.mjs'):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(base+path)
                    self.assertEqual(error.exception.code, 404)
                for path in ('/api/start', '/api/stop', '/api/config', '/api/recording', '/api/training'):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(Request(base+path, data=b'{}', headers={'Content-Type': 'application/json'}))
                    self.assertEqual(error.exception.code, 403)
                body = json.dumps(dict(viewer='test', frame='one', index=0, visible=True, media_ready=True)).encode()
                with urlopen(Request(base+'/api/display', data=body, headers={'Content-Type': 'application/json'})) as response:
                    self.assertEqual(response.status, 200)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base+'/api/display', data=body,
                                    headers={'Content-Type': 'application/json', 'Origin': 'http://evil.invalid'}))
                self.assertEqual(error.exception.code, 403)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
