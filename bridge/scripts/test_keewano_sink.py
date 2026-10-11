"""Keewano sink checks: no network, hardware or model calls."""
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import uuid

import keewano_sink
from keewano_sink import EVENTS_HASH, KeewanoSink, session_id, to_keewano

RUN = uuid.uuid4().hex


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class FakeKeewano:
    def __init__(self, fail_posts=0):
        self.calls, self.fail_posts = [], fail_posts

    def __call__(self, req, timeout=0):
        body = json.loads(req.data)
        path = req.full_url.rsplit("/", 1)[1]
        assert req.get_header("K-token") == "key"
        self.calls.append((path, body))
        if path == "in" and self.fail_posts:
            self.fail_posts -= 1
            raise urllib.error.URLError("down")
        if path == "custom":
            return FakeResponse(200, {"status": "registered"})
        return FakeResponse(202, {"received": len(body["events"]), "accepted": len(body["events"])})


class KeewanoSinkTests(unittest.TestCase):
    def sink(self, **kw):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.fake = kw.pop("fake", FakeKeewano())
        return KeewanoSink(api_key=kw.pop("api_key", "key"), opener=self.fake, spool=Path(self.dir.name)/"failed.jsonl",
                           backoff=0, load_env=False, **kw)

    def test_event_mapping(self):
        events = to_keewano(RUN, {"event": "executing", "at": 100.7, "skill_id": "skill-2", "duration_s": 10.3})
        self.assertEqual([(e["eventTypeId"], e["data"]) for e in events], [(2006, RUN), (2030, "skill-2"), (2032, 10300)])
        self.assertEqual(to_keewano(RUN, {"event": "finished", "verdict": "success"})[1]["data"], "success")
        self.assertEqual(to_keewano(RUN, {"event": "not_a_stage"}), [])

    def test_registers_once_then_sends_ordered_batches_in_one_session(self):
        sink = self.sink()
        for name in ("planning", "plan_ready", "finished"):
            sink.emit(RUN, {"event": name, "at": 1})
        self.assertTrue(sink.flush())
        paths = [c[0] for c in self.fake.calls]
        self.assertEqual(paths, ["custom", "in", "in", "in"])
        batches = [c[1] for c in self.fake.calls if c[0] == "in"]
        self.assertEqual([b["batch"] for b in batches], [1, 2, 3])
        self.assertEqual({b["dataSessionId"] for b in batches}, {session_id(RUN)})
        self.assertEqual({b["installId"] for b in batches}, {sink.user_id})
        self.assertEqual({b["customEventsHash"] for b in batches}, {EVENTS_HASH})
        self.assertNotIn("testUserName", batches[0])

    def test_runs_share_the_arm_user_but_not_the_session(self):
        sink = self.sink()
        other = uuid.uuid4().hex
        sink.emit(RUN, {"event": "planning"})
        sink.emit(other, {"event": "planning"})
        sink.flush()
        batches = [c[1] for c in self.fake.calls if c[0] == "in"]
        self.assertEqual(len({b["installId"] for b in batches}), 1)
        self.assertEqual(len({b["dataSessionId"] for b in batches}), 2)
        self.assertEqual([b["batch"] for b in batches], [1, 1])

    def test_retry_reuses_the_batch_number(self):
        sink = self.sink(fake=FakeKeewano(fail_posts=1))
        sink.emit(RUN, {"event": "planning"})
        sink.flush()
        batches = [c[1]["batch"] for c in self.fake.calls if c[0] == "in"]
        self.assertEqual(batches, [1, 1])
        self.assertEqual(sink.stats["sent"], 1)

    def test_outage_spools_and_never_raises(self):
        sink = self.sink(fake=FakeKeewano(fail_posts=99))
        sink.emit(RUN, {"event": "finished", "verdict": "success"})
        self.assertTrue(sink.flush())
        self.assertEqual(sink.stats["failed"], 2)
        spooled = json.loads((Path(self.dir.name)/"failed.jsonl").read_text())
        self.assertEqual(spooled["run_id"], RUN)

    def test_disabled_without_key(self):
        sink = self.sink(api_key="")
        sink.emit(RUN, {"event": "planning"})
        self.assertFalse(sink.enabled)
        self.assertEqual(self.fake.calls, [])

    def test_custom_ids_are_unique_and_registered_types_match(self):
        ids = [e["eventTypeId"] for e in keewano_sink.EVENT_TYPES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i >= 2000 for i in ids))


if __name__ == "__main__":
    unittest.main()
