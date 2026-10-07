"""Exchange-independent order book and exact trade classification."""
from collections import defaultdict
from decimal import Decimal


class SequenceGap(Exception):
    pass


def cents(price):
    return int(Decimal(str(price)) * 100)


class Book:
    def __init__(self, step=25):
        self.step = step * 100
        self.bids = {}
        self.asks = {}
        self.bins = defaultdict(float)
        self.update_id = 0
        self.low = self.high = None

    def _set(self, side, price, quantity):
        levels = self.bids if side == 'b' else self.asks
        p, q = cents(price), float(quantity)
        bucket = p // self.step * self.step
        key = (side, bucket)
        self.bins[key] += p / 100 * (q - levels.get(p, 0))
        if abs(self.bins[key]) < 1e-6:
            self.bins.pop(key, None)
        if q:
            levels[p] = q
        else:
            levels.pop(p, None)

    def snapshot(self, payload):
        self.bids.clear()
        self.asks.clear()
        self.bins.clear()
        for side, name in [('b', 'bids'), ('a', 'asks')]:
            for p, q in payload[name]:
                self._set(side, p, q)
        self.update_id = payload['lastUpdateId']
        self.low = min(self.bids) / 100 if self.bids else None
        self.high = max(self.asks) / 100 if self.asks else None

    def apply(self, event):
        if event['u'] <= self.update_id:
            return False
        if event['U'] > self.update_id + 1:
            raise SequenceGap(f"expected {self.update_id + 1}, received {event['U']}")
        for side in ('b', 'a'):
            for p, q in event[side]:
                self._set(side, p, q)
        self.update_id = event['u']
        return True

    def export(self):
        return {'lastUpdateId': self.update_id,
                'bids': [[str(p / 100), str(q)] for p, q in self.bids.items()],
                'asks': [[str(p / 100), str(q)] for p, q in self.asks.items()]}


# All trades are covered; displayed groups have explicit half-open intervals.
EDGES = tuple(Decimal(x) for x in ('100', '1000', '10000', '100000', '1000000', '10000000'))
LABELS = ('< $100', '$100–$1k', '$1k–$10k', '$10k–$100k',
          '$100k–$1M', '$1M–$10M', '≥ $10M')


def trade_values(trade):
    amount = Decimal(trade['p']) * Decimal(trade['q'])
    group = sum(amount >= edge for edge in EDGES)
    # Store signed millionths of a USDT; avoid accumulated binary float error.
    units = int((amount * 1_000_000).to_integral_value())
    return group, -units if trade['m'] else units


class MinuteMap:
    """Time-weighted minute summaries, never sums repeated standing liquidity."""
    def __init__(self, store):
        self.store = store
        self.last_ms = None
        self.minute = None
        self.sums = defaultdict(float)
        self.covered_ms = 0
        self.low_sum = self.high_sum = 0.0
        self.valid = False
        self.state = {}
        self.low = self.high = None
        self.coverage_start = self.coverage_end = None
        self.base = None

    def _flush(self):
        if self.minute is not None and self.covered_ms:
            sums = defaultdict(float, self.sums)
            duration = self.covered_ms
            low_sum, high_sum = self.low_sum, self.high_sum
            first, last = self.coverage_start, self.coverage_end
            if self.base:
                previous, base_duration, base_low, base_high, base_first, base_last = self.base
                for side, p, value in previous:
                    sums[(side, p)] += value * base_duration
                duration += base_duration
                low_sum += base_low * base_duration
                high_sum += base_high * base_duration
                first = min(first, base_first)
                last = max(last, base_last)
            rows = [[side, p, value / duration]
                    for (side, p), value in sums.items() if value > 1e-6]
            self.store.save_minute(self.minute, rows, duration,
                                   low_sum / duration, high_sum / duration, first, last)

    def advance(self, now_ms):
        if self.last_ms is None:
            self.last_ms = now_ms
            self.minute = now_ms // 60000 * 60000
            self.base = self.store.load_minute(self.minute)
            return
        now_ms = max(now_ms, self.last_ms)
        while self.last_ms < now_ms:
            end = min(now_ms, self.minute + 60000)
            duration = end - self.last_ms
            if self.valid:
                if self.coverage_start is None:
                    self.coverage_start = self.last_ms
                self.coverage_end = end
                for key, value in self.state.items():
                    self.sums[key] += max(0, value) * duration
                self.covered_ms += duration
                self.low_sum += self.low * duration
                self.high_sum += self.high * duration
            self.last_ms = end
            if end == self.minute + 60000:
                self._flush()
                self.minute += 60000
                self.sums.clear()
                self.covered_ms = 0
                self.low_sum = self.high_sum = 0.0
                self.coverage_start = self.coverage_end = None
                self.base = self.store.load_minute(self.minute)

    def update(self, now_ms, book=None):
        self.advance(now_ms)
        self.valid = book is not None
        self.state = dict(book.bins) if book else {}
        self.low = book.low if book else None
        self.high = book.high if book else None

    def persist(self, now_ms):
        self.advance(now_ms)
        self._flush()
