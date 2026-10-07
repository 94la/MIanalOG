import gzip
import json
from pathlib import Path
import tempfile
import unittest

from mtanalog.core import Book, MinuteMap, SequenceGap, trade_values
from mtanalog.storage import Store, unpack


def snapshot():
    return {'lastUpdateId': 100, 'bids': [['100', '2'], ['90', '3']],
            'asks': [['110', '4'], ['120', '5']]}


class BookTests(unittest.TestCase):
    def test_overlap_absolute_quantity_duplicate_and_zero(self):
        book = Book(10)
        book.snapshot(snapshot())
        event = {'U': 99, 'u': 102, 'b': [['100', '7']], 'a': [['120', '0']]}
        self.assertTrue(book.apply(event))
        self.assertEqual(book.bids[10000], 7)
        self.assertNotIn(12000, book.asks)
        self.assertAlmostEqual(book.bins[('b', 10000)], 700)
        self.assertFalse(book.apply(event))
        self.assertEqual(book.bids[10000], 7)

    def test_gap_does_not_mutate_book(self):
        book = Book()
        book.snapshot(snapshot())
        before = book.export()
        with self.assertRaises(SequenceGap):
            book.apply({'U': 102, 'u': 104, 'b': [['50', '200']], 'a': []})
        self.assertEqual(book.export(), before)

    def test_far_level_retained_and_invalidated_by_new_snapshot(self):
        book = Book()
        book.snapshot(snapshot())
        book.apply({'U': 101, 'u': 101, 'a': [['1000', '10']], 'b': []})
        self.assertEqual(book.asks[100000], 10)
        self.assertEqual(book.high, 120)
        book.snapshot({**snapshot(), 'lastUpdateId': 200})
        self.assertNotIn(100000, book.asks)

    def test_neighbours_aggregate_before_display_filter(self):
        book = Book(25)
        book.snapshot({'lastUpdateId': 1, 'bids': [['100', '6000'], ['101', '5000']],
                       'asks': [['130', '1']]})
        self.assertAlmostEqual(book.bins[('b', 10000)], 1_105_000)


class TradeTests(unittest.TestCase):
    def test_boundary_direction_and_exact_units(self):
        for value, group in [('99.99', 0), ('100', 1), ('1000', 2),
                             ('10000', 3), ('100000', 4), ('1000000', 5), ('10000000', 6)]:
            result = trade_values({'p': value, 'q': '1', 'm': False})
            self.assertEqual(result[0], group)
        self.assertEqual(trade_values({'p': '100.01', 'q': '0.1', 'm': True}), (0, -10001000))

    def test_restart_dedup_and_total_equals_cohorts(self):
        with tempfile.TemporaryDirectory() as root:
            events = [{'a': i, 'p': p, 'q': '1', 'm': bool(i % 2), 'T': i*1000}
                      for i, p in enumerate(['99', '100', '1000', '10000', '100000', '1000000', '10000000'], 1)]
            store = Store(root)
            expected = 0
            for event in events:
                cohort, delta = trade_values(event)
                expected += delta
                self.assertTrue(store.trade(event, cohort, delta))
            store.close()
            store = Store(root)
            self.assertFalse(store.trade(events[-1], *trade_values(events[-1])))
            total, count = store.db.execute('SELECT sum(delta),sum(count) FROM cvd').fetchone()
            self.assertEqual(total, expected)
            self.assertEqual(count, 7)
            store.close()


class TimeAndRetentionTests(unittest.TestCase):
    def test_partial_minute_restart_preserves_previous_segment(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            book = Book(10)
            book.snapshot(snapshot())
            first = MinuteMap(store)
            first.update(1000, book)
            first.persist(11000)
            second = MinuteMap(store)
            book.apply({'U': 101, 'u': 101, 'b': [['100', '6']], 'a': []})
            second.update(21000, book)
            second.persist(31000)
            second.persist(41000)
            duration, compressed, begin, end = store.db.execute(
                'SELECT covered_ms,bins,first_ms,last_ms FROM liquidity').fetchone()
            self.assertEqual((duration, begin, end), (30000, 1000, 41000))
            value = next(v for s, p, v in unpack(compressed) if s == 'b' and p == 10000)
            self.assertAlmostEqual(value, (200*10 + 600*20)/30)
            store.close()

    def test_wall_average_is_duration_weighted_not_update_count(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            book = Book(10)
            book.snapshot(snapshot())
            chart = MinuteMap(store)
            chart.update(0, book)
            # $200 for 10s, then $600 for 50s => $533.333, regardless of message count.
            chart.advance(10000)
            book.apply({'U': 101, 'u': 101, 'b': [['100', '6']], 'a': []})
            chart.update(10000, book)
            for t in (12000, 20000, 35000, 45000):
                chart.update(t, book)
            chart.persist(60000)
            minute, duration, compressed = store.db.execute('SELECT minute,covered_ms,bins FROM liquidity').fetchone()
            value = next(v for s, p, v in unpack(compressed) if s == 'b' and p == 10000)
            self.assertAlmostEqual(value, 533.3333333333)
            self.assertEqual(duration, 60000)
            store.close()

    def test_disappearance_and_gap_do_not_extend_wall(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            book = Book(10)
            book.snapshot(snapshot())
            chart = MinuteMap(store)
            chart.update(0, book)
            chart.advance(10000)
            book.apply({'U': 101, 'u': 101, 'b': [['100', '0']], 'a': []})
            chart.update(10000, book)
            chart.update(30000, None)
            chart.persist(120000)
            rows = store.db.execute('SELECT minute,covered_ms,bins FROM liquidity').fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], 30000)
            value = next(v for s, p, v in unpack(rows[0][2]) if s == 'b' and p == 10000)
            self.assertAlmostEqual(value, 200 / 3)
            store.close()

    def test_retention_preserves_boundary_checkpoint_and_raw_hour(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            hour = 3600000
            now = 20 * 86400000 + hour // 2
            cutoff = now - 14 * 86400000
            anchor = cutoff - hour // 2
            book = Book()
            book.snapshot(snapshot())
            for stamp in (anchor-hour, anchor, anchor+hour):
                store.checkpoint(book.export(), stamp, book.low, book.high)
                store.raw('snapshot', book.export(), stamp)
            store.save_minute(anchor-hour, [], 60000, 90, 120)
            store.save_minute(anchor+hour, [], 60000, 90, 120)
            store.prune(14, now)
            checkpoints = [int(p.name.split('.')[0]) for p in (Path(root)/'checkpoints').glob('*.json.gz')]
            self.assertEqual(sorted(checkpoints), [anchor, anchor+hour])
            self.assertFalse(list((Path(root)/'raw').glob(f'{anchor-hour}.*.jsonl.gz')))
            self.assertTrue(list((Path(root)/'raw').glob(f'{anchor}.*.jsonl.gz')))
            self.assertEqual(store.db.execute('SELECT count(*) FROM liquidity').fetchone()[0], 1)
            store.close()


if __name__ == '__main__':
    unittest.main()
