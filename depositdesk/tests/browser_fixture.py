"""Isolated browser test fixture with fictional simulator data and a seeded session.

Run: python tests/browser_fixture.py
Only loopback is served; this file is excluded from the deployment image.
Owner authentication is tested separately in test_app.py and test_upgrades.py.
"""
import secrets
from pathlib import Path
import sys
import tempfile
import uuid
from wsgiref.simple_server import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Desk, ThreadedWSGIServer, password_record


def main():
    with tempfile.TemporaryDirectory(prefix='depositdesk-browser-') as directory:
        password = secrets.token_urlsafe(32)
        app = Desk(directory, {'business_name':'Northline Studio · Test fixture', 'password':password_record(password)})
        token, _ = app.login(password, '127.0.0.1')
        rows=[('Studio Juniper','INV-2026-118','2450.00','pending'),('Ellis Morgan','SEP-PAY-004','875.50','draft'),
            ('Northline Tools','PO-00487','349.95','processed'),('Studio Juniper','INV-2026-104','1280.00','processed'),
            ('Ellis Morgan','SEP-REIMB-02','160.00','returned'),('Ridge Supply','PO-00492','620.00','pending'),
            ('Ridge Supply','PO-00493','94.25','failed'),('Sample contractor','TEST-INVOICE-001','125.50','draft')]
        recipients={}
        for name,reference,amount,state in rows:
            if name not in recipients:
                recipients[name]=app.add_recipient({'name':name,'kind':'vendor','account_label':'Fictional checking',
                    'authorization_ref':'DEMO-AUTH-'+str(len(recipients)+1),'acknowledged':True})['id']
            payment=app.draft({'request_id':str(uuid.uuid4()),'recipient_id':recipients[name],'amount':amount,'reference':reference,'memo':'Fictional test record.'})
            if state!='draft':
                app.submit(payment['id'],{'authorized':True,'amount':amount})
                if state!='pending':app.action(payment['id'],'simulate',{'state':'processed' if state=='returned' else state})
                if state=='returned':app.action(payment['id'],'simulate',{'state':'returned'})
        def fixture(environ,start_response):
            def respond(status,headers):
                if environ.get('PATH_INFO')=='/' and not environ.get('HTTP_COOKIE'):
                    headers.append(('Set-Cookie',f'desk_session={token}; Path=/; HttpOnly; SameSite=Strict'))
                return start_response(status,headers)
            return app(environ,respond)
        print('Fictional simulator fixture: http://127.0.0.1:8765',flush=True)
        with make_server('127.0.0.1',8765,fixture,server_class=ThreadedWSGIServer) as server:
            server.serve_forever()


if __name__=='__main__':
    main()
