# Save and indexing performance

The dashboard's Stop-to-auto-save window is six seconds instead of 120 seconds. Explicit Save commits immediately; Toss remains available during the pending window. Library registration waits until the recorder reports a completed save, rather than reacting to a metadata counter updated before the writer finishes.

Episode recording uses streaming H.264 with the `ultrafast` preset. This reduces encoding CPU work and end-of-episode backlog at the cost of larger video files at the same CRF. The recorder still finishes the video and closes/reopens the dataset so videos and Parquet metadata are readable before reporting the episode saved.

Dataset reopening reads local episode-reference columns directly through Arrow, avoiding the Hugging Face metadata cache/build pass and image-stat columns on each save. It unions camera metadata schemas so older wrist-only recordings can coexist with new wrist/overhead recordings. The first write of a newly introduced camera starts its own video history rather than looking for its path in the last wrist-only episode. Old recordings are not rewritten.

`EPISODE_SAVE_TIMING` in the recorder log separates save/encoding time from dataset reopen time.

Video and caption indexing decode sampled images with one FFmpeg process per clip instead of one process per image. Embedding inference remains serialized; frame decoding and Ollama caption generation do not hold the embedding lock. Camera indexing and video/caption requests overlap, and all camera requests settle before the shared SQLite connection closes. The UI identifies indexing as background work; the saved take remains available while indexing finishes.

## Local verification, October 11, 2026

Synthetic 10-second, 640x480, 30 fps clip on this Mac:

| Component | Before | After |
|---|---:|---:|
| H.264 encoding, medium versus ultrafast | 0.363 s | 0.087 s |
| Decode eight sample frames | 0.322 s | 0.042 s |

Reading the existing local episode-reference metadata took 0.0245–0.0261 seconds through the warmed previous loader and 0.0050–0.0053 seconds through direct Arrow reads (three calls each, with first-call timings of 0.1663 and 0.0251 seconds respectively).

These are component timings, not measured full robot-save or model-inference speedups. Two consecutive synthetic episodes with wrist and overhead cameras were also saved and reopened through the actual LeRobot writer; all 60 data rows and episode metadata remained readable. Save/reopen timings were 0.154/0.176 seconds and 0.092/0.073 seconds for those small 64x64 test clips.

Sampling tests cover episode-window boundaries, short clips, output count, and missing-video failure. Single-pass sampling may choose a different adjacent source frame than the old independent seeks; freshly indexed sample hashes reflect the newly sampled pixels.

The recorder and embedding service have been restarted with the Python changes. Normal existing recordings are not re-encoded. A regression test saves an initial wrist-only episode followed by two wrist/overhead episodes and checks that all rows remain readable and the original data/video files are unchanged. The new-camera fix prevents the observed overhead metadata error on future saves.

## Stop and save failure recovery

Stop stays enabled while Start waits for recorded frames, and aborts that wait so it cannot overwrite the stop result. During engaged teleop, the UI uses the controller's `record_ready` flag instead of requiring an instantaneous leader/follower pose match. Recording errors and timeouts report the actual stage rather than a generic cable/connection error. Stop and emergency-stop requests bypass the frontend calibration lease; new recording requests still respect it. The bridge proxy gives ordinary write commands 35 seconds to outlast the recorder's 30-second command wait. Repeated Stop on an already stopped recorder succeeds.

Empty and failed saves return `ok=false` to the HTTP caller. Failed writers refuse new recording until restarted. The UI clears stale “Saving…” notices on failure, registers a take only after durable completion, and resets its episode-count baseline after a writer restart or library deletion.

The running old writer was idle with zero recorded frames but had stale episode counters and a poisoned buffer. Dataset snapshots and unreferenced failed-save files were preserved under `datasets/recovery/20261011T223235Z-save-repair/`. The recorder was restarted with the fixes and checked through both its direct API and the dashboard proxy. Both arms reported six responding motors, both cameras streamed, and Stop returned success while idle. A new operator-recorded physical take has not yet been tested.

A complete earlier failed take was recovered offline from those preserved files: 287 frames (9.57 seconds), both cameras, and the task “move the green block to the orange bowl.” It is installed as episode 1 (Take 2) and registered in the library. The restored motor actions and measured states match the source rows exactly; both camera videos contain 287 frames and were re-encoded through the repaired writer. The original valid episode and failed-save source files remain preserved. Recovery provenance is in `recovered-episode.json` in the backup directory. This verifies persistence of a real captured recording; it does not establish task success or approve autonomous execution. The later zero-frame attempt has no recording to recover.

Both recovered camera videos were successfully indexed through the restarted embedding service using eight video samples and four caption samples each. The dashboard video endpoint served both clips with HTTP 206 range responses. Captions are model-generated descriptions and are not evidence of task success.

Regression checks: `python -m unittest discover -s bridge/scripts -p 'test_record_server*.py'` with the LeRobot environment, `node scripts/test-recorder-controls.cjs` from `frontend/`, and `npx tsc --noEmit`.

## Run feedback and endpoint handling

“Run it” is bounded joint replay, with live telemetry used for starting alignment, tracking, stale-reading checks, and controller deadlines. It does not adapt the demonstrated path to a changed scene. The dashboard now shows request preparation, execution time, evidence finalization, selected demonstration, measured joints, command targets, tracking error, and differences from the demonstration's start pose. Duplicate submissions stay disabled until the accepted run has finished; a blocked result remains visible when evidence indexing finishes.

The observed blocked run had a shoulder-lift encoder reading of -101.14 normalized units while recorded targets had already been clamped to -100. Feedback validation now allows at most two units of endpoint overshoot; recorded targets remain strictly within their original ranges. An initial overshoot is retained as the initial holding pose and can only return inward at the existing speed/command-step caps, avoiding a sudden clamp jump. Larger excursions, nonfinite/incomplete readings, and start-pose differences over five units still block. The local Waddle adapter was inspected for its calibrated-limit and command-step approach; its radian limits are not substituted for this recorder's normalized units.

Three feedback regression tests, seven focused harness/controller tests, fourteen recorder tests, TypeScript checking, and diff whitespace checking passed. The updated bridge was restarted while idle and disengaged, and both cameras and all six follower motors were available afterward. No physical replay was initiated during validation.
