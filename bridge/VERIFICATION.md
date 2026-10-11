> Historical snapshot: this records the state at the earlier test run, not a live hardware guarantee. The plan checkpoint files and current bridge status contain the latest observations.

# Local verification — 2026-10-10

The Speedhacks bridge was tested against the connected SO-101 leader and follower.

- Both configured serial ports were exclusively owned by the project recorder.
- Both arms reported six responding motors; live joint positions were available.
- Wrist snapshot returned a 640×480 image showing the physical gripper and orange block.
- Engagement reported `engaged=true`, `record_ready=true`, with about 1.1–1.4 normalized units of maximum tracking difference during the checks.
- An 82-frame diagnostic episode was saved with video.
- Two further engage → record → stop → discard cycles captured 55 frames each and returned to idle with teleop disengaged.
- The actual browser Start teaching → Stop teaching → Save demonstration flow passed and created another diagnostic episode.
- Both saved diagnostics were indexed through local EmbeddingGemma. Semantic search returned a stored episode.
- Video byte-range request returned HTTP 206 with the requested 64 bytes.
- TypeScript check passed. The app and camera were inspected in the browser; the guide is on the left and the full wrist view is visible.
- Frontend restart successfully rediscovered the project recorder using its persisted PID and verified command line.

The test exposed and fixed a conflict with a second health poller holding the arm ports and a tracking-stop trigger that was incorrectly active while teleop was disengaged. Project stop/estop behavior now releases torque without commanding a return pose.

These are brief stationary recording and engagement checks. They do not establish task completion, a full-range leader sweep, long-run reliability, or learned skill generalization. Two explicitly labeled diagnostic demonstrations remain in the local library.

Final state: recorder online on 8621, arms connected and disengaged, wrist feed active. Frontend on 3217. Ollama provides EmbeddingGemma on 11434. No other dashboard or health poller is required or running.
