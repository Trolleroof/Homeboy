# Homeboy

Homeboy is a local-first teaching and recall app for a SO-101 robot. Record a demonstration, index the camera video with EmbeddingGemma 2, and retrieve the recording with a natural-language query. An optional prompt-to-action harness can select an approved taught motion and run it through bounded local control.

## Start the dashboard

```sh
cd frontend
npm install
npm run dev
```

Open http://127.0.0.1:3217. The recorder bridge runs locally at `127.0.0.1:8621`; the EmbeddingGemma 2 service runs at `127.0.0.1:8622` and starts when needed.

## Video retrieval

Saved clips are indexed from eight evenly spaced frames in each episode's video window. EmbeddingGemma 2 encodes those pixels directly. Search compares the text query embedding with stored video vectors. Titles and descriptions do not affect retrieval scores. The video-indexing service needs Python 3.12, `uv`, and `ffmpeg`:

```sh
uv venv --python 3.12 .embedding-venv
uv pip install --python .embedding-venv/bin/python -r bridge/embedding-requirements.txt
```

The pinned model is downloaded from Hugging Face on first use. See [video retrieval verification](bridge/VIDEO_RETRIEVAL_VERIFICATION.md) for the current evidence and known limits.

## Robot setup and limits

See [the frontend setup guide](frontend/README.md) and [the robot harness guide](bridge/HARNESS.md) before connecting hardware. Physical execution requires operator approval, a clear workcell, and the original taught layout. The current harness replays bounded taught trajectories; it does not adapt a grasp to a moved object or a new environment.

Local configuration belongs in ignored `.env.local` files; start from the checked-in `.env.example` templates. Runtime datasets, model weights, logs, test artifacts, and progress checkpoints stay out of Git.
