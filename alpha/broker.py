# Native workers use this local, private model gateway for shared admission and
# durable attempt accounting. Opaque per-workflow leases bind requests to their
# host-side job and model configuration. No graders or expected answers enter it.

from __future__ import annotations

import asyncio
from copy import deepcopy
import os
from pathlib import Path
import secrets
import tempfile

from .accounting import JOB, BudgetStop
from .contracts import JSON, compact_json


class ModelBroker:
    def __init__(self, backend):
        self.backend = backend
        self.leases: dict[str, JSON] = {}
        self.tasks: set[asyncio.Task] = set()

    async def __aenter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='alpha-gateway-', dir='/tmp' if os.name == 'posix' else None)
        self.path = str(Path(self.directory.name) / 'model.sock')
        try:
            self.server = await asyncio.start_unix_server(self.handle, path=self.path, limit=16 * 1024 * 1024)
        except BaseException:
            self.directory.cleanup()
            raise
        os.chmod(self.path, 0o600)
        return self

    def lease(self, job: JSON) -> JSON:
        token = secrets.token_hex(24)
        self.leases[token] = deepcopy(job)
        return {'socket': self.path, 'lease': token}

    def release(self, token: str) -> None:
        self.leases.pop(token, None)

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.directory.cleanup()

    async def handle(self, reader, writer):
        try:
            await self.respond(reader, writer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass
            self.tasks.discard(asyncio.current_task())

    async def respond(self, reader, writer):
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        context = None
        try:
            import json
            request = json.loads(await reader.readline())
            job = self.leases.get(request.get('lease'))
            if job is None:
                raise ValueError('Unknown workflow lease.')
            role = request.get('role')
            if role not in job['variant']['roles']:
                raise ValueError('Unknown workflow role.')
            context = JOB.set({**job, 'role': role})
            model = job['variant'].get('models', {}).get(role, self.backend.requested_model)
            backend = self.backend.for_model(model)
            result = await backend.complete(schema=request['schema'], prompt=request['prompt'])
            payload = {'result': result}
        except BudgetStop as error:
            payload = {'error': {'kind': 'budget', 'message': str(error)}}
        except asyncio.CancelledError:
            raise
        except Exception:
            payload = {'error': {'kind': 'transport', 'message': 'The model transport failed.'}}
        finally:
            if context is not None:
                JOB.reset(context)
        try:
            writer.write((compact_json(payload) + '\n').encode())
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass


async def gateway_complete(gateway: JSON, *, role: str, schema: JSON, prompt: str) -> JSON:
    import json
    reader, writer = await asyncio.open_unix_connection(gateway['socket'], limit=16 * 1024 * 1024)
    try:
        writer.write((compact_json({'lease': gateway['lease'], 'role': role, 'schema': schema, 'prompt': prompt}) + '\n').encode())
        await writer.drain()
        response = json.loads(await reader.readline())
        if 'error' in response:
            if response['error']['kind'] == 'budget':
                raise BudgetStop(response['error']['message'])
            raise RuntimeError(response['error']['message'])
        return response['result']
    finally:
        writer.close()
        await writer.wait_closed()
