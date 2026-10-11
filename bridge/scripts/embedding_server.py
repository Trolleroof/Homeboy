"""Native EmbeddingGemma 2 text/video embeddings; never controls robot hardware."""
import hashlib
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL_ID = "google/embeddinggemma-2"
REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"
DATASET_ROOT = Path(os.environ.get("HOMEBOY_DATASET_ROOT", "~/.cache/huggingface/lerobot")).expanduser().resolve()
app = FastAPI(title="Homeboy native video embeddings")
lock = threading.Lock()
model = None
device = None


def load_model():
    global model, device
    if model is None:
        import torch
        from sentence_transformers import SentenceTransformer
        device = os.environ.get("HOMEBOY_EMBED_DEVICE") or ("mps" if torch.backends.mps.is_available() else "cpu")
        model = SentenceTransformer(
            MODEL_ID, revision=REVISION, device=device,
            config_kwargs={"audio_config": None},
            model_kwargs={"dtype": torch.float32, "attn_implementation": "eager"},
        )
    return model


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class VideoInput(BaseModel):
    repo_id: str
    video_path: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    max_frames: int = Field(default=8, ge=2, le=16)


@app.get("/health")
def health():
    return {"model": MODEL_ID, "revision": REVISION, "loaded": model is not None, "device": device, "capabilities": ["text", "video"]}


@app.post("/embed/text")
def embed_text(body: TextInput):
    try:
        with lock:
            started = time.monotonic()
            vector = load_model().encode_query(body.text, normalize_embeddings=True)
            return {"embedding": vector.tolist(), "model": MODEL_ID, "revision": REVISION, "elapsed_s": time.monotonic() - started}
    except Exception as error:
        logging.exception("Text embedding failed")
        raise HTTPException(503, f"EmbeddingGemma 2 unavailable: {error}") from error


@app.post("/embed/video")
def embed_video(body: VideoInput):
    from PIL import Image
    root = (DATASET_ROOT / body.repo_id).resolve()
    video = (root / body.video_path).resolve()
    if not root.is_relative_to(DATASET_ROOT) or root == DATASET_ROOT or not video.is_relative_to(root):
        raise HTTPException(400, "Video path must be inside its dataset")
    if not video.is_file():
        raise HTTPException(404, "Recorded video not found")
    duration = body.end - body.start
    if not math.isfinite(duration) or duration <= 0 or duration > 300:
        raise HTTPException(400, "Clip must span 0–300 seconds")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(503, "ffmpeg is required to decode recorded frames")
    # Sample only this episode's window, even when LeRobot concatenates clips.
    timestamps = [body.start + duration * i / body.max_frames for i in range(body.max_frames)]
    try:
        with lock, tempfile.TemporaryDirectory(prefix="homeboy-native-video-") as tmp:
            started = time.monotonic()
            frames = []
            digest = hashlib.sha256()
            for index, timestamp in enumerate(timestamps):
                filename = Path(tmp) / f"{index:02}.jpg"
                subprocess.run([
                    ffmpeg, "-v", "error", "-y", "-ss", str(timestamp), "-i", str(video),
                    "-frames:v", "1", "-vf", "scale=480:-2", str(filename),
                ], check=True, timeout=20, capture_output=True)
                digest.update(filename.read_bytes())
                with Image.open(filename) as image:
                    frames.append(image.convert("RGB").copy())
            # Native video modality consumes pixels directly. No task label,
            # caption, filename, or episode title is passed to the model.
            vector = load_model().encode(
                {"video": frames}, prompt="", normalize_embeddings=True,
                processing_kwargs={"video": {"max_frames": body.max_frames, "fps": None, "add_timestamps": False}},
            )
            return {
                "embedding": vector.tolist(), "model": MODEL_ID, "revision": REVISION,
                "source": "video_pixels", "frame_times": timestamps,
                "sample_sha256": digest.hexdigest(), "elapsed_s": time.monotonic() - started,
            }
    except Exception as error:
        logging.exception("Native video embedding failed")
        raise HTTPException(503, f"Video embedding failed: {error}") from error


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8622)
