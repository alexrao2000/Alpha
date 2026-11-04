# Each subprocess owns a process group. Cancellation and timeouts terminate its
# descendants and reap the parent; provider stderr never becomes experiment data.

from __future__ import annotations

import asyncio
import os
import signal as signals
from pathlib import Path


async def run_process(command: list[str], data: str, *, timeout: float,
                      signal: asyncio.Event | None = None, env: dict[str, str] | None = None,
                      cwd: str | Path | None = None, label: str = "Process") -> str:
    if signal is not None and signal.is_set():
        raise asyncio.CancelledError("Experiment cancelled.")
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, cwd=cwd, start_new_session=os.name == "posix")
    except OSError as error:
        raise RuntimeError(f"Cannot start {label}: {error.__class__.__name__}") from None
    communication = asyncio.create_task(process.communicate(data.encode()))
    cancellation = asyncio.create_task(signal.wait()) if signal is not None else None
    try:
        watched = {communication} | ({cancellation} if cancellation else set())
        done, _ = await asyncio.wait(watched, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if cancellation is not None and cancellation in done:
            raise asyncio.CancelledError("Experiment cancelled.")
        if communication not in done:
            raise TimeoutError(f"{label} timed out.")
        stdout, _ = communication.result()
        if process.returncode != 0:
            raise RuntimeError(f"{label} exited with code {process.returncode}.")
        return stdout.decode()
    finally:
        def terminate_group(force: bool = False) -> None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signals.SIGKILL if force else signals.SIGTERM)
                elif process.returncode is None:
                    process.kill() if force else process.terminate()
            except ProcessLookupError:
                pass

        terminate_group()
        try:
            await asyncio.wait_for(asyncio.shield(communication), 2)
        except TimeoutError:
            terminate_group(force=True)
            await communication
        finally:
            await process.wait()
            if cancellation is not None:
                cancellation.cancel()
                await asyncio.gather(cancellation, return_exceptions=True)
