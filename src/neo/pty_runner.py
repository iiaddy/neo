"""Run a command under a real PTY (no controlling terminal needed).

Headless-safe: the child gets its own session (os.setsid) and its
stdin/stdout/stderr attached to the PTY slave; the parent reads the master
fd via asyncio. On timeout the whole process group is killed.
Output is kept raw (ANSI escape sequences included) and decoded with
errors="replace".
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios


async def run_pty(
    command: str,
    cwd: str | None = None,
    timeout_s: float = 60.0,
    cols: int = 120,
    rows: int = 30,
    input_data: bytes | None = None,
) -> tuple[str, int | None]:
    """Run `command` under a PTY.

    Returns (output, exit_code). On timeout the process group is killed and
    whatever output arrived so far is returned with the kill exit code.
    """
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                     struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass
    loop = asyncio.get_running_loop()
    proc = await loop.run_in_executor(
        None,
        lambda: subprocess.Popen(
            command,
            shell=True,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            preexec_fn=os.setsid,  # own session + process group
            close_fds=True,
            env={**os.environ},
        ),
    )
    os.close(slave)  # parent only needs the master end

    output = bytearray()
    try:
        await _drain(master, output, proc, timeout_s, input_data)
    except asyncio.TimeoutError:
        _kill_group(proc)
        _drain_sync(master, output, proc)
    finally:
        os.close(master)

    await _wait_reap(proc)
    return bytes(output).decode("utf-8", errors="replace"), proc.returncode


async def _wait_reap(proc: subprocess.Popen) -> None:
    """Reap the child without blocking the loop; kill if it lingers."""
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(None, proc.wait),
                               timeout=5.0)
    except asyncio.TimeoutError:
        # Child outlived its PTY (closed slaves but kept running).
        _kill_group(proc)
        await loop.run_in_executor(None, proc.wait)


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _pump(master: int, output: bytearray, proc: subprocess.Popen) -> bool:
    """Read once from the master fd. Returns True when the stream is done."""
    try:
        chunk = os.read(master, 65536)
    except BlockingIOError:
        return False
    except OSError as exc:
        if exc.errno == errno.EIO:
            return True  # slave fully closed
        raise
    if chunk:
        output.extend(chunk)
        return False
    # empty read: done only once the child is really gone
    return proc.poll() is not None


async def _drain(master: int, output: bytearray, proc: subprocess.Popen,
                 timeout_s: float, input_data: bytes | None) -> None:
    loop = asyncio.get_running_loop()
    _set_nonblocking(master)
    done = loop.create_future()

    def _on_readable() -> None:
        if _pump(master, output, proc) and not done.done():
            done.set_result(None)

    loop.add_reader(master, _on_readable)
    try:
        if input_data:
            await asyncio.sleep(0.25)  # let the child reach its read
            try:
                os.write(master, input_data)
            except OSError:
                pass
        await asyncio.wait_for(asyncio.ensure_future(done), timeout=timeout_s)
    finally:
        loop.remove_reader(master)


def _drain_sync(master: int, output: bytearray, proc: subprocess.Popen) -> None:
    """Best-effort blocking drain after a kill (bounded)."""
    _set_nonblocking(master)
    for _ in range(200):
        if _pump(master, output, proc):
            return
        if proc.poll() is not None:
            # child dead; one last chance for buffered output
            for _ in range(20):
                if _pump(master, output, proc):
                    return
            return
