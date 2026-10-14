"""The agent's own computer, with a sleep/wake lifecycle.

`LocalSandbox` is a stand-in for the Firecracker microVM described in the lecture: the working
directory is the "disk". `sleep()` snapshots it to a tarball and removes it; `wake()` restores it.
It is NOT an isolation boundary. For real use implement the same `Sandbox` interface on top of a
microVM or container runtime. The shell is only as safe as that backend plus the autoreview layer.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tarfile
from pathlib import Path
from typing import Protocol


class Sandbox(Protocol):
    def state(self) -> str: ...
    async def wake(self) -> None: ...
    async def sleep(self) -> None: ...
    async def exec(self, command: str, timeout: int) -> str: ...
    async def read_file(self, path: str, offset: int = 0, limit: int = 20000) -> str: ...
    async def write_file(self, path: str, content: str) -> str: ...


class SandboxError(Exception):
    pass


class LocalSandbox:
    def __init__(self, root: Path, session: str, max_inline_output: int = 4000):
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session)
        self.workdir = Path(root) / "live" / safe
        self.snapshot = Path(root) / "snapshots" / f"{safe}.tar.gz"
        self.max_inline_output = max_inline_output
        self._lock = asyncio.Lock()
        self.snapshot.parent.mkdir(parents=True, exist_ok=True)
        self.workdir.parent.mkdir(parents=True, exist_ok=True)

    # ---- lifecycle ---------------------------------------------------------
    def state(self) -> str:
        if self.workdir.exists():
            return "awake"
        return "asleep" if self.snapshot.exists() else "none"

    async def wake(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._wake_sync)

    def _wake_sync(self) -> None:
        if self.workdir.exists():
            return
        tmp = self.workdir.with_suffix(".restoring")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        if self.snapshot.exists():
            with tarfile.open(self.snapshot, "r:gz") as tar:
                tar.extractall(tmp, filter="data")
        tmp.rename(self.workdir)

    async def sleep(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._sleep_sync)

    def _sleep_sync(self) -> None:
        if not self.workdir.exists():
            return
        tmp = self.snapshot.with_suffix(".tmp")
        with tarfile.open(tmp, "w:gz") as tar:
            tar.add(self.workdir, arcname=".")
        os.replace(tmp, self.snapshot)  # atomic: a crash never leaves a half-written snapshot
        shutil.rmtree(self.workdir)

    # ---- file access ----------------------------------------------------------
    def _resolve(self, path: str) -> Path:
        base = self.workdir.resolve()
        target = (base / path).resolve()
        if target != base and base not in target.parents:
            raise SandboxError(f"path '{path}' is outside the sandbox working directory")
        return target

    async def read_file(self, path: str, offset: int = 0, limit: int = 20000) -> str:
        await self.wake()
        target = self._resolve(path)
        if not target.is_file():
            raise SandboxError(f"no such file: {path}. Use run_shell with 'ls' to see what exists.")
        data = target.read_text(errors="replace")
        chunk = data[offset: offset + limit]
        more = len(data) - (offset + len(chunk))
        return chunk + (f"\n[... {more} more chars; call read_file with offset={offset + len(chunk)}]" if more > 0 else "")

    async def write_file(self, path: str, content: str) -> str:
        await self.wake()
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {len(content)} chars to {path}"

    # ---- commands ----------------------------------------------------------------
    async def exec(self, command: str, timeout: int) -> str:
        await self.wake()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.workdir),
            "LANG": "C.UTF-8",
        }
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            cwd=self.workdir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
            raise SandboxError(
                f"command timed out after {timeout}s and was killed. Run long jobs in the background "
                "(nohup ... > job.log 2>&1 &) and poll the log file."
            )
        text = out.decode(errors="replace")
        text = f"{text}\n[exit code {proc.returncode}]" if proc.returncode else text
        return self._spill(text)

    def _spill(self, text: str) -> str:
        """Long output goes to a file; the model gets the head and the path."""
        if len(text) <= self.max_inline_output:
            return text
        out_dir = self.workdir / ".outputs"
        out_dir.mkdir(exist_ok=True)
        n = len(list(out_dir.iterdir())) + 1
        rel = f".outputs/out-{n}.txt"
        (self.workdir / rel).write_text(text)
        head = text[: self.max_inline_output // 2]
        return f"{head}\n[... output truncated ({len(text)} chars). Full output saved to {rel}; use read_file or grep on it.]"
