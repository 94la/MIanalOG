import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from mtanalog.updates import delta
from mtanalog.web import ChartService


def sample(start=0,revision='a'*24):
    return dict(empty=False,revision=revision,mode='1d',low=100,high=300,rows=2,
                price_step=100,column_ms=120000,start_ms=start,end_ms=start+240000,
                columns=2,liquidity=[[[0,10]],[[1,20]]],known=[[100,300],[100,300]],
                coverage=[1,1],cvd=[[1]*8,[2]*8],candles=[dict(time=start//1000,open=100,high=110,low=90,close=105)],
                prices=[[start+1000,105]])

class UpdatesTests(unittest.TestCase):
    def test_tail_and_corrections(self):
        before=sample();after=copy.deepcopy(before);after['revision']='b'*24
        after['liquidity'][1]=[[1,21]];after['cvd'][1]=[3]*8
        after['candles'][0]['low']=80
        result=delta(before,after)
        self.assertEqual(result['patch']['liquidity'],[[1,[[1,21]]]])
        self.assertEqual(result['patch']['known'],[])
        self.assertEqual(result['series']['candles']['upsert'][0]['low'],80)

    def test_window_drop_and_geometry_fallback(self):
        before=sample();after=sample(120000,'b'*24)
        after['liquidity'][0]=before['liquidity'][1]
        result=delta(before,after)
        self.assertEqual(result['drop'],1)
        self.assertEqual(result['patch']['liquidity'],[[1,[[1,20]]]])
        self.assertEqual(result['series']['candles']['remove'],[0])
        after['low']=0
        self.assertIsNone(delta(before,after))

    def test_service_unchanged_delta_and_expired_base(self):
        service=ChartService({'price_step':200})
        original=sample();original['liquidity']=[[[i,100] for i in range(100)] for _ in range(2)]
        with patch('mtanalog.web.chart_data',return_value=original): first=json.loads(service.update('1d'))
        self.assertEqual(json.loads(service.update('1d',since=first['revision']))['type'],'unchanged')
        updated=copy.deepcopy(original);updated['liquidity'][1][0][1]=101
        service.cache.clear();service.cache_bytes=0
        with patch('mtanalog.web.chart_data',return_value=updated): result=json.loads(service.update('1d',since=first['revision']))
        self.assertEqual(result['type'],'delta')
        self.assertEqual(result['patch']['liquidity'][0][0],1)
        self.assertNotIn('type',json.loads(service.update('1d',since='c'*24)))
