import json
import unittest
from unittest.mock import patch
from mtanalog.metrics import LoadMetrics

class MetricsTests(unittest.TestCase):
    def test_activity_expiry_errors_and_anonymity(self):
        metrics=LoadMetrics()
        visitor='12345678-1234-1234-1234-123456789abc'
        metrics.observe(200,20,100,visitor,now=600)
        metrics.observe(429,70,50,visitor,now=601)
        metrics.observe(503,0,0,rejected=True,now=602)
        with patch('mtanalog.metrics.time.time',return_value=603):
            result=metrics.snapshot()
        self.assertEqual(result['active_visitors_5m'],1)
        self.assertEqual(result['requests_5m'],3)
        self.assertEqual(result['errors_5m'],2)
        self.assertEqual(result['rejected_5m'],1)
        self.assertNotIn(visitor,json.dumps(result))
        with patch('mtanalog.metrics.time.time',return_value=903):
            self.assertEqual(metrics.snapshot()['active_visitors_5m'],0)

    def test_history_and_samples_are_bounded(self):
        metrics=LoadMetrics()
        for minute in range(1500):metrics.observe(200,1,1,now=minute*60)
        self.assertEqual(len(metrics.rows),1440)
        for i in range(2000):metrics.observe(200,i,1,now=1500*60)
        self.assertEqual(len(metrics.durations),1024)
