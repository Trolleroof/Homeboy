"""Decode an episode's sampled images in one FFmpeg pass."""
from pathlib import Path
import subprocess


def sample_frames(ffmpeg, video, start, end, count, directory):
    directory = Path(directory)
    # Trim the input to this episode; the fps filter samples across that window.
    subprocess.run([
        ffmpeg, "-v", "error", "-y", "-ss", str(start), "-t", str(end-start), "-i", str(video),
        "-vf", f"fps={count/(end-start)}:start_time=0:round=up,scale=480:-2",
        "-frames:v", str(count), "-fps_mode", "passthrough", "-start_number", "0",
        str(directory / "%02d.jpg"),
    ], check=True, timeout=60, capture_output=True)
    frames = [directory / f"{i:02}.jpg" for i in range(count)]
    if not all(frame.is_file() for frame in frames):
        raise ValueError("Recorded clip did not contain all requested sample frames")
    return frames
