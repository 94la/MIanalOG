import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mtanalog import supervisor


class SupervisionTests(unittest.TestCase):
    def test_managed_start_uses_service_instead_of_second_process(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(supervisor, 'managed_service', return_value=True), \
                 patch.object(supervisor, 'service_action') as action, \
                 patch.object(supervisor.subprocess, 'check_output', return_value=b'123\n'), \
                 patch.object(supervisor.subprocess, 'Popen') as launch:
                self.assertEqual(supervisor.start({'data_dir': root}, Path(root)/'config.toml'), 123)
                action.assert_called_once_with('start')
                launch.assert_not_called()

    def test_managed_stop_stops_unit_so_restart_always_does_not_undo_it(self):
        with patch.object(supervisor, 'managed_service', return_value=True), \
             patch.object(supervisor, 'service_action') as action, \
             patch.object(supervisor.os, 'kill') as kill:
            self.assertTrue(supervisor.stop({'data_dir': '/unused'}))
            action.assert_called_once_with('stop')
            kill.assert_not_called()
