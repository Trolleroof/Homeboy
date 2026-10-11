# Homeboy

Run `npm install` and `npm run dev` in this folder, then open http://127.0.0.1:3217.

This Next.js application owns its bridge at `/api/bridge`. It starts the project recorder in `../bridge/scripts/record_server.py` on localhost:8621. The SUDS dashboard on port 3117 is no longer used. Stop other robot controllers and health pollers before connecting.

The Python interpreter defaults to `/Users/nikhi/SUDS/.venv/bin/python`, reusing the installed LeRobot dependencies and existing leader/follower calibration files. Set `WILLOW_PYTHON` in `.env.local` to use another compatible environment. The controller source and hardware configuration belong to this project under `bridge/`.

## Teaching

1. Plug in and power both arms. Align their poses and clear the follower's path.
2. Click **Start teaching**. Willow starts the recorder if necessary, checks live joint readings and the wrist camera, aligns the follower to the leader over two seconds, and waits for recording to produce frames.
3. Move the leader through the task. Click **Stop teaching**, then save or discard. Stopping releases torque at the current pose; support the arm as needed. The recorder automatically saves pending takes after 120 seconds.
4. Saving automatically indexes the recorded video. Describe what happened or appeared in the clip to retrieve it. Existing clips have an **Index video** button. If indexing fails, the recording remains saved and you can retry.

The emergency STOP cuts torque without commanding a return pose. After a stop, inspect the workspace and use **Reset stop** before starting again. Tracking errors and stale joint reads prevent recording/readiness.

## Storage and search

- Leader/follower serial ports: `bridge/config/arms.json`.
- Camera mapping: `bridge/config/cameras.json`. This project currently records only `wrist=0`.
- Episodes, joint actions, and H.264 video: `~/.cache/huggingface/lerobot/willow/speedhacks-wrist-v1`.
- Video vectors and optional display metadata: `datasets/willow_skills.sqlite3`.
- Recorder log: `bridge/logs/recorder.log`.
- Native video embedding service: localhost:8622, auto-started on the first indexing/search request. Its Python environment is separate from LeRobot.
- Set up that environment from the project root: `uv venv --python 3.12 .embedding-venv`, then `uv pip install --python .embedding-venv/bin/python -r bridge/embedding-requirements.txt`.
- The first request downloads the pinned `google/embeddinggemma-2` model from Hugging Face; it needs network access and several GB of disk space. Subsequent runs use the local cache. No hosted inference API key is needed.
- Each saved episode contributes eight evenly spaced wrist-camera frames from its exact clip window. EmbeddingGemma 2 embeds these pixels directly with its native video input. No title or generated caption is included in the video vector. Audio is disabled.
- Natural-language queries are embedded by the same version of EmbeddingGemma 2, then compared against stored video vectors in SQLite. Results return the linked recording. Descriptions are optional when registering an episode; registration makes no text embedding request. Cards use recording numbers instead of titles.
- Retrieval uses only native video vectors. Titles, task descriptions, catalog descriptions, and stored prompt embeddings never determine matches. Legacy label vectors remain on disk for compatibility but are not searched. The robot harness uses the same video-only recall rule, with approval and task-contract checks still required.
- The video threshold (0.65) and top-two margin (0.03) are demo heuristics; they need calibration with more clips. Scores indicate similarity, not successful execution or robot safety.
- This indexes saved recordings from the camera stream. Continuous live-stream sliding-window indexing is not implemented. Eight samples can miss brief actions in long episodes.
- Verification evidence: `../bridge/VIDEO_RETRIEVAL_VERIFICATION.md`.

The search panel retrieves saved demonstrations. The new **Prompt robot** panel separately imports and approves a taught motion, retrieves its indexed video through Codex tools, builds and executes a bounded program through the bridge, and saves successful visual outcomes as searchable experience memory. It requires the original fixed layout and cannot adapt to moved objects. Setup, control limits and current verification evidence are in [the harness guide](../bridge/HARNESS.md).

The [Dial SMS adapter](../bridge/DIAL_SMS.md) routes authorized phone requests into the same harness, sends expiring confirmation tokens, and reports execution outcomes. SMS consent is separate from the local operator's workcell check. Dial signup supports the event coupon `DIAL_FOR_SPEEDHACKS`; account access, redemption and live carrier delivery require separate verification.
