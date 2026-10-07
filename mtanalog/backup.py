"""Local consistent SQLite and source backup; never includes credentials."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile

from .storage import sync_directory


def backup(config, project):
    root = Path(config['data_dir'])
    destination = root/'backups'
    destination.mkdir(exist_ok=True)
    lock = (destination/'backup.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if shutil.disk_usage(root).free < (config.get('min_free_gib', 3)+1)*2**30:
            raise RuntimeError('Insufficient disk space for backup')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        target = destination/stamp
        with tempfile.TemporaryDirectory(dir=destination, prefix='.pending-') as temporary:
            temporary = Path(temporary)
            source = sqlite3.connect(f'file:{(root/"archive.sqlite").resolve()}?mode=ro', uri=True)
            output = sqlite3.connect(temporary/'archive.sqlite')
            try:
                source.backup(output, pages=100, sleep=.01)
                if output.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup integrity check failed')
            finally:
                output.close()
                source.close()
            files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=project).split(b'\0')
            with tarfile.open(temporary/'sources.tar.gz', 'w:gz') as archive:
                for name in files:
                    if not name:
                        continue
                    path = Path(os.fsdecode(name))
                    # Explicit source roots, never arbitrary tracked data or secrets.
                    allowed = path.parts[0] in ('mtanalog', 'tests', 'deploy', 'docs') or str(path) in (
                        'README.md', 'AGENTS.md', 'spec.md', 'tasks.md', 'config.toml',
                        'requirements.txt', 'requirements.lock.txt', '.gitignore')
                    actual = Path(project)/path
                    if allowed and actual.is_file() and not actual.is_symlink():
                        archive.add(actual, arcname=str(path), recursive=False)
            (temporary/'manifest.json').write_text(json.dumps({
                'created_utc': stamp, 'scope': 'SQLite + tracked source/config; excludes raw feeds and credentials'
            })+'\n')
            for path in temporary.iterdir():
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
            sync_directory(temporary)
            os.rename(temporary, target)
            sync_directory(destination)
        previous = sorted(p for p in destination.iterdir() if p.is_dir() and not p.name.startswith('.'))
        for path in previous[:-3]:
            shutil.rmtree(path)
        sync_directory(destination)
        return target
    finally:
        lock.close()
