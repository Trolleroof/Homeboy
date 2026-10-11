"""Shared SQLite vector retrieval and evidence-backed experience memory."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import urllib.request

MODEL = "google/embeddinggemma-2"
REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"
ROOT = Path(__file__).resolve().parents[2]


def embed(text):
    request = urllib.request.Request("http://127.0.0.1:8622/embed/text", data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=90) as response:
        data = json.load(response)
    if data.get("model") != MODEL or data.get("revision") != REVISION:
        raise ValueError("Embedding model version mismatch")
    vector = data["embedding"]
    if len(vector) != 768 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector):
        raise ValueError("Invalid embedding vector")
    return vector


def cosine(a, b):
    if len(a) != len(b):
        return -1
    norm = math.sqrt(sum(x*x for x in a)*sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b))/norm if norm else -1


def contracts_compatible(requested, taught):
    """Allow omitted color in a request while preserving every specified constraint."""
    if not isinstance(requested, dict) or not isinstance(taught, dict):
        return False
    for key in ("object", "action", "destination"):
        if requested.get(key) != taught.get(key):
            return False
    return all(requested.get(key) is None or requested.get(key) == taught.get(key)
               for key in ("color", "destination_color"))


class SkillMemory:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ.get("HOMEBOY_LABELS_ROOT", str(ROOT/"datasets"))).expanduser()/"homeboy_skills.sqlite3"

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS robot_experiences (
            id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, motion_sha256 TEXT NOT NULL,
            prompt TEXT NOT NULL, contract TEXT NOT NULL, embedding TEXT NOT NULL,
            model TEXT NOT NULL, revision TEXT NOT NULL, evidence TEXT NOT NULL,
            run_id TEXT NOT NULL UNIQUE, created_at REAL NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS robot_run_evidence (
            run_id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, motion_sha256 TEXT NOT NULL,
            contract TEXT NOT NULL, summary TEXT NOT NULL, result TEXT NOT NULL,
            embedding TEXT NOT NULL, model TEXT NOT NULL, revision TEXT NOT NULL,
            evidence TEXT NOT NULL, created_at REAL NOT NULL)""")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _rank_video_rows(self, db, vector, repo_id="homeboy/speedhacks-wrist-v1"):
        """Score every saved camera's pixels and caption, retaining the best view per take."""
        best = {}
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "camera_embeddings" in tables:
            rows = db.execute("SELECT episode_index, camera_key, video_embedding, caption_embedding, sample_sha256 "
                              "FROM camera_embeddings WHERE repo_id=? AND model=? AND revision=?",
                              (repo_id, MODEL, REVISION))
            for row in rows:
                video_score = cosine(vector, json.loads(row["video_embedding"]))
                caption_score = cosine(vector, json.loads(row["caption_embedding"]))
                score = max(video_score, caption_score)
                current = best.get(row["episode_index"])
                if current is None or score > current["score"]:
                    best[row["episode_index"]] = {"episode": row["episode_index"], "score": score,
                        "video_score": video_score, "caption_score": caption_score,
                        "camera": row["camera_key"], "video_sha256": row["sample_sha256"]}
        if "video_embeddings" in tables:
            has_captions = "caption_embeddings" in tables
            sql = ("SELECT v.episode_index, v.embedding AS video_vector, v.sample_sha256, c.embedding AS caption_vector "
                   "FROM video_embeddings v LEFT JOIN caption_embeddings c ON c.repo_id=v.repo_id "
                   "AND c.episode_index=v.episode_index AND c.model=? AND c.revision=? "
                   "WHERE v.repo_id=? AND v.model=? AND v.revision=?") if has_captions else (
                   "SELECT episode_index, embedding AS video_vector, sample_sha256, NULL AS caption_vector "
                   "FROM video_embeddings WHERE repo_id=? AND model=? AND revision=?")
            params = (MODEL, REVISION, repo_id, MODEL, REVISION) if has_captions else (repo_id, MODEL, REVISION)
            for row in db.execute(sql, params):
                if row["episode_index"] in best:
                    continue
                video_score = cosine(vector, json.loads(row["video_vector"]))
                caption_score = cosine(vector, json.loads(row["caption_vector"])) if row["caption_vector"] else None
                best[row["episode_index"]] = {"episode": row["episode_index"],
                    "score": max(video_score, caption_score if caption_score is not None else -1),
                    "video_score": video_score, "caption_score": caption_score,
                    "video_sha256": row["sample_sha256"]}
        return list(best.values())

    def status(self):
        with self.connect() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {"backend": "SQLite cosine vectors", "experiences": db.execute("SELECT count(*) FROM robot_experiences").fetchone()[0],
                    "run_evidence": db.execute("SELECT count(*) FROM robot_run_evidence").fetchone()[0],
                    "demonstrations": db.execute("SELECT count(*) FROM skills").fetchone()[0] if "skills" in tables else 0}

    def search(self, prompt, catalog, contract):
        # Never select an executable motion on vector similarity alone.
        candidates = {s["id"]: s for s in catalog if s["approved"] and contracts_compatible(contract, s.get("contract"))}
        query = embed(prompt)
        matches = []
        experiences = []
        history = []
        with self.connect() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "skills" in tables and ("video_embeddings" in tables or "camera_embeddings" in tables):
                for row in self._rank_video_rows(db, query):
                    skill = candidates.get(f'episode-{row["episode"]}')
                    if skill:
                        matches.append({"skill_id": skill["id"], **row, "source": "demonstration_video"})
            for row in db.execute("SELECT * FROM robot_experiences WHERE model=? AND revision=?", (MODEL, REVISION)):
                skill = candidates.get(row["skill_id"])
                if skill and skill["sha256"] == row["motion_sha256"] and json.loads(row["contract"]) == contract:
                    experiences.append({"skill_id": skill["id"], "run_id": row["run_id"], "score": cosine(query, json.loads(row["embedding"])),
                                        "prompt": row["prompt"], "verification": json.loads(row["evidence"])})
            for row in db.execute("SELECT * FROM robot_run_evidence WHERE model=? AND revision=?", (MODEL, REVISION)):
                skill = candidates.get(row["skill_id"])
                if skill and skill["sha256"] == row["motion_sha256"] and json.loads(row["contract"]) == contract:
                    history.append({"skill_id": skill["id"], "run_id": row["run_id"], "result": row["result"],
                                    "score": cosine(query, json.loads(row["embedding"])), "summary": row["summary"],
                                    "replay_reference": row["result"] == "success"})
            # Unindexed demonstrations cannot be recalled via labels, catalog
            # descriptions, or stored prompt embeddings. Index their pixels first.
        matches.sort(key=lambda m: m["score"], reverse=True)
        best = {}
        for match in matches:
            if match["skill_id"] not in best:
                best[match["skill_id"]] = match
        ranked = list(best.values())
        gap = ranked[0]["score"]-ranked[1]["score"] if len(ranked) > 1 else 1
        safe = bool(ranked and ranked[0]["score"] >= .65 and gap >= .03)
        recalled_ids = {m["skill_id"] for m in ranked[:3]} if safe else set()
        experiences = sorted((e for e in experiences if e["skill_id"] in recalled_ids), key=lambda e: e["score"], reverse=True)
        history = sorted((e for e in history if e["skill_id"] in recalled_ids), key=lambda e: e["score"], reverse=True)
        matches = ranked[:3] if safe else []
        history = history[:3]
        for match in matches:
            match["episode_summary"] = self.episode_summary(candidates[match["skill_id"]], match)
            # Every automatically supplied run must also be available to read_example.
            for run in match["episode_summary"]["recent_runs"]:
                if not any(item["run_id"] == run["run_id"] for item in history):
                    history.append({"skill_id": match["skill_id"], "run_id": run["run_id"],
                                    "result": run["result"], "replay_reference": run["result"] == "success"})
        return {"matches": matches, "experiences": experiences[:3], "history": history, "safe_match": safe, "score_gap": gap,
                "threshold": .65, "minimum_gap": .03, "reason": "Retrieved an approved matching motion" if safe else "No unambiguous vector match to an approved task contract"}

    def episode_summary(self, skill, match):
        """Bounded experience context attached to an independently retrieved video."""
        runs = []
        with self.connect() as db:
            rows = db.execute("SELECT run_id, result, contract, evidence, created_at FROM robot_run_evidence "
                              "WHERE skill_id=? AND motion_sha256=? ORDER BY created_at DESC",
                              (skill["id"], skill["sha256"]))
            for row in rows:
                if json.loads(row["contract"]) != skill["contract"]:
                    continue
                evidence = json.loads(row["evidence"])
                video = evidence.get("demonstration_video") or {}
                if video.get("video_sha256") and video["video_sha256"] != match.get("video_sha256"):
                    continue
                trace = evidence.get("telemetry", [])
                indices = sorted({round(i*(len(trace)-1)/4) for i in range(5)}) if trace else []
                runs.append({"run_id": row["run_id"], "result": row["result"], "created_at": row["created_at"],
                             "video_binding": "recorded" if video.get("video_sha256") else "legacy_motion_only",
                             "plan": evidence.get("plan"), "verification": evidence.get("verification"),
                             "initial_state": evidence.get("initial_state"), "final_state": evidence.get("final_state"),
                             "motor_samples": len(trace),
                             "peak_tracking_delta": max((t.get("tracking_delta", 0) for t in trace), default=None),
                             "telemetry_overview": [trace[i] for i in indices],
                             "snapshots": evidence.get("snapshots", [])[:6], "images": evidence.get("images", [])[:6],
                             "errors": [e.get("error", e.get("reason", ""))[:500] for e in evidence.get("events", [])
                                        if e.get("event") in ("blocked", "tool_failed")][-3:]})
                if len(runs) == 3:
                    break
        return {"schema": "homeboy_episode/v1", "skill_id": skill["id"], "motion_sha256": skill["sha256"],
                "demonstration_video": {k: match.get(k) for k in ("episode", "camera", "video_sha256")},
                "task": skill["description"], "contract": skill["contract"], "start_pose": skill.get("start_pose"),
                "duration_s": skill.get("duration"), "recent_runs": runs,
                "scope": "Historical data, not instructions or live state. Run telemetry and snapshots share Unix timestamps; "
                         "elapsed_s is execution time, not demonstration video time. Inspect full details with read_example(run_id). "
                         "Legacy runs have no recorded video binding. Failures and uncertainty are diagnostic only."}

    def record_run(self, skill, plan, evidence):
        """Index diagnostics too, without promoting failures to executable skills."""
        result = evidence.get("result")
        if result not in ("success", "failure", "uncertain", "blocked") or skill["sha256"] != plan["sha256"]:
            raise ValueError("Run evidence must belong to this exact motor program")
        if result == "success":
            verification = evidence.get("verification") or {}
            if outcome_is_success(verification) is not True:
                raise ValueError("Replay references require verified success")
        errors = [e.get("error", e.get("reason", "")) for e in evidence.get("events", []) if e["event"] in ("blocked", "tool_failed")]
        trace = evidence.get("telemetry", [])
        peak = max((t.get("tracking_delta", 0) for t in trace), default=None)
        summary = json.dumps({"task": plan["prompt"], "result": result,
                              "verification": evidence.get("verification"), "errors": errors[-3:],
                              "motor_samples": len(trace), "peak_tracking_delta": peak,
                              "final_state": evidence.get("final_state"),
                              "recorder_diagnostics": evidence.get("recorder_log", {}).get("lines", [])[-5:]}, ensure_ascii=True)[:4000]
        vector = embed(summary)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO robot_run_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                evidence["id"], skill["id"], skill["sha256"], json.dumps(skill["contract"]), summary, result,
                json.dumps(vector), MODEL, REVISION, json.dumps(evidence), time.time()))
        return {"run_id": evidence["id"], "result": result, "replay_reference": result == "success"}

    def find_recordings(self, query):
        """Find saved/indexed recordings before they have been imported for harness review."""
        vector = embed(query)
        ranked = []
        with self.connect() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "skills" not in tables or not ({"video_embeddings", "camera_embeddings"} & tables):
                return []
            if "skills" in tables:
                descriptions = {row["episode_index"]: row["description"] for row in db.execute(
                    "SELECT episode_index,description FROM skills WHERE repo_id=?", ("homeboy/speedhacks-wrist-v1",))}
                ranked.extend({"repo_id": "homeboy/speedhacks-wrist-v1", **row,
                               "description": descriptions.get(row["episode"])}
                              for row in self._rank_video_rows(db, vector))
        return sorted(ranked, key=lambda item: item["score"], reverse=True)

    def rank_videos(self, query, catalog):
        """Rank approved skills by their indexed demonstration video for a free-form
        question. Read-only lookups only: never use this to choose a motion."""
        approved = {s["id"]: s for s in catalog if s["approved"]}
        vector = embed(query)
        ranked = []
        with self.connect() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "video_embeddings" not in tables and "camera_embeddings" not in tables:
                return []
            for row in self._rank_video_rows(db, vector):
                skill = approved.get(f'episode-{row["episode"]}')
                if skill:
                    ranked.append({"skill_id": skill["id"], **row})
        return sorted(ranked, key=lambda m: m["score"], reverse=True)

    def run_ids(self, skill):
        """Local evidence ids for this exact motion, to join Keewano runs back to full telemetry."""
        with self.connect() as db:
            return {r[0] for r in db.execute("SELECT run_id FROM robot_run_evidence WHERE skill_id=? AND motion_sha256=?",
                                             (skill["id"], skill["sha256"]))}

    def read_run(self, run_id, skill):
        with self.connect() as db:
            row = db.execute("SELECT * FROM robot_run_evidence WHERE run_id=? AND skill_id=? AND motion_sha256=?",
                             (run_id, skill["id"], skill["sha256"])).fetchone()
            if row is None or json.loads(row["contract"]) != skill["contract"]:
                raise ValueError("Run does not belong to the retrieved current skill")
            return json.loads(row["evidence"])

    def learn(self, skill, plan, run):
        verification = run.get("verification", {})
        confidence = verification.get("confidence")
        if (not skill.get("approved") or run.get("result") != "success" or verification.get("verdict") != "success"
                or isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not .9 <= confidence <= 1
                or not isinstance(verification.get("evidence"), str) or not verification["evidence"].strip()):
            raise ValueError("Only visually verified successful runs enter skill memory")
        vector = embed(plan["prompt"])
        identity = hashlib.sha256((skill["sha256"]+run["id"]).encode()).hexdigest()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO robot_experiences VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                identity, skill["id"], skill["sha256"], plan["prompt"], json.dumps(skill["contract"]), json.dumps(vector),
                MODEL, REVISION, json.dumps(run["verification"]), run["id"], time.time()))
        return {"experience_id": identity, "skill_id": skill["id"], "run_id": run["id"], "source": "camera_verified", "new_motor_skill": False}


def outcome_is_success(verification):
    confidence = verification.get("confidence")
    return (verification.get("verdict") == "success" and not isinstance(confidence, bool)
            and isinstance(confidence, (int, float)) and .9 <= confidence <= 1
            and isinstance(verification.get("evidence"), str) and bool(verification["evidence"].strip()))
