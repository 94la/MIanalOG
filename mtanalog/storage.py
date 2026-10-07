"""Compressed raw feeds, SQLite aggregates and retention anchors."""
import fcntl
import gzip
import logging
import shutil
import json
import os
import sqlite3
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path


def pack(value):
    return zlib.compress(json.dumps(value, separators=(',', ':')).encode())


def unpack(value):
    return json.loads(zlib.decompress(value))


LOG = logging.getLogger(__name__)


def raw_paths(root):
    paths = list(Path(root).glob('*.jsonl.gz'))
    def key(path):
        parts = path.name.split('.')
        return int(parts[0]), int(parts[1]) if parts[1].isdigit() else 0
    compact = {}
    for path in paths:
        parts = path.name.split('.')
        if len(parts) > 4 and parts[2] == 'compact':
            hour = int(parts[0])
            end = int(parts[3])
            if hour not in compact or end > compact[hour][0]:
                compact[hour] = (end, path)
    # Manifest in the filename makes interrupted compaction cleanup idempotent.
    return sorted((p for p in paths if key(p)[0] not in compact
                   or p == compact[key(p)[0]][1]
                   or key(p)[1] > compact[key(p)[0]][0]), key=key)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    def __init__(self, root, read_only=False):
        self.root = Path(root)
        self.read_only = read_only
        self.lock = None
        self.raw_pending = []
        self.raw_hour = None
        self.compacted_hour = None
        if read_only:
            self.db = sqlite3.connect(f'file:{(self.root/"archive.sqlite").resolve()}?mode=ro',
                                      uri=True, timeout=10)
            self.db.execute('BEGIN')
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root/'collector.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('Archive writer is already running') from None
        for directory in ('raw', 'checkpoints'):
            (self.root / directory).mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / 'archive.sqlite', timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS liquidity(
            minute INTEGER PRIMARY KEY, covered_ms INTEGER NOT NULL,
            low REAL, high REAL, bins BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS cvd(
            minute INTEGER NOT NULL, cohort INTEGER NOT NULL, delta INTEGER NOT NULL,
            count INTEGER NOT NULL, PRIMARY KEY(minute,cohort));
        CREATE TABLE IF NOT EXISTS prices(
            minute INTEGER PRIMARY KEY, time INTEGER NOT NULL, price REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS gaps(
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, start INTEGER NOT NULL,
            end INTEGER, detail TEXT NOT NULL);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(liquidity)')}
        for column in ('first_ms', 'last_ms'):
            if column not in columns:
                self.db.execute(f'ALTER TABLE liquidity ADD COLUMN {column} INTEGER')
        self.db.commit()
        orphans = list((self.root/'raw').glob('*.tmp'))
        if orphans:
            quarantine = self.root/'raw-recovery'
            quarantine.mkdir(exist_ok=True)
            for path in orphans:
                os.replace(path, quarantine/(path.name+'.'+str(time.time_ns())))
            self.meta('raw_incomplete_segments', self.get_meta('raw_incomplete_segments', 0)+len(orphans))
            self.db.commit()
            LOG.warning('Isolated %s incomplete raw segments from an interrupted write', len(orphans))

    def get_meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def meta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))

    def raw(self, kind, payload, received_ms=None):
        received_ms = int(time.time()*1000) if received_ms is None else received_ms
        hour = received_ms // 3_600_000 * 3_600_000
        self.raw_hour = hour
        self.raw_pending.append((hour, json.dumps(
            {'received_ms': received_ms, 'kind': kind, 'data': payload},
            separators=(',', ':'))+'\n'))

    def _seal_raw(self):
        if not self.raw_pending:
            return
        batches = {}
        for hour, line in self.raw_pending:
            batches.setdefault(hour, []).append(line)
        for hour, lines in batches.items():
            target = self.root/'raw'/f'{hour}.{time.time_ns()}.jsonl.gz'
            temporary = target.with_suffix('.tmp')
            with temporary.open('xb') as output:
                output.write(gzip.compress(''.join(lines).encode(), compresslevel=3, mtime=0))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        sync_directory(self.root/'raw')
        self.raw_pending.clear()

    def compact_raw(self):
        # Only closed atomic segments; never append to a possibly torn gzip member.
        groups = {}
        for path in raw_paths(self.root/'raw'):
            hour = int(path.name.split('.')[0])
            if hour != self.raw_hour:
                groups.setdefault(hour, []).append(path)
        for hour, paths in groups.items():
            if len(paths) < 2:
                continue
            # Keep the first ordering key so a interrupted cleanup never reorders records.
            first_key = paths[0].name.split('.')[1]
            key = int(first_key) if first_key.isdigit() else 0
            last_parts = paths[-1].name.split('.')
            end_key = int(last_parts[3]) if len(last_parts)>4 and last_parts[2]=='compact' else int(last_parts[1]) if last_parts[1].isdigit() else 0
            target = self.root/'raw'/f'{hour}.{key}.compact.{end_key}.jsonl.gz'
            temporary = target.with_suffix('.tmp')
            with temporary.open('wb') as output:
                for source in paths:
                    with source.open('rb') as stream:
                        shutil.copyfileobj(stream, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            sync_directory(target.parent)
            for source in paths:
                if source != target:
                    source.unlink()
            sync_directory(target.parent)

    def checkpoint(self, book, received_ms, low, high):
        self.flush()  # Raw becomes durable before DB/checkpoint publication.
        payload = {'received_ms': received_ms, 'known_low': low,
                   'known_high': high, 'book': book}
        target = self.root / 'checkpoints' / f'{received_ms}.json.gz'
        temporary = target.with_suffix('.tmp')
        with gzip.open(temporary, 'wt', encoding='utf-8') as stream:
            json.dump(payload, stream, separators=(',', ':'))
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        sync_directory(target.parent)

    def save_minute(self, minute, bins, covered_ms, low, high, first_ms=None, last_ms=None):
        self.db.execute('INSERT OR REPLACE INTO liquidity '
                        '(minute,covered_ms,low,high,bins,first_ms,last_ms) VALUES(?,?,?,?,?,?,?)',
                        (minute, covered_ms, low, high, pack(bins),
                         first_ms if first_ms is not None else minute,
                         last_ms if last_ms is not None else minute + covered_ms))

    def load_minute(self, minute):
        row = self.db.execute('SELECT bins,covered_ms,low,high,first_ms,last_ms '
                              'FROM liquidity WHERE minute=?', (minute,)).fetchone()
        if row:
            compressed, duration, low, high, first, last = row
            return unpack(compressed), duration, low, high, first or minute, last or minute+duration
        return None

    def trade(self, event, cohort, delta):
        last = self.get_meta('last_trade_id')
        if last is not None and event['a'] <= last:
            return False
        minute = event['T'] // 60000 * 60000
        self.db.execute('''INSERT INTO cvd VALUES(?,?,?,1)
            ON CONFLICT(minute,cohort) DO UPDATE SET
            delta=delta+excluded.delta, count=count+1''', (minute, cohort, delta))
        self.db.execute('''INSERT INTO prices VALUES(?,?,?)
            ON CONFLICT(minute) DO UPDATE SET time=excluded.time,price=excluded.price
            WHERE excluded.time>=prices.time''', (minute, event['T'], float(event['p'])))
        self.meta('last_trade_id', event['a'])
        self.meta('last_trade_ms', event['T'])
        return True

    def gap(self, kind, start, end=None, detail=''):
        cursor = self.db.execute('INSERT INTO gaps(kind,start,end,detail) VALUES(?,?,?,?)',
                                 (kind, start, end, detail))
        return cursor.lastrowid

    def close_gap(self, gap_id, end):
        self.db.execute('UPDATE gaps SET end=? WHERE id=?', (end, gap_id))

    def flush(self):
        if self.read_only:
            return
        self._seal_raw()
        self.db.commit()
        if self.raw_hour != self.compacted_hour:
            self.compact_raw()
            self.compacted_hour = self.raw_hour

    def prune(self, days, now_ms=None):
        self.flush()
        cutoff = (now_ms or int(time.time() * 1000)) - days * 86_400_000
        # Retain the last checkpoint at/before cutoff and raw hour containing it.
        checkpoints = sorted((int(p.name.split('.')[0]), p)
                             for p in (self.root / 'checkpoints').glob('*.json.gz'))
        anchors = [entry for entry in checkpoints if entry[0] <= cutoff]
        anchor = anchors[-1][0] if anchors else None
        for timestamp, path in checkpoints:
            if timestamp < cutoff and timestamp != anchor:
                path.unlink()
        raw_cutoff = (anchor if anchor is not None else cutoff) // 3_600_000 * 3_600_000
        for path in (self.root / 'raw').glob('*.jsonl.gz'):
            if int(path.name.split('.')[0]) < raw_cutoff:
                path.unlink()
        for path in (self.root/'raw-recovery').glob('*'):
            prefix = path.name.split('.')[0]
            if path.is_file() and prefix.isdigit() and int(prefix) < raw_cutoff:
                path.unlink()
        for table in ('liquidity', 'cvd', 'prices'):
            self.db.execute(f'DELETE FROM {table} WHERE minute < ?', (cutoff // 60000 * 60000,))
        self.db.execute('DELETE FROM gaps WHERE end IS NOT NULL AND end < ?', (cutoff,))
        self.flush()

    def close(self):
        try:
            self.flush()
        finally:
            self.db.close()
            if self.lock:
                self.lock.close()

    def status(self):
        def utc(timestamp):
            return datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat() if timestamp else None
        rows = self.db.execute('SELECT min(minute),max(minute),count(*) FROM liquidity').fetchone()
        return {'first_map_utc': utc(rows[0]), 'last_map_utc': utc(rows[1]),
                'map_minutes': rows[2], 'last_trade_id': self.get_meta('last_trade_id'),
                'last_trade_utc': utc(self.get_meta('last_trade_ms')),
                'collector': self.get_meta('collector'),
                'raw_incomplete_segments': self.get_meta('raw_incomplete_segments', 0),
                'gaps': self.db.execute('SELECT kind,count(*) FROM gaps GROUP BY kind').fetchall()}
