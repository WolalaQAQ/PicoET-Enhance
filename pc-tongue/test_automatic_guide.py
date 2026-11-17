"""Clock/frame/paint contracts for independent, automatic capture."""
import tempfile
import unittest

from face_capture import MouthGuide, WholeMouthRecording
from test_face_capture import header, JPEG_DATA


class AutomaticGuideTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.guide = MouthGuide('basics', automatic=True, clock=lambda: self.now)
        self.seq = 0

    def feed(self, *, paint=True):
        self.now += .1
        self.seq += 1
        if paint:
            self.guide.display(self.guide.frame, self.guide.phase, visible=True, media_ready=True)
        return self.guide.feed(dict(capture_session_id=8, service_generation=99,
                                    mouth_sensor_ns=self.seq*100_000_000))

    def test_no_paint_no_countdown_and_old_phase_does_not_unlock_hold(self):
        for _ in range(60):
            self.feed(paint=False)
        self.assertEqual(self.guide.phase, 'prepare')
        self.assertEqual(self.guide.status()['remaining_seconds'], 3.)
        frame = self.guide.frame
        for _ in range(40):
            self.feed()
        self.assertEqual(self.guide.phase, 'hold')
        self.assertFalse(self.guide.display(frame, 'prepare', visible=True, media_ready=True))

    def test_countdown_then_continuous_frames_without_chat_or_phase_buttons(self):
        for _ in range(85):
            self.feed()
        self.assertEqual(self.guide.phase, 'hold')
        self.assertTrue(self.guide.candidates())
        self.assertTrue(self.guide.status()['automatic'])

    def test_pause_resume_and_lost_display_reset_current_hold(self):
        for _ in range(60):
            self.feed()
        self.assertTrue(self.guide.frames)
        self.guide.advance('pause')
        for _ in range(90):
            self.feed()
        self.assertEqual(self.guide.phase, 'prepare')
        self.assertFalse(self.guide.frames)
        self.guide.advance('resume')
        for _ in range(60):
            self.feed()
        self.assertTrue(self.guide.frames)
        self.now += 2.
        self.feed(paint=False)
        self.assertFalse(self.guide.frames)

    def test_skip_moves_in_preset_order_with_a_new_display_token(self):
        frame = self.guide.frame
        self.guide.advance('skip')
        self.assertEqual(self.guide.step['id'], 'JawOpen')
        self.assertNotEqual(self.guide.frame, frame)

    def test_automatic_window_completion_preserves_unknown_targets(self):
        with tempfile.TemporaryDirectory() as folder:
            recording = WholeMouthRecording(folder)
            recording.begin('fixture', confirmed=True, preset_id='basics', automatic=True)
            recording.bind_capture(8, 99, evidence=dict(test_only=True))
            recording.guide.clock = lambda: self.now
            for seq in range(1, 90):
                self.now = seq*.1
                guide = recording.guide
                recording.display(recording.session_id, guide.frame, guide.phase,
                                  visible=True, media_ready=True)
                recording.on_frame(header(seq, ns=seq*100_000_000), JPEG_DATA)
            self.assertGreaterEqual(recording.guide.index, 1)
            self.assertTrue(all(all(value is None for value in row['targets']) for row in recording.images))
            self.assertTrue(recording.manifest['automatic_guidance'])
            recording.stop()


if __name__ == '__main__':
    unittest.main()
