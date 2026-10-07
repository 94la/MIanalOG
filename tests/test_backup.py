import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from mtanalog.backup import backup
from mtanalog.storage import Store


class BackupTests(unittest.TestCase):
    def test_consistent_backup_excludes_credentials_and_rotates(self):
        with tempfile.TemporaryDirectory() as root:
            project=Path(root);data=project/'data'
            (project/'README.md').write_text('source')
            writer=Store(data);writer.meta('test',42);writer.flush()
            (data/'private.json').write_text('never include')
            config={'data_dir':str(data),'min_free_gib':0}
            with patch('mtanalog.backup.subprocess.check_output',return_value=b'README.md\0data/private.json\0'):
                for _ in range(4):target=backup(config,project)
            with sqlite3.connect(target/'archive.sqlite') as db:
                self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
                self.assertEqual(json.loads(db.execute("select value from meta where key='test'").fetchone()[0]),42)
            with tarfile.open(target/'sources.tar.gz') as archive:self.assertEqual(archive.getnames(),['README.md'])
            self.assertEqual(len([p for p in (data/'backups').iterdir() if p.is_dir()]),3)
            writer.close()
