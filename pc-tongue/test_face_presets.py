import json
from pathlib import Path
import tempfile
import unittest
from face_capture import MouthGuide, WholeMouthRecording
from face_presets import capture_presets, capture_preset
from face_prompts import prompt_plan
from backend import make_server
from runtime import Runtime
import threading
from urllib.request import Request, urlopen


class PresetTests(unittest.TestCase):
    def test_named_presets_have_explicit_distinct_orders_and_derived_lengths(self):
        presets = {p['id']:p for p in capture_presets()}
        self.assertEqual({k:len(p['steps']) for k,p in presets.items()},
                         dict(basics=12,tongue=11,lips=13,complete=73))
        self.assertEqual(presets['complete']['steps'],prompt_plan())
        self.assertEqual([s['id'] for s in presets['basics']['steps'][:3]],
                         ['neutral','JawOpen','MouthClose'])
        self.assertEqual([s['id'] for s in presets['tongue']['steps'][1:4]],
                         ['tongue_short','tongue_medium','tongue_long'])

    def test_guide_uses_selected_order_and_finishes_at_its_own_total(self):
        for preset in capture_presets():
            guide = MouthGuide(preset['id'])
            seen = []
            while guide.step:
                seen.append(guide.step['id'])
                guide.advance('skip')
            self.assertEqual(seen,[s['id'] for s in preset['steps']])
            self.assertEqual(guide.status()['total'],len(seen))
            self.assertEqual(guide.status()['phase'],'finished')
            self.assertEqual(guide.status()['progress'],1)

    def test_recording_freezes_preset_before_frames_and_never_generates_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = WholeMouthRecording(directory)
            status = recorder.begin('fixture',confirmed=True,preset_id='tongue')
            self.assertEqual(status['guide']['total'],11)
            manifest = json.loads((Path(status['path'])/'manifest.json').read_text())
            self.assertEqual(manifest['capture_plan'],capture_preset('tongue'))
            mutable = capture_preset('tongue')
            mutable['steps'].reverse()
            self.assertEqual(recorder.guide.steps[0]['id'],'neutral')
            self.assertEqual(recorder.samples(),[])
            self.assertEqual(status['trainable'],0)
            recorder.stop()

    def test_unknown_preset_is_rejected_before_recording_directory_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'recordings'
            with self.assertRaises(ValueError):
                WholeMouthRecording(root).begin('fixture',confirmed=True,preset_id='missing')
            self.assertFalse(root.exists())

    def test_API_lists_presets_without_capture_and_passes_selected_preset_to_real_recorder(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Runtime(Path(directory)/'config.json')
            server=make_server(runtime,port=0)
            thread=threading.Thread(target=server.serve_forever)
            thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base+'/api/presets') as response:
                    catalog=json.load(response)
                self.assertEqual(len(catalog['presets']),4)
                self.assertFalse(runtime.running)
                self.assertEqual(runtime.record_status['state'],'idle')
                # Simulated already-running input; no socket or actual device is started.
                runtime.running=runtime.connected=True
                runtime.mode='live'
                runtime._stock_receive=lambda:None
                body=json.dumps(dict(action='begin',person_id='fixture',confirmed=True,preset_id='basics')).encode()
                with urlopen(Request(base+'/api/recording',data=body,headers={'Content-Type':'application/json'})) as response:
                    result=json.load(response)
                self.assertEqual(result['guide']['preset_id'],'basics')
                self.assertEqual(result['guide']['total'],12)
            finally:
                runtime.stop()
                runtime.udp.close()
                server.shutdown()
                thread.join()
                server.server_close()


if __name__=='__main__':
    unittest.main()
