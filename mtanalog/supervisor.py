"""Unprivileged collector supervision and daily backups on the existing VPS."""
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

LOG = logging.getLogger(__name__)


SERVICE = 'mtanalog-supervisor.service'


def service_environment():
    env = os.environ.copy()
    env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    env.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path='+env['XDG_RUNTIME_DIR']+'/bus')
    return env


def managed_service():
    # Only adopt this project's installed unit, never an unrelated service.
    unit = Path.home()/'.config/systemd/user'/SERVICE
    return unit.exists() and str(Path(__file__).resolve().parents[1]) in unit.read_text()


def service_action(action):
    subprocess.run(['systemctl', '--user', action, SERVICE], env=service_environment(), check=True)


def process_is_supervisor(pid):
    try:
        parts = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        return b'mtanalog' in parts and b'serve' in parts
    except OSError:
        return False


def start(config, config_path):
    root = Path(config['data_dir'])
    root.mkdir(parents=True, exist_ok=True)
    if managed_service():
        service_action('start')
        result = subprocess.check_output(['systemctl', '--user', 'show', SERVICE, '-p', 'MainPID', '--value'], env=service_environment())
        return int(result)
    pid_path = root / 'supervisor.pid'
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text())
            if process_is_supervisor(pid):
                return pid
        except ValueError:
            pass
    with (root / 'launcher.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, '-m', 'mtanalog', '--config', str(config_path), 'serve'],
            cwd=Path(config_path).parent, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True)
    time.sleep(1)
    if process.poll() is not None:
        raise RuntimeError(f'Supervisor did not start; see {root / "launcher.log"}')
    return process.pid


def stop(config):
    if managed_service():
        service_action('stop')
        return True
    pid_path = Path(config['data_dir']) / 'supervisor.pid'
    if not pid_path.exists():
        return False
    pid = int(pid_path.read_text())
    if not process_is_supervisor(pid):
        return False
    os.kill(pid, signal.SIGTERM)
    return True


def serve(config, config_path):
    root = Path(config['data_dir'])
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / 'supervisor.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    pid_path = root / 'supervisor.pid'
    pid_path.write_text(str(os.getpid()) + '\n')
    handler = RotatingFileHandler(root / 'supervisor.log', maxBytes=5_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger().handlers[:] = [handler]
    stopping = threading.Event()
    def requested_stop(signum, _frame):
        LOG.warning('Supervisor received %s; shutdown requested', signal.Signals(signum).name)
        stopping.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, requested_stop)
    command = [sys.executable, '-m', 'mtanalog', '--config', str(config_path)]
    worker = None
    backup_child = None
    next_backup = time.time()+120
    LOG.info('Supervisor started; collection and daily backups enabled')
    try:
        while not stopping.is_set():
            if worker is None or worker.poll() is not None:
                if shutil.disk_usage(root).free < config['min_free_gib'] * 2**30:
                    LOG.error('Disk safety floor reached; collector restart suspended')
                    stopping.wait(60)
                    continue
                if worker is not None:
                    LOG.warning('Collector exited with %s; restarting', worker.returncode)
                    if stopping.wait(10):
                        break
                worker = subprocess.Popen(command + ['collect'], cwd=Path(config_path).parent)
            if backup_child is not None and backup_child.poll() is not None:
                if backup_child.returncode:
                    LOG.warning('Local backup failed; retry in one hour')
                    next_backup = time.time()+3600
                backup_child = None
            if time.time() >= next_backup and backup_child is None:
                backup_child = subprocess.Popen(command+['backup'], cwd=Path(config_path).parent)
                next_backup = time.time()+86400
            # Small atomic status file, never exposes connection credentials.
            status_path = root / 'supervisor.json'
            temporary = status_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'pid': os.getpid(), 'collector_pid': worker.pid,
                'heartbeat_ms': int(time.time()*1000), 'next_backup_ms': int(next_backup*1000)}))
            os.replace(temporary, status_path)
            stopping.wait(2)
    finally:
        for child in ([worker] if worker else []) + ([backup_child] if backup_child else []):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        pid_path.unlink(missing_ok=True)
        (root / 'supervisor.json').unlink(missing_ok=True)
        LOG.info('Supervisor stopped')
        lock.close()
