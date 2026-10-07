import asyncio
import contextlib
import gzip
import json
import logging
import shutil
import signal
import time
import urllib.error
import urllib.parse
import urllib.request

from websockets.asyncio.client import connect

from .core import Book, MinuteMap, SequenceGap, trade_values
from .storage import Store

LOG = logging.getLogger(__name__)


def now_ms():
    return int(time.time() * 1000)


class RestError(Exception):
    def __init__(self, message, retry_after=5):
        super().__init__(message)
        self.retry_after = retry_after


def rest(config, path, **params):
    url = config['rest_url'] + path + '?' + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Respect rate limits; never log credential-bearing headers or bodies.
        delay = int(error.headers.get('Retry-After', '60' if error.code in (418, 429) else '5'))
        raise RestError(f'Binance HTTP {error.code}: {path}', delay) from error


async def receive(config, queue, stop):
    """Proactive overlapping connection rollover preserves sequence continuity."""
    async def open_socket():
        return await connect(config['stream_url'], ping_interval=None, open_timeout=20,
                             max_size=8 * 1024 * 1024, max_queue=4096, close_timeout=5)

    ws = await open_socket()
    rollover = None
    opened = time.monotonic()
    try:
        while not stop.is_set():
            if time.monotonic() - opened > 23 * 3600 and rollover is None:
                rollover = asyncio.create_task(open_socket())
            if rollover is not None and rollover.done():
                try:
                    replacement = rollover.result()
                except Exception:
                    LOG.exception('Rollover connection failed; retrying in 60 seconds')
                    opened = time.monotonic() - 23 * 3600 + 60
                else:
                    await ws.close()
                    ws = replacement
                    opened = time.monotonic()
                rollover = None
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except TimeoutError:
                continue
            event = json.loads(raw).get('data', {})
            if event.get('e') == 'serverShutdown':
                opened = time.monotonic() - 23 * 3600 - 1
                continue
            if event.get('e') in ('depthUpdate', 'aggTrade'):
                # Bounded buffer: overflow is a detected failure, never silent loss.
                queue.put_nowait((now_ms(), event))
    finally:
        if rollover:
            if rollover.done() and not rollover.cancelled():
                with contextlib.suppress(Exception):
                    await rollover.result().close()
            else:
                rollover.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await rollover
        await ws.close()


