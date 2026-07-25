"""Claude CLI process adapter.

The rest of the application talks to this module through callbacks and a small
``SessionHandle`` interface.  No task persistence, HTTP or scheduling logic is
allowed in here.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

EventCallback = Callable[[dict[str, Any]], None]
ErrorCallback = Callable[[str], None]
ExitCallback = Callable[[int], None]


@dataclass(frozen=True)
class CliLaunchSpec:
    task_id: str
    cwd: Path
    model: str | None
    permission_mode: str | None
    add_dirs: tuple[str, ...]
    session_id: str | None = None
    resume: bool = False


class SessionHandle:
    def __init__(self, process: subprocess.Popen[str]):
        self._process = process
        self._write_lock = threading.Lock()

    @property
    def process(self) -> subprocess.Popen[str]:
        return self._process

    def is_alive(self) -> bool:
        return self._process.poll() is None

    def send_user_message(self, text: str) -> None:
        payload = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._write_lock:
            if self._process.stdin is None:
                raise BrokenPipeError("Claude CLI stdin 不可用")
            self._process.stdin.write(encoded)
            self._process.stdin.flush()

    def terminate(self) -> None:
        if self.is_alive():
            self._process.terminate()


class ClaudeCliAdapter:
    """Starts Claude Code in stream-json mode and exposes typed callbacks."""

    def __init__(self, executable: str | None = None):
        configured = executable or os.environ.get("CLAUDE2007_CLAUDE_BIN", "claude")
        self._base_command = shlex.split(configured)
        if not self._base_command:
            raise ValueError("CLAUDE2007_CLAUDE_BIN 不能为空")

    @property
    def executable_name(self) -> str:
        return self._base_command[0]

    def build_command(self, spec: CliLaunchSpec) -> list[str]:
        command = [
            *self._base_command,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
        if spec.model:
            command += ["--model", spec.model]
        if spec.permission_mode and spec.permission_mode != "default":
            command += ["--permission-mode", spec.permission_mode]
        for directory in spec.add_dirs:
            command += ["--add-dir", directory]
        if spec.resume and spec.session_id:
            command += ["--resume", spec.session_id]
        return command

    def start(
        self,
        spec: CliLaunchSpec,
        *,
        on_event: EventCallback,
        on_error: ErrorCallback,
        on_exit: ExitCallback,
    ) -> SessionHandle:
        process = subprocess.Popen(
            self.build_command(spec),
            cwd=str(spec.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        handle = SessionHandle(process)

        threading.Thread(
            target=self._pump_stdout,
            args=(process, on_event, on_exit),
            daemon=True,
            name=f"claude-stdout-{spec.task_id}",
        ).start()
        threading.Thread(
            target=self._pump_stderr,
            args=(process, on_error),
            daemon=True,
            name=f"claude-stderr-{spec.task_id}",
        ).start()
        return handle

    @staticmethod
    def _pump_stdout(
        process: subprocess.Popen[str],
        on_event: EventCallback,
        on_exit: ExitCallback,
    ) -> None:
        try:
            if process.stdout is not None:
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = {"type": "x-raw", "text": line}
                    on_event(event)
        finally:
            code = process.wait()
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            on_exit(code)

    @staticmethod
    def _pump_stderr(process: subprocess.Popen[str], on_error: ErrorCallback) -> None:
        tail: list[str] = []
        if process.stderr is not None:
            for raw_line in process.stderr:
                tail.append(raw_line.rstrip())
                tail = tail[-40:]
        code = process.wait()
        if process.stderr is not None:
            try:
                process.stderr.close()
            except OSError:
                pass
        if code != 0 and tail:
            on_error("\n".join(tail))
