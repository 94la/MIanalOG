import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
from urllib.parse import urlsplit, parse_qs
from http.server import ThreadingHTTPServer

from mtanalog.access import COOKIE, invite, permission
from mtanalog.web import ChartService, handler_for


def token(path):
    return parse_qs(urlsplit(Path(path).read_text().strip()).fragment)['access'][0]


class AccessTests(unittest.TestCase):
    def test_deadline_rotation_restart_owner_and_private_storage(self):
        with tempfile.TemporaryDirectory() as root:
            config={'data_dir':root,'web_access_restricted':True}
            guest=Path(root)/'guest.txt';owner=Path(root)/'owner.txt'
            end=invite(config,'https://chart.example',guest,owner,now=100)
            first=token(guest);permanent=token(owner)
            self.assertEqual(end,100+12*3600)
            self.assertEqual(permission(config,first,now=end-.01),(True,end))
            self.assertEqual(permission(config,first,now=end),(False,None))
            self.assertEqual(permission(config,permanent,now=end+86400),(True,None))
            saved=(Path(root)/'web-access.json').read_text()
            self.assertNotIn(first,saved);self.assertNotIn(permanent,saved)
            for path in [guest,owner,Path(root)/'web-access.json']:
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
            invite(config,'https://chart.example',guest,now=200)
            self.assertEqual(permission(config,first,now=201),(False,None))
            self.assertTrue(permission(config,token(guest),now=201)[0])
            self.assertTrue(permission(config,permanent,now=201)[0])

    def test_fail_closed_missing_malformed_state_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            config={'data_dir':root,'web_access_restricted':True}
            self.assertFalse(permission(config,'x'*43)[0])
            (Path(root)/'web-access.json').write_text('[]')
            self.assertFalse(permission(config,'x'*43)[0])
            for base in ['http://chart.example','https://chart.example/?key=x','https://user:pass@chart.example']:
                with self.assertRaises(ValueError):invite(config,base,Path(root)/'guest')
            with self.assertRaises(ValueError):invite(config,'https://chart.example',Path(root)/'guest')
            self.assertEqual(permission({'data_dir':root},''),(True,None))

    def test_http_gate_cookie_expiry_no_bypass_and_origin(self):
        with tempfile.TemporaryDirectory() as root:
            config={'data_dir':root,'web_access_restricted':True,'price_step':200}
            guest=Path(root)/'guest';owner=Path(root)/'owner'
            end=invite(config,'https://chart.example',guest,owner)
            service=ChartService(config)
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(service))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            def request(route,cookie=None,body=None,origin=None):
                headers={'Host':'chart.example','Connection':'close'}
                if cookie:headers['Cookie']=COOKIE+'='+cookie
                if body is not None:headers['Content-Type']='application/json'
                if origin:headers['Origin']=origin
                return urllib.request.urlopen(urllib.request.Request(base+route,data=body,headers=headers))
            try:
                with request('/') as r:self.assertIn('Просмотр по приглашению',r.read().decode())
                with request('/healthz') as r:self.assertEqual(r.status,200)
                with request('/access.js') as r:self.assertEqual(r.status,200)
                for route in ['/api/chart','/readyz','/app.js','/data/web-access.json','/?access='+token(guest)]:
                    if route.startswith('/?'):
                        with request(route) as r:self.assertIn('Просмотр по приглашению',r.read().decode())
                        continue
                    with self.assertRaises(urllib.error.HTTPError) as error:request(route)
                    self.assertEqual(error.exception.code,401)
                body=json.dumps({'token':token(guest)}).encode()
                with self.assertRaises(urllib.error.HTTPError) as error:request('/api/access',body=body,origin='https://attacker.example')
                self.assertEqual(error.exception.code,403)
                with request('/api/access',body=body,origin='https://chart.example') as r:
                    cookie=r.headers['Set-Cookie']
                    for flag in ['Secure','HttpOnly','SameSite=Lax','Path=/']:self.assertIn(flag,cookie)
                    self.assertNotIn(token(guest),r.read().decode())
                with patch('mtanalog.web.chart_data',return_value={'empty':True}) as build:
                    with request('/api/chart',cookie=token(guest)) as r:
                        self.assertEqual(r.status,200);self.assertEqual(int(r.headers['X-Access-Expires']),int(end))
                    with patch('mtanalog.access.time.time',return_value=end):
                        with self.assertRaises(urllib.error.HTTPError) as error:request('/api/chart',cookie=token(guest))
                        self.assertEqual(error.exception.code,401)
                        with request('/',cookie=token(owner)) as r:self.assertIn(b'id="dashboard"',r.read())
                    build.assert_called_once()
            finally:
                server.shutdown();server.server_close();thread.join()
