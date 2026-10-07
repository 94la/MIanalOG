import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mtanalog.storage import Store, raw_paths


class StorageSafetyTests(unittest.TestCase):
    def test_lock_precedes_sqlite_open_and_status_is_read_only(self):
        with tempfile.TemporaryDirectory() as root:
            writer=Store(root)
            writer.meta('collector', {'state':'collecting'})
            writer.flush()
            with patch('mtanalog.storage.sqlite3.connect') as connect:
                with self.assertRaises(RuntimeError):
                    Store(root)
                connect.assert_not_called()
            reader=Store(root,read_only=True)
            self.assertEqual(reader.get_meta('collector')['state'],'collecting')
            with self.assertRaises(Exception):
                reader.meta('x',1)
            reader.close();writer.close()

    def test_atomic_segments_restart_and_incomplete_tail_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            store.raw('first',{},1000);store.flush();store.close()
            original=raw_paths(Path(root)/'raw')[0]
            old=original.read_bytes()
            broken=original.with_name('0.1.jsonl.tmp')
            broken.write_bytes(gzip.compress(b'partial\n')[:-8])
            store=Store(root)
            self.assertEqual(store.get_meta('raw_incomplete_segments'),1)
            store.raw('second',{},2000);store.flush();store.close()
            self.assertEqual(original.read_bytes(),old)
            records=[]
            for path in raw_paths(Path(root)/'raw'):
                with gzip.open(path,'rt') as stream:
                    records.extend(json.loads(line)['kind'] for line in stream)
            self.assertEqual(records,['first','second'])
            self.assertFalse(broken.exists())
            self.assertEqual(len(list((Path(root)/'raw-recovery').iterdir())),1)

    def test_failed_raw_publication_does_not_commit_derived_trade(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            event={'a':1,'T':1000,'p':'100','q':'1','m':False}
            store.trade(event,1,100000000);store.raw('aggTrade',event,1000)
            with patch('mtanalog.storage.os.replace',side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):store.flush()
            reader=Store(root,read_only=True)
            self.assertIsNone(reader.get_meta('last_trade_id'))
            reader.close();store.close()

    def test_compaction_manifest_survives_interrupted_source_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            store.raw('one',{},1000);store.flush()
            store.raw('two',{},2000);store.flush()
            originals=[(path,path.read_bytes()) for path in raw_paths(Path(root)/'raw')]
            store.raw('next-hour',{},3600001);store.flush()
            for path,body in originals:path.write_bytes(body)  # Simulate crash before old-file unlink.
            records=[]
            for path in raw_paths(Path(root)/'raw'):
                with gzip.open(path,'rt') as stream:
                    records.extend(json.loads(line)['kind'] for line in stream)
            self.assertEqual(records,['one','two','next-hour'])
            store.close()
