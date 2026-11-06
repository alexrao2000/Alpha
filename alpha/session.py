# Checkpoints use atomic replacement and checksums. Completed trajectories are
# append-only, hash-chained journal records; resume refuses altered code, inputs,
# models, candidates, policy, or evidence and never reconstructs model answers.

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path

from .contracts import JSON, Run, compact_json


def digest(value) -> str:
    return sha256(compact_json(value).encode()).hexdigest()


def job_key(case_id: str, variant: str, trial: int) -> str:
    return compact_json([case_id, variant, trial])


def atomic_write(path: Path, value: JSON) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        stream.write(compact_json(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class RunStore:
    def __init__(self, plan: JSON, directory: Path | None = None, *, resume: bool = False):
        self.directory = directory
        self.plan = deepcopy(plan)
        self.fingerprint = digest(plan)
        self.state: JSON = {'runs': [], 'attempts': [], 'frozen': None, 'active_seconds': 0.0, 'status': 'running'}
        self.previous = '0' * 64
        self.completed: dict[str, Run] = {}
        if directory is None:
            if resume:
                raise ValueError('Resume requires a run directory.')
            return
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'checkpoint.json'
        self.journal = directory / 'progress.jsonl'
        if resume:
            record = json.loads(self.path.read_text(encoding='utf-8'))
            if record.get('checksum') != digest(record.get('state')) or record.get('fingerprint') != self.fingerprint:
                raise ValueError('Resume refused: checkpoint, task, code, model, candidate, or policy fingerprint differs.')
            self.state = record['state']
            recovered = []
            for line in self.journal.read_text(encoding='utf-8').splitlines():
                run = json.loads(line)
                checksum = run.pop('record_sha256', None)
                previous = run.pop('previous_record_sha256', None)
                if previous != self.previous or checksum != digest({'previous': previous, 'run': run}):
                    raise ValueError('Resume refused: recorded trajectory was altered.')
                run['previous_record_sha256'], run['record_sha256'] = previous, checksum
                key = job_key(run['case_id'], run['variant'], run['trial'])
                if key in self.completed:
                    raise ValueError('Resume refused: duplicate completed workflow.')
                self.completed[key] = run
                recovered.append(run)
                self.previous = checksum
            old = self.state['runs']
            if len(old) > len(recovered) or old != recovered[:len(old)]:
                raise ValueError('Resume refused: checkpoint and trajectory journal disagree.')
            self.state['runs'] = recovered
            for attempt in self.state['attempts']:
                if attempt['status'] == 'running':
                    attempt['status'] = 'interrupted'
            frozen = self.state.get('frozen')
            if frozen:
                discovery = [r for r in recovered if r['split'] == 'discovery']
                if frozen['discovery_sha256'] != digest(discovery):
                    raise ValueError('Resume refused: frozen discovery evidence differs.')
            if self.state.get('status') == 'complete':
                self.state['status'] = 'complete'
        else:
            if self.path.exists() or any(directory.iterdir()):
                raise ValueError('Run directory is not empty; use --resume for an existing experiment.')
            self.journal.touch(mode=0o600)
        self.save()

    def save(self) -> None:
        if self.directory is not None:
            atomic_write(self.path, {'fingerprint': self.fingerprint, 'plan': self.plan,
                                     'checksum': digest(self.state), 'state': self.state})

    def complete(self, run: Run) -> None:
        key = job_key(run['case_id'], run['variant'], run['trial'])
        if key in self.completed:
            raise ValueError('Cannot replace a completed workflow.')
        run['previous_record_sha256'] = self.previous
        checksum = digest({'previous': self.previous, 'run': {k: v for k, v in run.items() if k != 'previous_record_sha256'}})
        run['record_sha256'] = checksum
        if self.directory is not None:
            with self.journal.open('a', encoding='utf-8') as stream:
                stream.write(compact_json(run) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        self.previous = checksum
        self.completed[key] = run
        self.state['runs'].append(run)
        self.save()

    def freeze(self, selection: JSON) -> None:
        if self.state['frozen'] is not None:
            if self.state['frozen'] != selection:
                raise ValueError('Cannot change the frozen discovery decision.')
            return
        self.state['frozen'] = deepcopy(selection)
        self.save()
