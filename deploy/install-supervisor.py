#!/usr/bin/env python3
"""Install this project's user service without creating a second archive writer."""
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
UNIT = 'mtanalog-supervisor.service'


def main():
    if any(c.isspace() for c in str(ROOT)):
        raise SystemExit('Use a checkout path without whitespace for systemd installation.')
    if os.getuid() == 0:
        raise SystemExit('Run as the unprivileged checkout owner, not root.')
    env = os.environ.copy()
    env['XDG_RUNTIME_DIR'] = f'/run/user/{os.getuid()}'
    env['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path='+env['XDG_RUNTIME_DIR']+'/bus'
    def run(*args):
        return subprocess.run(args, cwd=ROOT, env=env, check=True)
    generated = ROOT/'data/deploy'/UNIT
    generated.parent.mkdir(parents=True, exist_ok=True)
    template = (ROOT/'deploy/mtanalog-supervisor.service.in').read_text()
    escaped = str(ROOT).replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"')
    generated.write_text(template.replace('@ROOT@', escaped))
    run('systemd-analyze', 'verify', str(generated))
    run('loginctl', 'enable-linger', str(os.getuid()))
    destination = Path.home()/'.config/systemd/user'/UNIT
    if destination.exists() and str(ROOT) not in destination.read_text():
        raise SystemExit('Existing unit belongs to another checkout; review manually.')
    if not destination.exists():
        run(str(ROOT/'.venv/bin/python'), '-m', 'mtanalog', 'stop')
    else:
        run('systemctl', '--user', 'stop', UNIT)
    # Wait for the old supervisor and its children to finish flushing.
    for _ in range(75):
        if not (ROOT/'data/supervisor.pid').exists():
            break
        time.sleep(1)
    else:
        raise SystemExit('Old supervisor did not exit; no new writer started.')
    # Ask the user manager to link the project unit; avoid writing its home directly.
    if destination.exists():
        run('systemctl', '--user', 'disable', UNIT)
    run('systemctl', '--user', 'enable', str(generated))
    run('systemctl', '--user', 'daemon-reload')
    run('systemctl', '--user', 'start', UNIT)
    run('systemctl', '--user', 'is-active', UNIT)


if __name__ == '__main__':
    main()