async def collect(config, duration=None):
    store = Store(config['data_dir'])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    deadline = time.monotonic() + duration if duration else float('inf')
    book = Book(config['base_price_step'])
    base_step = store.get_meta('base_price_step')
    if base_step is not None and base_step != config['base_price_step']:
        store.close()
        raise ValueError('base_price_step differs from the existing archive')
    store.meta('base_price_step', config['base_price_step'])
    chart = MinuteMap(store)
    last_depth = None
    depth_gap = None
    # Recover last durable state only when the incoming sequence still overlaps it.
    saved = sorted((store.root / 'checkpoints').glob('*.json.gz'), key=lambda p: int(p.name.split('.')[0]))
    if saved:
        try:
            with gzip.open(saved[-1], 'rt') as stream:
                checkpoint = json.load(stream)
            book.snapshot(checkpoint['book'])
            book.low, book.high = checkpoint['known_low'], checkpoint['known_high']
            last_depth = checkpoint['received_ms']
        except (OSError, ValueError, KeyError):
            LOG.exception('Checkpoint unavailable; will initialize from REST')
            book = Book(config['base_price_step'])
    latest_state = store.get_meta('collector', {})
    start_gap = latest_state.get('last_depth_ms') or last_depth
    if start_gap:
        depth_gap = store.gap('depth', start_gap, detail='collector restart / continuity pending')
    last_checkpoint = last_flush = last_prune = 0
    events = trades = 0
    backoff = 1

    async def process_trade(event, received, recovered=False):
        nonlocal trades
        cohort, delta = trade_values(event)
        if store.trade(event, cohort, delta):
            store.raw('aggTrade_recovered' if recovered else 'aggTrade', event, received)
            trades += 1

    async def fill_trades(event, received):
        last = store.get_meta('last_trade_id')
        if last is not None and event['a'] > last + 1:
            pending = store.get_meta('pending_cvd_recovery')
            if pending:
                gap_id = pending['id']
                store.db.execute('UPDATE gaps SET end=?,detail=? WHERE id=?',
                    (event['T'], f'missing aggregate IDs {pending["first_id"]}..{event["a"]-1}', gap_id))
            else:
                gap_id = store.gap('cvd', store.get_meta('last_trade_ms'),
                                   event['T'], f'missing aggregate IDs {last + 1}..{event["a"] - 1}')
                store.meta('pending_cvd_recovery', {'id': gap_id, 'first_id': last + 1})
            LOG.warning('Recovering aggregate trades %s..%s', last + 1, event['a'] - 1)
            cursor = last + 1
            # A two-week offline gap must not trigger unlimited REST requests.
            for _ in range(200):
                batch = await asyncio.to_thread(rest, config, '/api/v3/aggTrades',
                                               symbol=config['symbol'], fromId=cursor, limit=1000)
                needed = [row for row in batch if cursor <= row['a'] < event['a']]
                if not needed or needed[0]['a'] != cursor:
                    break
                for row in needed:
                    if row['a'] != cursor:
                        break
                    await process_trade(row, received, recovered=True)
                    cursor += 1
                store.flush()  # Bound recovery buffers and make progress durable.
                if cursor >= event['a']:
                    store.db.execute('DELETE FROM gaps WHERE id=?', (gap_id,))
                    store.meta('pending_cvd_recovery', None)
                    break
                await asyncio.sleep(.1)
            if cursor < event['a']:
                LOG.error('Unrecovered CVD gap remains recorded at ID %s', cursor)
                store.meta('pending_cvd_recovery', None)
        await process_trade(event, received)

    try:
        while not stop.is_set() and time.monotonic() < deadline:
            queue = asyncio.Queue(maxsize=20000)
            reader = asyncio.create_task(receive(config, queue, stop))
            snapshot_task = None
            ready = False
            snapshot_ready_ms = 0
            try:
                while not stop.is_set() and time.monotonic() < deadline:
                    if reader.done():
                        await reader
                        raise ConnectionError('Market stream ended')
                    try:
                        received, event = await asyncio.wait_for(queue.get(), timeout=1)
                    except TimeoutError:
                        received, event = now_ms(), None
                    if event and event['e'] == 'aggTrade':
                        await fill_trades(event, received)
                    elif event and event['e'] == 'depthUpdate':
                        store.raw('depth', event, received)
                        if not ready:
                            if book.update_id and event['U'] <= book.update_id + 1 <= event['u']:
                                ready = True
                            elif book.update_id and event['u'] <= book.update_id:
                                continue
                            else:
                                if snapshot_task is None:
                                    snapshot_task = asyncio.create_task(asyncio.to_thread(
                                        rest, config, '/api/v3/depth', symbol=config['symbol'], limit=5000))
                                # Wait while reader buffers both streams. Process this event again.
                                snapshot = await snapshot_task
                                snapshot_task = None
                                snapshot_ready_ms = now_ms()
                                book.snapshot(snapshot)
                                store.raw('snapshot', snapshot, now_ms())
                                store.checkpoint(book.export(), now_ms(), book.low, book.high)
                                if event['u'] <= book.update_id:
                                    continue
                                if event['U'] > book.update_id + 1:
                                    raise SequenceGap('Snapshot does not overlap buffered depth')
                                ready = True
                            if depth_gap:
                                store.close_gap(depth_gap, max(received, snapshot_ready_ms))
                                depth_gap = None
                            chart.update(max(received, snapshot_ready_ms), book)
                            backoff = 1
                            LOG.info('Book synchronized at %s; known initial band %.2f..%.2f',
                                     book.update_id, book.low, book.high)
                        chart.advance(received)
                        if book.apply(event):
                            chart.update(received, book)
                            last_depth = received
                            events += 1
                    current = now_ms()
                    # Detect a stalled depth feed even if trades keep arriving.
                    if ready and last_depth and queue.empty() and current - last_depth > 15000:
                        raise ConnectionError('No depth update for 15 seconds')
                    if current - last_flush >= 5000:
                        if ready:
                            # No wall-clock advancement across buffered depth events.
                            horizon = received if not queue.empty() else current
                            chart.persist(max(chart.last_ms or horizon, horizon))
                        store.meta('collector', {'state': 'collecting' if ready else 'synchronizing',
                            'heartbeat_ms': current, 'last_depth_ms': last_depth,
                            'depth_events_this_run': events, 'trades_this_run': trades,
                            'queue_events': queue.qsize(), 'processing_lag_ms': max(0, current-received),
                            'bid_levels': len(book.bids), 'ask_levels': len(book.asks),
                            'known_low': book.low, 'known_high': book.high})
                        store.flush()
                        last_flush = current
                    if ready and current - last_checkpoint >= config['checkpoint_seconds'] * 1000:
                        store.checkpoint(book.export(), current, book.low, book.high)
                        last_checkpoint = current
                    if current - last_prune >= 3600000:
                        store.prune(config['retention_days'], current)
                        last_prune = current
                        if shutil.disk_usage(store.root).free < config['min_free_gib'] * 2**30:
                            raise OSError('Free disk below configured safety floor')
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.warning('Stream interrupted: %s', error)
                current = now_ms()
                # Last valid depth is the conservative end of known coverage.
                chart.update(max(chart.last_ms or current, last_depth or current), None)
                if depth_gap is None:
                    depth_gap = store.gap('depth', last_depth or current, detail=str(error))
                store.meta('collector', {'state': 'reconnecting', 'heartbeat_ms': current,
                                         'last_depth_ms': last_depth, 'error': str(error)})
                store.flush()
                if isinstance(error, OSError) and shutil.disk_usage(store.root).free < config['min_free_gib'] * 2**30:
                    stop.set()
                delay = max(backoff, getattr(error, 'retry_after', 0))
                if not stop.is_set() and time.monotonic() < deadline:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=min(delay, max(0, deadline-time.monotonic())))
                    except TimeoutError:
                        pass
                backoff = min(60, backoff * 2)
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader
                if snapshot_task:
                    snapshot_task.cancel()
    finally:
        # Unprocessed buffered updates are unknown after a stop, not standing liquidity.
        if chart.valid:
            chart.update(max(chart.last_ms or 0, last_depth or 0), None)
        chart.persist(now_ms())
        if book.update_id:
            store.checkpoint(book.export(), now_ms(), book.low, book.high)
        store.meta('collector', {'state': 'stopped', 'heartbeat_ms': now_ms(),
                                 'last_depth_ms': last_depth})
        store.close()
