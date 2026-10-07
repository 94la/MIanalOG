#!/usr/bin/env python3
"""Install the web systemd service; Caddy configuration is managed separately."""
import argparse
import os
from pathlib import Path
import pwd
import subprocess
import time
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def wait_for_health(url='http://127.0.0.1:8790/healthz', timeout=30, interval=.5):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(interval)
    raise RuntimeError('Web did not become ready; inspect journalctl -u mtanalog-web')


def unit_text(root, user, group):
    # systemd specifiers must be escaped even inside quoted paths.
    def quote(value):
        return '"'+str(value).replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"')+'"'
    return f'''[Unit]
Description=MIanalOG public BTCUSDT web charts
After=network-online.target
Wants=network-online.target

[Service]
User={user}
Group={group}
WorkingDirectory={str(root).replace('%', '%%')}
ExecStart={quote(root/'.venv/bin/python')} -m mtanalog web
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths={quote(root/'data')}
UMask=0077

[Install]
WantedBy=multi-user.target
'''


def main():
    if any(c.isspace() for c in str(ROOT)):
        raise SystemExit('Use a checkout path without whitespace for systemd installation.')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True, help='Unprivileged owner of the checkout')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0:
        parser.error('Choose an unprivileged user')
    import grp
    text = unit_text(ROOT, args.user, grp.getgrgid(account.pw_gid).gr_name)
    if args.dry_run:
        print(text)
        return
    if os.geteuid() != 0:
        parser.error('Run with sudo/root')
    if ROOT.stat().st_uid != account.pw_uid or not (ROOT/'.venv/bin/python').exists():
        parser.error('Checkout must belong to --user and contain .venv')
    data = ROOT/'data'
    data.mkdir(exist_ok=True)
    os.chown(data, account.pw_uid, account.pw_gid)
    target = Path('/etc/systemd/system/mtanalog-web.service')
    if target.exists() and str(ROOT) not in target.read_text():
        parser.error('Existing unit belongs to another checkout; review it manually')
    with tempfile.TemporaryDirectory(prefix='mianalog-unit-') as folder:
        candidate = Path(folder)/target.name
        candidate.write_text(text)
        subprocess.run(['systemd-analyze', 'verify', str(candidate)], check=True)
    target.write_text(text)
    target.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', 'mtanalog-web.service'], check=True)
    subprocess.run(['systemctl', 'restart', 'mtanalog-web.service'], check=True)
    import tomllib
    config = tomllib.loads((ROOT/'config.toml').read_text())
    wait_for_health(f"http://127.0.0.1:{config.get('web_port', 8790)}/healthz")
    print('Web service ready. Configure Caddy separately using deploy/Caddyfile.example.')


if __name__ == '__main__':
    main()
