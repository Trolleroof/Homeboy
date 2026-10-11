import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from skill_memory import SkillMemory, MODEL, REVISION
from task_harness import task_contract


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = SkillMemory(Path(self.tmp.name)/"skills.sqlite3")
        self.contract = task_contract("Place the orange block in the bin")
        self.skill = {"id": "episode-2", "approved": True, "sha256": "motion", "contract": self.contract, "description": "Place the orange block in the bin"}
        self.run = {"id": "run", "result": "success", "verification": {"verdict": "success", "confidence": .95, "evidence": "Block in bin"}}
        with self.memory.connect() as db:
            db.execute("CREATE TABLE skills (repo_id TEXT, episode_index INTEGER, description TEXT, embedding TEXT)")
            db.execute("CREATE TABLE video_embeddings (repo_id TEXT, episode_index INTEGER, embedding TEXT, model TEXT, revision TEXT, sample_sha256 TEXT)")
            db.execute("INSERT INTO skills VALUES (?,?,?,?)", ("homeboy/speedhacks-wrist-v1", 2, "Orange block in bin", None))
            db.execute("INSERT INTO video_embeddings VALUES (?,?,?,?,?,?)", ("homeboy/speedhacks-wrist-v1", 2, json.dumps([1., 0.]), MODEL, REVISION, "video-2"))
        self.embedding = patch("skill_memory.embed", return_value=[1., 0.])
        self.embedding.start()

    def tearDown(self):
        self.embedding.stop()
        self.tmp.cleanup()

    def test_success_is_logged_but_prompt_embedding_cannot_replace_video(self):
        learned = self.memory.learn(self.skill, {"prompt": self.skill["description"]}, self.run)
        self.assertEqual(learned["skill_id"], "episode-2")
        with self.memory.connect() as db:
            db.execute("DELETE FROM video_embeddings")
        result = self.memory.search(self.skill["description"], [self.skill], self.contract)
        self.assertEqual(result["matches"], [])
        self.assertFalse(result["safe_match"])
        self.memory.learn(self.skill, {"prompt": self.skill["description"]}, self.run)
        self.assertEqual(self.memory.status()["experiences"], 1)
        changed = {**self.skill, "sha256": "changed", "approved": False}
        self.assertEqual(self.memory.search(self.skill["description"], [changed], self.contract)["matches"], [])

    def test_experiences_are_recalled_with_indexed_video_and_current_motion(self):
        self.memory.learn(self.skill, {"prompt": self.skill["description"]}, self.run)
        result = self.memory.search(self.skill["description"], [self.skill], self.contract)
        self.assertEqual(result["matches"][0]["source"], "demonstration_video")
        self.assertEqual(result["experiences"][0]["run_id"], "run")
        result = self.memory.search(self.skill["description"], [{**self.skill, "sha256": "changed"}], self.contract)
        self.assertEqual(result["experiences"], [])

    def test_failures_uncertainty_and_invalid_evidence_do_not_learn(self):
        for verdict in ("uncertain", "failure", "blocked"):
            with self.assertRaises(ValueError):
                self.memory.learn(self.skill, {"prompt": "x"}, {**self.run, "result": verdict})
        with self.assertRaises(ValueError):
            self.memory.learn(self.skill, {"prompt": "x"}, {**self.run, "verification": {"verdict": "success", "confidence": .7, "evidence": "hidden"}})
        self.assertEqual(self.memory.status()["experiences"], 0)

    def test_existing_vector_db_is_actually_read(self):
        result = self.memory.search("Place the orange block in the bin", [self.skill], self.contract)
        self.assertEqual(result["matches"][0]["source"], "demonstration_video")
        with self.memory.connect() as db:
            db.execute("UPDATE video_embeddings SET embedding=?", (json.dumps([-1., 0.]),))
        self.assertFalse(self.memory.search(self.skill["description"], [self.skill], self.contract)["safe_match"])

    def test_finds_saved_episode_by_task_before_harness_import(self):
        found = self.memory.find_recordings("pick up the orange block and put it in the bin")
        self.assertEqual(found[0]["episode"], 2)
        self.assertEqual(found[0]["description"], "Orange block in bin")
        self.assertEqual(found[0]["score"], 1.0)

    def test_ambiguous_vector_match_is_rejected(self):
        other = {**self.skill, "id": "episode-3"}
        with self.memory.connect() as db:
            db.execute("INSERT INTO skills VALUES (?,?,?,?)", ("homeboy/speedhacks-wrist-v1", 3, "Orange block in bin", None))
            db.execute("INSERT INTO video_embeddings VALUES (?,?,?,?,?,?)", ("homeboy/speedhacks-wrist-v1", 3, json.dumps([1., 0.]), MODEL, REVISION, "video-3"))
        result = self.memory.search(self.skill["description"], [self.skill, other], self.contract)
        self.assertFalse(result["safe_match"])
        self.assertEqual(result["matches"], [])

    def test_failed_history_is_searchable_diagnostic_but_not_learned(self):
        evidence = {"id": "failed-run", "result": "blocked", "telemetry": [{"tracking_delta": 9}],
                    "events": [{"event": "blocked", "error": "Follower not tracking"}]}
        indexed = self.memory.record_run(self.skill, {"prompt": self.skill["description"], "sha256": "motion"}, evidence)
        self.assertFalse(indexed["replay_reference"])
        found = self.memory.search(self.skill["description"], [self.skill], self.contract)
        self.assertEqual(found["history"][0]["run_id"], "failed-run")
        self.assertFalse(found["history"][0]["replay_reference"])
        self.assertEqual(found["experiences"], [])
        self.assertEqual(self.memory.read_run("failed-run", self.skill)["telemetry"][0]["tracking_delta"], 9)
        with self.assertRaises(ValueError):
            self.memory.read_run("failed-run", {**self.skill, "sha256": "changed"})
        changed = self.memory.search(self.skill["description"], [{**self.skill, "sha256": "changed"}], self.contract)
        self.assertEqual(changed["history"], [])
        with self.memory.connect() as db:
            db.execute("DELETE FROM video_embeddings")
        self.assertEqual(self.memory.search(self.skill["description"], [self.skill], self.contract)["history"], [])

    def test_successful_run_history_requires_real_verification(self):
        plan = {"prompt": self.skill["description"], "sha256": "motion"}
        with self.assertRaises(ValueError):
            self.memory.record_run(self.skill, plan, {"id": "invalid", "result": "success"})
        self.memory.record_run(self.skill, plan, self.run)
        self.assertTrue(self.memory.search(self.skill["description"], [self.skill], self.contract)["history"][0]["replay_reference"])
        with self.assertRaises(ValueError):
            self.memory.record_run(self.skill, {**plan, "sha256": "different"}, self.run)

    def test_video_match_automatically_supplies_bounded_episode_context(self):
        plan = {"prompt": self.skill["description"], "sha256": "motion"}
        for index in range(5):
            evidence = {"id": f"failed-{index}", "result": "failure", "plan": {"reason": "Tracking diverged", "speed": 1.5},
                        "demonstration_video": {"video_sha256": "video-2"},
                        "telemetry": [{"at": 100+t, "elapsed_s": t/30, "tracking_delta": t} for t in range(40)],
                        "snapshots": [{"at": 101, "frame": "during.jpg"}], "events": []}
            self.memory.record_run(self.skill, plan, evidence)
        found = self.memory.search(self.skill["description"], [self.skill], self.contract)
        summary = found["matches"][0]["episode_summary"]
        self.assertEqual(summary["demonstration_video"]["video_sha256"], "video-2")
        self.assertEqual(len(summary["recent_runs"]), 3)
        recent = summary["recent_runs"][0]
        self.assertEqual(recent["run_id"], "failed-4")
        self.assertEqual(recent["plan"]["speed"], 1.5)
        self.assertEqual(recent["peak_tracking_delta"], 39)
        self.assertEqual(len(recent["telemetry_overview"]), 5)
        self.assertEqual(recent["telemetry_overview"][-1]["at"], 139)
        self.assertEqual(recent["video_binding"], "recorded")
        self.assertTrue({r["run_id"] for r in summary["recent_runs"]} <= {r["run_id"] for r in found["history"]})
        # Detailed trace remains lossless and available through the existing tool path.
        self.assertEqual(len(self.memory.read_run(recent["run_id"], self.skill)["telemetry"]), 40)

    def test_episode_context_excludes_changed_motion_video_and_ambiguous_retrieval(self):
        plan = {"prompt": self.skill["description"], "sha256": "motion"}
        self.memory.record_run(self.skill, plan, {"id": "old-video", "result": "failure",
                               "demonstration_video": {"video_sha256": "old"}})
        found = self.memory.search(self.skill["description"], [self.skill], self.contract)
        self.assertEqual(found["matches"][0]["episode_summary"]["recent_runs"], [])
        changed = self.memory.search(self.skill["description"], [{**self.skill, "sha256": "changed"}], self.contract)
        self.assertEqual(changed["matches"][0]["episode_summary"]["recent_runs"], [])
        with self.memory.connect() as db:
            db.execute("DELETE FROM video_embeddings")
        self.assertEqual(self.memory.search(self.skill["description"], [self.skill], self.contract)["matches"], [])

    def test_legacy_episode_context_marks_missing_plan_and_video_binding(self):
        self.memory.record_run(self.skill, {"prompt": self.skill["description"], "sha256": "motion"},
                               {"id": "legacy", "result": "uncertain"})
        found = self.memory.search("Place the block in the bin", [self.skill], task_contract("Place the block in the bin"))
        recent = found["matches"][0]["episode_summary"]["recent_runs"][0]
        self.assertEqual(recent["video_binding"], "legacy_motion_only")
        self.assertIsNone(recent["plan"])
        self.assertEqual(recent["telemetry_overview"], [])
        self.assertIn("legacy", {r["run_id"] for r in found["history"]})


if __name__ == "__main__":
    unittest.main()
