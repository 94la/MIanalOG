"""Read-only chart snapshots from the time-weighted minute archive."""
import json
import math
from pathlib import Path
import sqlite3
import time

import numpy as np

from .storage import unpack

PRESETS = {'1d': (1, 5), '1w': (7, 10)}


def price_settings(config, mode, step=None, margin=None):
    if mode not in PRESETS:
        raise ValueError('Unknown timeframe')
    step = config['price_step'] if step is None else step
    margin = PRESETS[mode][1] if margin is None else margin
    base = config.get('base_price_step', 25)
    if (not isinstance(step, int) or not base <= step <= 2000 or step % base
            or not math.isfinite(margin) or not .5 <= margin <= 20
            or not math.isclose(margin*2, round(margin*2), abs_tol=1e-9)):
        raise ValueError('Invalid price settings')
    return step, round(margin*2)/2


def chart_data(config, mode='1d', end_ms=None, price_step=None, margin_pct=None):
    if mode not in PRESETS:
        raise ValueError('Unknown timeframe')
    days, _ = PRESETS[mode]
    step, margin = price_settings(config, mode, price_step, margin_pct)
    end = end_ms or int(time.time() * 1000)
    requested_start = end - days * 86400000
    path = Path(config['data_dir']) / 'archive.sqlite'
    db = sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True, timeout=10)
    try:
        db.execute('BEGIN')
        first = db.execute('SELECT min(minute) FROM liquidity WHERE minute>=? AND minute<?',
                           (requested_start // 60000 * 60000, end)).fetchone()[0]
        if first is None:
            return {'empty': True, 'mode': mode, 'generated_ms': end}
        start = max(requested_start, first)
        prices = db.execute('SELECT minute,time,price FROM prices WHERE minute>=? AND minute<? ORDER BY minute',
                            (start // 60000 * 60000, end)).fetchall()
        if not prices:
            return {'empty': True, 'mode': mode, 'generated_ms': end}
        low = math.floor(min(p for _, _, p in prices) * (1-margin/100) / step) * step
        high = math.ceil(max(p for _, _, p in prices) * (1+margin/100) / step) * step
        rows = int(round((high-low)/step))
        columns = min(960, max(1, math.ceil((end-start)/60000)))
        if rows * columns > 2_000_000:
            raise ValueError('Chart matrix too large')
        edges = np.linspace(start, max(end, start+1), columns+1)
        amounts = np.zeros((columns, rows))
        durations = np.zeros(columns)
        known = np.zeros((columns, 2))
        for minute, covered, lower, upper, packed, begin, finish in db.execute(
                'SELECT minute,covered_ms,low,high,bins,first_ms,last_ms FROM liquidity '
                'WHERE minute>=? AND minute<? ORDER BY minute', (start//60000*60000, end)):
            begin = begin if begin is not None else minute
            finish = finish if finish is not None else minute+covered
            a, b = max(start, begin), min(end, finish)
            if b <= a:
                continue
            grouped = np.zeros(rows)
            for _, price, value in unpack(packed):
                row = (price // (step*100) * step-low) // step
                if 0 <= row < rows:
                    grouped[row] += value
            left = max(0, int(np.searchsorted(edges, a, side='right'))-1)
            right = min(columns-1, int(np.searchsorted(edges, b, side='left'))-1)
            for column in range(left, right+1):
                weight = max(0, min(b, edges[column+1])-max(a, edges[column]))
                weight *= min(1, covered/max(1, finish-begin))
                amounts[column] += grouped*weight
                durations[column] += weight
                known[column] += np.array([lower, upper])*weight
        populated = durations > 0
        amounts[populated] /= durations[populated, None]
        known[populated] /= durations[populated, None]
        # Sparse arrays retain ALL volumes; browser applies the requested filter.
        liquidity = [None if not populated[c] else
                     [[r, round(float(v), 2)] for r, v in enumerate(amounts[c]) if v > .01]
                     for c in range(columns)]
        deltas = np.zeros((columns, 7))
        for minute, cohort, delta in db.execute('SELECT minute,cohort,delta FROM cvd WHERE minute>=? '
                                              'AND minute<?', (start//60000*60000, end)):
            col = min(columns-1, max(0, int(np.searchsorted(edges, minute, side='right'))-1))
            deltas[col, cohort] += delta/1e6
        cvd = np.cumsum(np.column_stack([deltas.sum(axis=1), deltas]), axis=0)
        gaps = db.execute('SELECT kind,start,end FROM gaps WHERE start<? AND '
                          '(end IS NULL OR end>?)', (end, start)).fetchall()
        invalid = min((a for kind, a, _ in gaps if kind == 'cvd'), default=None)
        meta = dict(db.execute('SELECT key,value FROM meta WHERE key IN (\'collector\',\'last_trade_ms\')'))
        last_trade = json.loads(meta.get('last_trade_ms', '0'))
        cvd_rows = [None if (edges[c+1] <= prices[0][0] or edges[c] > last_trade
                            or invalid is not None and edges[c+1] > invalid)
                    else [round(float(v), 2) for v in cvd[c]] for c in range(columns)]
        collector = json.loads(meta.get('collector', '{}'))
        last_price = prices[-1][2]
        return {'empty': False, 'mode': mode, 'generated_ms': end,
                'requested_start_ms': requested_start, 'start_ms': start, 'end_ms': end,
                'price_margin_pct': margin, 'low': low, 'high': high, 'price_step': step,
                'rows': rows, 'columns': columns, 'liquidity': liquidity,
                'coverage': [round(min(1, float(d)/(edges[c+1]-edges[c])), 3)
                             for c, d in enumerate(durations)],
                'known': [None if not populated[c] else list(known[c]) for c in range(columns)],
                'prices': [[timestamp, price] for _, timestamp, price in prices],
                'cvd': cvd_rows, 'gaps': gaps, 'last_price': last_price,
                'price_change_pct': (last_price/prices[0][2]-1)*100,
                'collector': {'state': collector.get('state', 'unknown'),
                              'heartbeat_ms': collector.get('heartbeat_ms', 0),
                              'processing_lag_ms': collector.get('processing_lag_ms', 0),
                              'queue_events': collector.get('queue_events', 0)},
                'default_min_usdt': config['min_liquidity_usdt'],
                'aggregation': 'time-weighted mean', 'refresh_seconds': 15}
    finally:
        db.close()
