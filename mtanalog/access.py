"""Optional shared-link access. Only hashes are kept in the server registry."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import urlsplit

COOKIE = '__Host-mianalog-access'


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def read_registry(config):
    try:
        value = json.loads((Path(config['data_dir'])/'web-access.json').read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def permission(config, token, now=None):
    if not config.get('web_access_restricted', False):
        return True, None
    if not isinstance(token, str) or len(token) != 43:
        return False, None
    now = time.time() if now is None else now
    registry = read_registry(config)
    hashed = digest(token)
    owner = registry.get('owner_hash', '')
    if isinstance(owner, str) and secrets.compare_digest(owner, hashed):
        return True, None
    guest = registry.get('guest_hash', '')
    expires = registry.get('guest_expires', 0)
    if (isinstance(guest, str) and isinstance(expires, (float, int))
            and expires > now and secrets.compare_digest(guest, hashed)):
        return True, expires
    return False, None


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(value.encode())
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def invite(config, base_url, output, owner_output=None, hours=12, now=None):
    parsed = urlsplit(base_url)
    if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Use an HTTPS origin, without path, query, or fragment')
    if not 0 < hours <= 24*30:
        raise ValueError('Hours must be between 0 and 720')
    now = time.time() if now is None else now
    root = Path(config['data_dir'])
    root.mkdir(parents=True, exist_ok=True)
    with (root/'web-access.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = read_registry(config)
        if not registry.get('owner_hash'):
            if owner_output is None:
                raise ValueError('First invitation needs --owner-output for permanent owner access')
            owner = secrets.token_urlsafe(32)
            private_write(owner_output, base_url.rstrip('/')+'/enter#access='+owner+'\n')
            registry['owner_hash'] = digest(owner)
        guest = secrets.token_urlsafe(32)
        expires = now+hours*3600
        registry.update(guest_hash=digest(guest), guest_expires=expires)
        private_write(output, base_url.rstrip('/')+'/enter#access='+guest+'\n')
        private_write(root/'web-access.json', json.dumps(registry)+'\n')
        return expires
