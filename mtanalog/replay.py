"""Verify raw depth/trade replay independently against durable derived data."""
import gzip
import json
from pathlib import Path
import sqlite3
import zlib
from .storage import raw_paths
from .core import Book, SequenceGap, trade_values


def verify(config):
    root = Path(config['data_dir'])
    checkpoints = sorted((root/'checkpoints').glob('*.json.gz'), key=lambda p: int(p.name.split('.')[0]))
    if not checkpoints:
        raise ValueError('No checkpoint yet')
    with gzip.open(checkpoints[-1], 'rt') as stream:
        latest = json.load(stream)
    limit = latest['book']['lastUpdateId']
    book = Book(config['base_price_step'])
    deltas = {}
    last_trade_id = -1
    trade_count = 0
    applied = gaps = snapshots = 0
    complete = False
    truncated = False
    for path in raw_paths(root/'raw'):
        try:
            with gzip.open(path, 'rt') as stream:
                for line in stream:
                    record = json.loads(line)
                    event = record['data']
                    kind = record['kind']
                    if kind == 'snapshot' and event['lastUpdateId'] <= limit:
                        book.snapshot(event)
                        complete = True
                        snapshots += 1
                    elif kind == 'depth' and complete and event['u'] <= limit:
                        try:
                            applied += book.apply(event)
                        except SequenceGap:
                            complete = False
                            gaps += 1
                    elif kind.startswith('aggTrade') and event['a'] > last_trade_id:
                        last_trade_id = event['a']
                        trade_count += 1
                        cohort, delta = trade_values(event)
                        key = (event['T'] // 60000 * 60000, cohort)
                        deltas[key] = deltas.get(key, 0) + delta
        except (EOFError, gzip.BadGzipFile, zlib.error, json.JSONDecodeError):
            # The active gzip member may not have a footer yet. Report, never hide it.
            truncated = True
    # A boundary checkpoint is necessary once the initial raw snapshot has expired.
    if not complete or book.update_id != limit:
        with gzip.open(checkpoints[0], 'rt') as stream:
            anchor = json.load(stream)
        book.snapshot(anchor['book'])
        complete = True
        applied = 0
        for path in raw_paths(root/'raw'):
            try:
                with gzip.open(path, 'rt') as stream:
                    for line in stream:
                        record = json.loads(line)
                        event = record['data']
                        if record['kind'] == 'snapshot' and book.update_id < event['lastUpdateId'] <= limit:
                            book.snapshot(event)
                            complete = True
                        elif record['kind'] == 'depth' and complete and book.update_id < event['u'] <= limit:
                            try:
                                applied += book.apply(event)
                            except SequenceGap:
                                complete = False
            except (EOFError, gzip.BadGzipFile, zlib.error, json.JSONDecodeError):
                truncated = True
    expected = Book(config['base_price_step'])
    expected.snapshot(latest['book'])
    book_matches = (complete and book.update_id == limit and
                    book.bids == expected.bids and book.asks == expected.asks)
    db = sqlite3.connect(f'file:{(root/"archive.sqlite").resolve()}?mode=ro', uri=True)
    stored = {(minute, cohort): delta for minute, cohort, delta in db.execute('SELECT minute,cohort,delta FROM cvd')}
    db.close()
    compared = len(stored)
    cvd_matches = bool(stored) and all(deltas.get(key) == value for key, value in stored.items())
    return {'book_matches_checkpoint': book_matches, 'cvd_matches_sqlite': cvd_matches,
            'replayed_depth_updates': applied, 'raw_snapshots': snapshots,
            'unique_trades': trade_count, 'compared_cvd_rows': compared,
            'sequence_gaps_before_resnapshot': gaps, 'active_or_truncated_gzip': truncated,
            'checkpoint_update_id': limit,
            'quarantined_raw_segments': len(list((root/'raw-recovery').glob('*')))}
