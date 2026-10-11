import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest

from PIL import Image
from video_samples import sample_frames


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
class VideoSamplingTests(unittest.TestCase):
    def test_sampling_stays_in_episode_window_and_returns_expected_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "two-episodes.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:r=30:d=1",
                "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=30:d=1",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0", "-c:v", "libx264", str(video),
            ], check=True, capture_output=True)
            for start, end, color in ((0, 1, "red"), (1, 2, "blue"), (1.2, 1.3, "blue")):
                directory = root / str(start)
                directory.mkdir()
                frames = sample_frames("ffmpeg", video, start, end, 8, directory)
                self.assertEqual(len(frames), 8)
                for frame in frames:
                    with Image.open(frame) as image:
                        r, g, b = image.convert("RGB").getpixel((20, 20))
                        self.assertGreater(r if color == "red" else b, 200)
                        self.assertLess(b if color == "red" else r, 30)

    def test_missing_video_fails_instead_of_returning_partial_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(subprocess.CalledProcessError):
                sample_frames("ffmpeg", Path(tmp)/"missing.mp4", 0, 1, 8, tmp)
