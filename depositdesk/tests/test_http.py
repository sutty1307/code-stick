"""Real HTTP and fresh CLI setup, entirely local with fictional records."""
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from wsgiref.simple_server import make_server, WSGIRequestHandler

from app import Desk, ThreadedWSGIServer, password_record


class Quiet(WSGIRequestHandler):
    def log_message(self,*args):pass


class HttpAndCliTests(unittest.TestCase):
    def test_real_http_payment_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            password='HTTP fixture password only 123!'
            app=Desk(directory,{'password':password_record(password)})
            with make_server('127.0.0.1',0,app,server_class=ThreadedWSGIServer,handler_class=Quiet) as server:
                origin='http://127.0.0.1:'+str(server.server_port)
                app.origin,app.host=origin,origin.removeprefix('http://')
                worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
                client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                csrf=''
                def request(path,data=None):
                    headers={'Origin':origin,'Content-Type':'application/json','X-CSRF-Token':csrf}
                    req=urllib.request.Request(origin+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
                    with client.open(req,timeout=5) as response:
                        return json.load(response)
                try:
                    with self.assertRaises(urllib.error.HTTPError) as denied:request('/api/state')
                    self.assertEqual(denied.exception.code,401)
                    csrf=request('/api/login',{'password':password})['csrf']
                    recipient=request('/api/recipients',{'name':'HTTP test recipient','kind':'contractor','account_label':'Fictional bank','authorization_ref':'TEST-AUTH','acknowledged':True})
                    body={'request_id':str(uuid.uuid4()),'recipient_id':recipient['id'],'amount':'125.50','reference':'HTTP-INV-0041'}
                    payment=request('/api/payments',body)
                    self.assertEqual(request('/api/payments',body)['id'],payment['id'])
                    route='/api/payments/'+payment['id']
                    self.assertEqual(request(route+'/submit',{'authorized':True,'amount':'125.50'})['state'],'pending')
                    self.assertEqual(request(route+'/simulate',{'state':'processed'})['state'],'processed')
                    self.assertEqual(request(route+'/simulate',{'state':'returned'})['state'],'returned')
                    self.assertEqual(len(request(route)['audit']),5)
                    self.assertEqual(request(route)['payment']['reference'],'HTTP-INV-0041')
                    request('/api/logout',{})
                    with self.assertRaises(urllib.error.HTTPError):request('/api/state')
                finally:
                    server.shutdown();worker.join(5)

    def test_fresh_init_wsgi_import_and_password_rotation(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            env={**os.environ,'DEPOSITDESK_DATA_DIR':directory,'DEPOSITDESK_PROVIDER':'simulator',
                'DEPOSITDESK_BUSINESS_NAME':'Fresh fixture','DEPOSITDESK_OWNER_PASSWORD':'Fresh fixture password 123!'}
            def run(*args):return subprocess.run([sys.executable,*args],cwd=root,env=env,capture_output=True,text=True,timeout=20)
            self.assertEqual(run('app.py','init').returncode,0)
            self.assertNotIn('password',json.loads(Path(directory,'owner.json').read_text()))
            self.assertEqual(run('-c','from wsgi import application; assert application.mode == "simulator"').returncode,0)
            env['DEPOSITDESK_OWNER_PASSWORD']='Fresh replacement password 456!'
            self.assertEqual(run('app.py','passwd').returncode,0)
            self.assertNotEqual(run('app.py','init').returncode,0)


if __name__=='__main__':unittest.main()
