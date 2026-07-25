from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.api import AppContext, QuietHTTPServer, make_handler
from backend.cli_adapter import ClaudeCliAdapter
from backend.scheduler import Scheduler


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("condition timed out")


class RefactorSmokeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        (self.base / "frontend" / "styles").mkdir(parents=True)
        (self.base / "frontend" / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
        (self.base / "config.json").write_text(json.dumps({
            "user_name": "Tester",
            "default_model": "sonnet",
            "default_permission_mode": "acceptEdits",
            "models": ["sonnet"],
            "projects": [{"name": "当前目录", "path": ".", "pinned": True}],
            "friends": [],
        }), encoding="utf-8")
        stub = ROOT / "tests" / "stub_claude.py"
        cli = ClaudeCliAdapter(f"{sys.executable} {stub}")
        self.context = AppContext(
            self.base,
            cli=cli,
            scheduler_autostart=False,
            skills_dir=self.base / "skills",
        )

    def tearDown(self):
        self.context.close()
        time.sleep(0.08)
        self.temp.cleanup()

    def test_task_lifecycle_and_event_sequence(self):
        task = self.context.task_service.create_task(
            title="test",
            project="当前目录",
            cwd=self.base,
            model="sonnet",
            permission_mode="acceptEdits",
            prompt="hello",
            agent_name="小蓝",
            agent_avatar="🤖",
        )
        finished = wait_until(lambda: (
            current if (current := self.context.task_service.get_task(task["id"]))["status"] == "idle" else None
        ))
        self.assertEqual(finished["agent_name"], "小蓝")
        self.assertEqual(finished["agent_avatar"], "🤖")
        self.assertTrue(finished["session_id"].startswith("stub-"))

        events = self.context.task_service.replay_after(task["id"])
        sequences = [event["_seq"] for event in events]
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertIn("result", {event["type"] for event in events})

        self.context.task_service.send_message(task["id"], "again")
        wait_until(lambda: len([
            event for event in self.context.task_service.replay_after(task["id"])
            if event.get("type") == "result"
        ]) >= 2)

    def test_interrupt_is_cancelled_not_error(self):
        task = self.context.task_service.create_task(
            title="sleep",
            project="当前目录",
            cwd=self.base,
            model="sonnet",
            permission_mode="acceptEdits",
            prompt="sleep",
        )
        wait_until(lambda: self.context.task_service.get_task(task["id"])["session_id"])
        self.context.task_service.interrupt(task["id"])
        stopped = wait_until(lambda: (
            current if (current := self.context.task_service.get_task(task["id"]))["status"] == "idle" else None
        ))
        self.assertEqual(stopped["status"], "idle")
        exit_event = wait_until(lambda: next((
            event for event in reversed(self.context.task_service.replay_after(task["id"]))
            if event.get("type") == "x-proc-exit"
        ), None))
        self.assertTrue(exit_event["cancelled"])

    def test_http_api_and_static_entry(self):
        server = QuietHTTPServer(("127.0.0.1", 0), make_handler(self.context))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
                self.assertIn(b"ok", response.read())
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/tasks",
                data=json.dumps({
                    "prompt": "http",
                    "project": "当前目录",
                    "model": "sonnet",
                    "permission_mode": "acceptEdits",
                    "agent_name": "HTTP Agent",
                    "agent_avatar": "🛰️",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                task = json.loads(response.read())
            self.assertEqual(task["agent_name"], "HTTP Agent")
            self.assertEqual(task["agent_avatar"], "🛰️")
        finally:
            server.shutdown()
            server.server_close()

    def test_scheduler_math_and_skill_boundary(self):
        now = time.time()
        interval = Scheduler.compute_next({
            "sched_type": "interval",
            "interval_min": 5,
            "created_at": now - 1000,
        }, after=now)
        self.assertGreater(interval, now)
        with self.assertRaises(ValueError):
            self.context.skills.read("../escape")


if __name__ == "__main__":
    unittest.main()
