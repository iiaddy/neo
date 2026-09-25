"""Post-edit verification: run project checks and report back."""
from __future__ import annotations

import subprocess
from pathlib import Path


def run_verification(commands: list[str], workdir: str | Path,
                     timeout: int = 180) -> list[tuple[str, bool, str]]:
    """Run each shell command; return [(command, ok, output)]."""
    results = []
    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=str(workdir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", timeout=timeout)
            out = proc.stdout or ""
            if len(out) > 20_000:
                out = out[-20_000:] + "\n…[truncated]"
            results.append((cmd, proc.returncode == 0, out.strip()))
        except subprocess.TimeoutExpired:
            results.append((cmd, False, f"timed out after {timeout}s"))
        except OSError as e:
            results.append((cmd, False, f"failed to start: {e}"))
    return results
