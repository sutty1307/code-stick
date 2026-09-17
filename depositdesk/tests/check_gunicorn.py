"""Optional smoke test against the installed Gunicorn, with disposable local data."""
import http.cookiejar
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def main():
    root=Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='desk-wsgi-') as directory:
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
        origin=f'http://127.0.0.1:{port}'
        password=secrets.token_urlsafe(32)
        env={**os.environ,'DEPOSITDESK_PROVIDER':'simulator','DEPOSITDESK_ORIGIN':origin,
            'DEPOSITDESK_DATA_DIR':directory,'DEPOSITDESK_BUSINESS_NAME':'WSGI smoke fixture','DEPOSITDESK_OWNER_PASSWORD':password}
        subprocess.run([sys.executable,'app.py','init'],cwd=root,env=env,check=True,capture_output=True,timeout=10)
        process=subprocess.Popen([sys.executable,'-m','gunicorn','--config','gunicorn.conf.py','--bind',f'127.0.0.1:{port}','wsgi:application'],
            cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
        client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        csrf=''
        def request(path,body=None):
            req=urllib.request.Request(origin+path,data=json.dumps(body).encode() if body is not None else None,
                headers={'Origin':origin,'Content-Type':'application/json','X-CSRF-Token':csrf})
            with client.open(req,timeout=3) as response:return response.status,json.load(response)
        try:
            deadline=time.monotonic()+8
            while True:
                try:
                    assert request('/readyz')[0]==200;break
                except (urllib.error.URLError,TimeoutError):
                    if process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Gunicorn did not become ready.')
                    time.sleep(.1)
            csrf=request('/api/login',{'password':password})[1]['csrf']
            rid=request('/api/recipients',{'name':'WSGI fixture','kind':'vendor','account_label':'Fictional bank','authorization_ref':'TEST-ONLY','acknowledged':True})[1]['id']
            payment=request('/api/payments',{'request_id':str(uuid.uuid4()),'recipient_id':rid,'reference':'WSGI-INV-001','amount':'1.25'})[1]
            route='/api/payments/'+payment['id']
            assert request(route+'/submit',{'amount':'1.25','authorized':True})[1]['state']=='pending'
            assert request(route+'/simulate',{'state':'processed'})[1]['state']=='processed'
            new_password=secrets.token_urlsafe(32)
            subprocess.run([sys.executable,'app.py','passwd'],cwd=root,env={**env,'DEPOSITDESK_OWNER_PASSWORD':new_password},check=True,capture_output=True,timeout=10)
            try:request('/api/state');raise AssertionError('Old session survived rotation.')
            except urllib.error.HTTPError as error:assert error.code==401
            try:request('/api/login',{'password':password});raise AssertionError('Old password survived rotation.')
            except urllib.error.HTTPError as error:assert error.code==401
            csrf=request('/api/login',{'password':new_password})[1]['csrf']
            assert request(route)[1]['payment']['state']=='processed'
            print(json.dumps({'result':'passed','gunicorn_version':importlib.metadata.version('gunicorn'),
                'workers':2,'threads_per_worker':4,'checks':['readiness','owner login','draft','release','processed',
                    'password rotation while serving','old session rejected','old password rejected','new password accepted','record retained']},indent=2))
        finally:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.communicate(timeout=6)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.communicate()


if __name__=='__main__':main()
