# Native video retrieval — verified pipeline

## What changed

The installed Ollama `embeddinggemma` model was version 1, which only embedded text labels. Google’s current EmbeddingGemma 2 supports native video input. Homeboy now uses version 2 locally for video-only search.

Official reference: https://ai.google.dev/gemma/docs/embeddinggemma/multimodal-embeddinggemma-with-sentence-transformers

Model: `google/embeddinggemma-2`

Pinned revision: `914f7f89142e33e77833254d9c9b90c3cef7303b`

## Actual data path

1. The recorder saves the wrist camera stream as a LeRobot episode video, together with joint observations and actions.
2. After saving and registering the skill, the dashboard calls `POST /api/skills` with `action: index_video`.
3. The API looks up the saved episode's exact video path and time window.
4. The dedicated service on localhost:8622 decodes eight evenly spaced frames inside that window using ffmpeg.
5. It passes only these RGB pixels as `model.encode({"video": frames})`. No task label, caption, filename text, or title enters the video vector.
6. SQLite stores the normalized 768-dimensional vector, model revision, sampled timestamps, sample hash, and episode identity in `video_embeddings`. Older text vectors stay in a separate table.
7. A natural-language query is encoded by the same pinned model using `encode_query`, compared with the stored video vectors, and returns the linked recording when the demo threshold passes.

Current indexing also asks the configured local Ollama vision model (default `gemma3:4b`) for a literal caption of four sampled frames. EmbeddingGemma 2 embeds that caption separately. SQLite stores the caption text, caption embedding, EmbeddingGemma revision, and caption model in `caption_embeddings`. Search and harness retrieval score both vectors and rank each clip by the larger cosine similarity. The caption is shown in a collapsed **Caption** row below each saved or matched video. A captioning failure leaves the already-saved recording and video vector intact; indexing can be retried.

No paid inference API is used. Hugging Face is needed for the initial model download. Model inference runs locally on MPS on this Mac. The environment is separate from the robot's LeRobot environment.

## Observed results

Dataset: `homeboy/speedhacks-wrist-v1`, episode 2, 309 frames, 10.3 seconds.

Frame timestamps in seconds: 0, 1.2875, 2.575, 3.8625, 5.15, 6.4375, 7.725, 9.0125.

Sample SHA256: `5e6f60b247e2fab5639cfdb3b6212f9bf64890a4c7fa312a57c07fa456041b6c`.

| Query | Source | Cosine similarity | Result |
| --- | --- | ---: | --- |
| A white robot gripper picks up an orange block on a wooden table | Video pixels | 0.779 | Episode 2 |
| A wooden tabletop with black and white square markers | Video pixels | 0.696 | Episode 2 |
| A wooden tabletop with black and white square markers | Task label | 0.309 | Rejected |
| Wash a mug in the sink | Video pixels | 0.579 | Rejected |
| A cat sleeping on a sofa | Video pixels | 0.528 | Rejected |

The table/marker details are absent from the task label. Together with the pixel-only model input, this distinguishes video-content retrieval from title matching. Scores across versions are not directly calibrated comparisons.

After reconnecting the frontend route, its API returned `match_source: video_pixels`, `sampled_frames: 8`, the native model name, and the correct episode/video URL. The unrelated mug query returned an empty result list.

Warm episode indexing took about 4.2 seconds in the first measured API run. An automatic service restart plus first query took 10.3 seconds with weights already cached. These are individual measurements, not latency guarantees.

## Checks performed

- Native video service generated a finite 768-dimensional vector from actual episode frames.
- Frontend API accepted explicit video indexing and reported the indexed state.
- Natural-language frontend searches returned the indexed clip and rejected unrelated queries.
- The earlier check kept task-label mode separate; the strict video-only update below removes that mode entirely.
- Service auto-start succeeded after stopping only the embedding process.
- TypeScript and ESLint passed.
- No robot motion was commanded during retrieval checks.
- Dashboard interaction is left to the operator; browser rendering and a new physical teaching run are not claimed as verified.

Local machine-readable results live in `datasets/embeddinggemma2-video-checks.json`, `datasets/embeddinggemma2-frontend-checks.json`, and `datasets/embeddinggemma2-index-check.json`. Runtime data is ignored by Git.

## Limits and remaining work

- This processes recordings from the stream after saving. There is no continuous live-stream sliding-window index yet.
- Only one non-diagnostic clip exists. Collect several distinct skills to test ranking, ambiguity, and rejection properly.
- Eight sampled frames can miss short actions, particularly in long episodes. Segmenting long recordings is future work.
- Threshold 0.65 and margin 0.03 are demonstration heuristics, not calibrated probabilities or physical safety checks.
- Returning a clip does not establish task success. It does not generate joint commands, adapt motions, or prove OOD execution.
- Automatic indexing after a newly saved real episode is implemented but requires a fresh operator teaching run to verify the complete hardware-to-index path.

## Operator dashboard check

1. Refresh http://127.0.0.1:3217.
2. The existing task clip should show **VIDEO INDEXED**. Search always uses video content.
3. Search `A wooden tabletop with black and white square markers`.
4. Expect episode 2, similarity near 0.696, and **Video content · 8 sampled frames**.
5. Search `Wash a mug in the sink`; expect no close match.

These checks require no robot motion. Send the displayed result or error if the UI disagrees with the API evidence.

## Strict video-only update (2026-10-11)

- Removed Ollama label embedding requests at registration, the label search source, keyword matching, and the UI source selector. Episode registration accepts no description. Cards identify recordings by number.
- Removed robot-memory fallback to stored label vectors, catalog descriptions, and verified-experience prompt vectors. Experience evidence is still stored, but selecting a demonstration requires its indexed video pixels. Approval and task-contract checks remain in place.
- Removed description-based exclusion from retrieval: even a description beginning with `Diagnostic:` cannot alter a video's rank. Executable eligibility is still checked separately by the robot harness.
- Reindexed actual episode 2 through the live frontend API: eight decoded frames, pinned EmbeddingGemma 2 revision, source `video_pixels`.
- Query `A wooden tabletop with black and white square markers` returned episode 2 at 0.695679. Replacing its description temporarily with `Diagnostic: a cat sleeping on a sofa` produced exactly the same score and result identity. Restored the original description afterward.
- `Wash a mug in the sink` scored 0.578869 and returned no match. An explicit `source: label` request returned HTTP 400.
- All 22 hardware-fake integration and retrieval regression tests passed; TypeScript and ESLint passed. Dashboard HTML rendered video-only search without the task-label selector. Browser interaction and a fresh physical recording were not exercised in this update.
- Machine-readable live results: `datasets/video-only-retrieval-checks.json` (ignored runtime evidence).

This confirms pixel-based retrieval for the available clip; it does not establish ranking quality across a broad video collection. The current input remains saved local LeRobot recordings, not an external video-site crawler. Audio, continuous live indexing, and temporal segmentation are outside this implementation.
