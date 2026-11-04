# Verify lifecycle and failure boundaries with local processes, without model calls.

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

from alpha.backends import CodexBackend
from alpha.processes import run_process


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_bounded(self):
        with self.assertRaisesRegex(TimeoutError, 'timed out'):
            await run_process([sys.executable, '-c', 'import time;time.sleep(30)'], '', timeout=0.1)

    async def test_nonzero_exit_never_copies_provider_diagnostics(self):
        with self.assertRaises(RuntimeError) as result:
            await run_process([sys.executable, '-c', 'import sys;print("PRIVATE_PROVIDER_DIAGNOSTIC",file=sys.stderr);sys.exit(7)'], '', timeout=5, label='Model CLI')
        self.assertNotIn('PRIVATE_PROVIDER_DIAGNOSTIC', str(result.exception))
        self.assertIn('code 7', str(result.exception))

    @unittest.skipUnless(os.name == 'posix', 'Requires POSIX process groups')
    async def test_cancellation_terminates_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            ready, stopped = Path(directory) / 'ready', Path(directory) / 'stopped'
            leaf = ('import signal,time,sys;from pathlib import Path;'
                    f'signal.signal(signal.SIGTERM,lambda *_:(Path({str(stopped)!r}).write_text("stopped"),sys.exit(0)));'
                    f'Path({str(ready)!r}).write_text("ready");time.sleep(30)')
            parent = f'import subprocess,sys;subprocess.run([sys.executable,"-c",{leaf!r}])'
            cancellation = asyncio.Event()
            task = asyncio.create_task(run_process([sys.executable, '-c', parent], '', timeout=10, signal=cancellation))
            try:
                async with asyncio.timeout(5):
                    while not ready.exists():
                        await asyncio.sleep(0.01)
                cancellation.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                async with asyncio.timeout(3):
                    while not stopped.exists():
                        await asyncio.sleep(0.01)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_malformed_cli_event_stream_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'invalid-codex'
            binary.write_text(f'#!{sys.executable}\nprint("not json")\n')
            binary.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, 'unreadable event stream'):
                await CodexBackend(binary=str(binary)).complete(schema={}, prompt='test')
