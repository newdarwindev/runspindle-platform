"""Single-process Strix manager; reservations precede all external operations."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from .backend import Backend, execute, helper, digest
from .model import choose_jobs
from .state import State


def gh(path, method='GET'):
    return json.loads(execute(['gh', 'api', '--method', method, path], timeout=45).stdout or 'null')


def pages(path, key):
    result = []
    for page in range(1, 101):
        separator = '&' if '?' in path else '?'
        value = gh(f'{path}{separator}per_page=100&page={page}')
        rows = value[key]
        result.extend(rows)
        if len(rows) < 100:
            return result
    raise RuntimeError('Pagination limit reached; inventory uncertain')


def trusted(run, repository):
    return (run.get('event') in ('push', 'workflow_dispatch') and
            (run.get('head_branch') == 'main' or run.get('head_branch', '').startswith('ci/')) and
            run.get('head_repository', {}).get('full_name') == repository)


class Manager:
    def __init__(self, cfg, directory, backend=None):
        self.cfg = cfg
        self.state = State(directory)
        self.backend = backend or Backend(self.state)
        self.stopping = False

    def inventory(self):
        queue, jobs = [], []
        for repo in self.cfg['repositories']:
            full = self.cfg['owner'] + '/' + repo
            # Include in_progress workflow runs containing queued sibling jobs.
            runs = gh(f'repos/{full}/actions/runs?per_page=30')['workflow_runs']
            for run in runs:
                if not trusted(run, full):
                    continue
                active = [r for r in self.state.active() if r['repository'] == repo]
                if run['status'] == 'completed':
                    updated = datetime.fromisoformat(run['updated_at'].replace('Z', '+00:00')).timestamp()
                    if not active or updated < min(r['created'] for r in active) - 120:
                        continue
                for job in pages(f'repos/{full}/actions/runs/{run["id"]}/jobs', 'jobs'):
                    job = job | {'repository': repo, 'run_id': run['id'], 'head_sha': run['head_sha'],
                                 'event': run['event'], 'head_branch': run['head_branch']}
                    jobs.append(job)
                    if job['status'] == 'queued':
                        queue.append(job)
        return queue, jobs

    def registrations(self, repo):
        return pages(f'repos/{self.cfg["owner"]}/{repo}/actions/runners', 'runners')

    def remove_registration(self, resource):
        full = f'{self.cfg["owner"]}/{resource["repository"]}'
        for runner in self.registrations(resource['repository']):
            if runner['name'] == resource['runner_name']:
                gh(f'repos/{full}/actions/runners/{runner["id"]}', 'DELETE')
        if any(r['name'] == resource['runner_name'] for r in self.registrations(resource['repository'])):
            raise RuntimeError('Runner registration still present')

    def cleanup(self, resource, reason):
        self.state.update(resource, 'cleaning', cleanup_reason=reason)
        try:
            self.state.update(resource, 'cleaning', observed_guest=helper('observe', resource['slot']))
            self.backend.diagnostics(resource)
            self.backend.destroy(resource)
            self.remove_registration(resource)
            self.state.update(resource, 'destroyed', cleanup_verified=True)
        except Exception as error:
            self.state.update(resource, 'uncertain', cleanup_error=str(error))

    def reconcile(self):
        occupied = self.state.active()
        for slot in range(2):
            observed = helper('observe', slot)
            if observed.get('ActiveState') in ('active', 'activating', 'deactivating') and not any(r['slot'] == slot for r in occupied):
                # No replacement may launch while an untracked service exists.
                helper('stop', slot, observed['resource_id'])
                directory = Path('/var/lib/runspindle-fleet/slots') / str(slot)
                for name in ('disk.qcow2', 'seed.iso', 'serial.log'):
                    path = directory / name
                    if path.exists():
                        archive = self.state.directory / f'orphan-{slot}-{int(time.time())}-{name}'
                        path.rename(archive)
        for resource in self.state.active():
            observed = helper('observe', resource['slot'])
            if resource['phase'] != 'running' or observed.get('ActiveState') != 'active':
                self.cleanup(resource, 'restart reconciliation')
        names = {r['runner_name'] for r in self.state.active()}
        for repo in self.cfg['repositories']:
            for runner in self.registrations(repo):
                if runner['name'].startswith('runspindle-fleet-') and runner['name'] not in names:
                    gh(f'repos/{self.cfg["owner"]}/{repo}/actions/runners/{runner["id"]}', 'DELETE')
        self.state.set('reconciled_at', time.time())

    def advance(self, resource, jobs):
        now = time.time()
        try:
            if resource['phase'] in ('reserved', 'uncertain', 'cleaning'):
                self.cleanup(resource, 'unfinished lifecycle')
            elif resource['phase'] == 'starting':
                if now > resource['details']['boot_deadline']:
                    self.cleanup(resource, 'boot timeout')
                elif self.backend.ready(resource):
                    self.backend.isolation(resource)
                    full = f'{self.cfg["owner"]}/{resource["repository"]}'
                    token = gh(f'repos/{full}/actions/runners/registration-token', 'POST')['token']
                    self.state.update(resource, 'registering', registration_deadline=now + 120)
                    self.backend.register(resource, full, token, self.cfg['lanes']['strix-linux-x64']['labels'])
                    registration = next((r for r in self.registrations(resource['repository'])
                                         if r['name'] == resource['runner_name']), None)
                    if not registration:
                        raise RuntimeError('Registration not observed')
                    self.state.update(resource, 'running', runner_id=registration['id'], registered_at=time.time())
            elif resource['phase'] == 'running':
                claimed = [j for j in jobs if j.get('runner_name') == resource['runner_name']]
                if len(claimed) > 1:
                    raise RuntimeError('Ephemeral runner claimed multiple jobs')
                if claimed:
                    job = claimed[0]
                    self.state.update(resource, 'running', claimed_job=job)
                    if job['status'] == 'completed':
                        try:
                            result = execute(['gh', 'api', f'repos/{self.cfg["owner"]}/{resource["repository"]}/actions/jobs/{job["id"]}/logs'], timeout=45)
                            (self.backend.directory(resource) / 'github-job.log').write_text(result.stdout)
                        except Exception as error:
                            self.state.update(resource, 'running', log_error=str(error))
                        self.cleanup(resource, 'job ' + str(job.get('conclusion')))
                    elif helper('observe', resource['slot']).get('ActiveState') != 'active':
                        gh(f'repos/{self.cfg["owner"]}/{resource["repository"]}/actions/runs/{job["run_id"]}/cancel', 'POST')
                        self.cleanup(resource, 'forced guest termination during job')
                    elif job.get('started_at') and now - datetime.fromisoformat(job['started_at'].replace('Z', '+00:00')).timestamp() > self.cfg['job_timeout_seconds']:
                        gh(f'repos/{self.cfg["owner"]}/{resource["repository"]}/actions/runs/{job["run_id"]}/cancel', 'POST')
                        self.cleanup(resource, 'execution timeout: failed evidence')
                elif now - resource['details']['registered_at'] > 180:
                    self.cleanup(resource, 'registration-to-job timeout: blocked evidence')
                elif helper('observe', resource['slot']).get('ActiveState') != 'active':
                    self.cleanup(resource, 'forced guest termination')
            else:
                self.cleanup(resource, 'interrupted registration')
        except Exception as error:
            self.state.update(resource, 'uncertain', error=str(error))

    def tick(self):
        queue, jobs = self.inventory()
        self.state.set('queue', queue)
        self.state.set('poll_at', time.time())
        for resource in self.state.active():
            self.advance(resource, jobs)
        active = self.state.active()
        reserved_jobs = {(r['repository'], r['requested_job']) for r in active}
        queue = [j for j in queue if (j['repository'], j['id']) not in reserved_jobs]
        selected, cursor = choose_jobs(self.cfg, 'strix', queue, active,
                                       self.state.get('cursor', 0), self.state.get('drained', True))
        for job in selected:
            resource = self.state.reserve(job)
            # Cursor is persisted alongside each actual reservation, before boot.
            index = self.cfg['repositories'].index(job['repository'])
            self.state.set('cursor', (index + 1) % len(self.cfg['repositories']))
            try:
                self.backend.create(resource)
            except Exception as error:
                self.state.update(resource, 'uncertain', error=str(error))
        self.state.set('heartbeat', time.time())
        self.state.set('peak_slots', max(self.state.get('peak_slots', 0), len(self.state.active())))

    def run(self):
        os.umask(0o077)
        with self.state.lock():
            signal.signal(signal.SIGTERM, lambda *args: setattr(self, 'stopping', True))
            self.reconcile()
            while not self.stopping:
                try:
                    self.tick()
                    self.state.set('error', None)
                except Exception as error:
                    self.state.set('error', str(error))
                for _ in range(self.cfg['poll_seconds']):
                    if self.stopping:
                        break
                    time.sleep(1)
            # SIGTERM preserves guests and reservations for restart recovery.
            self.state.set('stopped_at', time.time())
