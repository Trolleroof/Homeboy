"""Video recall must be independent of all label and prompt embeddings."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from skill_memory import MODEL, REVISION, SkillMemory

class VideoOnlyRecall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = SkillMemory(Path(self.tmp.name) / "skills.sqlite3")
        self.vector = [1.0] + [0.0] * 767
        self.contract = {"object": "block"}
        self.skill = {"id": "episode-2", "approved": True, "contract": self.contract,
                      "description": "a cat sleeping on a sofa", "sha256": "motion"}
        with self.memory.connect() as db:
            db.execute("CREATE TABLE skills (repo_id TEXT, episode_index INTEGER, description TEXT, embedding TEXT)")
            db.execute("CREATE TABLE video_embeddings (repo_id TEXT, episode_index INTEGER, embedding TEXT, model TEXT, revision TEXT)")
            db.execute("INSERT INTO skills VALUES (?,?,?,?)", ("willow/speedhacks-wrist-v1", 2, self.skill["description"], json.dumps(self.vector)))

    def search(self):
        with patch("skill_memory.embed", return_value=self.vector) as encode:
            result = self.memory.search("gripper picking up an orange block", [self.skill], self.contract)
            encode.assert_called_once_with("gripper picking up an orange block")
            return result

    def index(self, revision=REVISION):
        with self.memory.connect() as db:
            db.execute("INSERT INTO video_embeddings VALUES (?,?,?,?,?)", ("willow/speedhacks-wrist-v1", 2, json.dumps(self.vector), MODEL, revision))

    def test_labels_catalog_and_experience_cannot_replace_unindexed_video(self):
        with self.memory.connect() as db:
            db.execute("INSERT INTO robot_experiences VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                "experience", "episode-2", "motion", "gripper picking up an orange block",
                json.dumps(self.contract), json.dumps(self.vector), MODEL, REVISION, "{}", "run", 0))
        self.assertFalse(self.search()["safe_match"])
        self.assertEqual(self.search()["matches"], [])

    def test_description_changes_do_not_change_video_recall(self):
        self.index()
        before = self.search()
        with self.memory.connect() as db:
            db.execute("UPDATE skills SET description='Diagnostic: misleading title', embedding=NULL")
        self.skill["description"] = "unrelated description"
        self.assertEqual(before, self.search())
        self.assertEqual(before["matches"][0]["source"], "demonstration_video")

    def test_incompatible_video_revision_is_excluded(self):
        self.index("old-revision")
        self.assertFalse(self.search()["safe_match"])

    def test_approval_and_contract_still_gate_motion_recall(self):
        self.index()
        self.skill["approved"] = False
        self.assertFalse(self.search()["safe_match"])
        self.skill["approved"] = True
        self.skill["contract"] = {"object": "mug"}
        self.assertFalse(self.search()["safe_match"])

if __name__ == "__main__":
    unittest.main()
