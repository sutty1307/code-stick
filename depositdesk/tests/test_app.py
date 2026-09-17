import csv
import hashlib
import hmac
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
import uuid

from app import Desk, Problem, cents, password_record
from providers import DwollaSandbox, NoRedirect, ProviderError, Simulator, resource_id, resource_url


class CountingProvider(Simulator):
    def __init__(self):
        self.calls = 0

    def submit(self, payment):
        self.calls += 1
        return super().submit(payment)


class DeskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'A test-only password 127!'
        cls.auth = password_record(cls.password)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"password": self.auth, "business_name": "Test business"}
        self.provider = CountingProvider()
        self.app = Desk(self.temp.name, self.config, self.provider)
        self.cookie = ''
        self.csrf = ''

    def http(self, path, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else b''
        env = {'REQUEST_METHOD': 'POST' if data is not None else 'GET', 'PATH_INFO': path,
            'HTTP_HOST': '127.0.0.1:8765', 'HTTP_ORIGIN': 'http://127.0.0.1:8765',
            'REMOTE_ADDR': '127.0.0.1', 'HTTP_COOKIE': self.cookie, 'HTTP_X_CSRF_TOKEN': self.csrf,
            'CONTENT_TYPE': 'application/json', 'CONTENT_LENGTH': str(len(body)), 'wsgi.input': io.BytesIO(body)}
        env.update(headers or {})
        captured = {}
        def start(status, response_headers):
            captured.update(status=int(status.split()[0]), headers=dict(response_headers))
        result = b''.join(self.app(env, start))
        parsed = json.loads(result) if captured['headers'].get('Content-Type', '').startswith('application/json') else result
        return captured['status'], parsed, captured['headers']

    def sign_in(self):
        status, body, headers = self.http('/api/login', {'password': self.password})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.csrf = body['csrf']
        return headers

    def recipient(self, **changes):
        data = {'name': 'Sample contractor', 'kind': 'contractor', 'account_label': 'Test checking',
            'authorization_ref': 'TEST-AUTH-001', 'acknowledged': True}
        data.update(changes)
        return self.app.add_recipient(data)['id']

    def payload(self, recipient=None, **changes):
        data = {'request_id': str(uuid.uuid4()), 'recipient_id': recipient or self.recipient(),
            'amount': '125.50', 'reference': 'TEST-INVOICE-001', 'memo': 'Test work'}
        data.update(changes)
        return data

    def approve(self, payment):
        return self.app.submit(payment['id'], {'authorized': True, 'amount': '125.50'})

    def test_sign_in_csrf_cookie_and_private_records(self):
        self.assertEqual(self.http('/api/state')[0], 401)
        headers = self.sign_in()
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(self.http('/api/state')[0], 200)
        self.assertFalse(self.http('/api/state')[1]['live_enabled'])
        self.assertEqual(self.http('/api/recipients', {}, {'HTTP_X_CSRF_TOKEN': 'wrong'})[0], 403)
        self.assertEqual(self.http('/api/logout', {})[0], 200)
        self.assertEqual(self.http('/api/state')[0], 401)

    def test_origin_host_and_non_json_are_rejected(self):
        self.assertEqual(self.http('/api/login', {'password': self.password}, {'HTTP_ORIGIN': 'https://elsewhere.example'})[0], 403)
        self.assertEqual(self.http('/', headers={'HTTP_HOST': 'evil.example'})[0], 403)
        self.assertEqual(self.http('/api/login', {}, {'CONTENT_TYPE': 'text/plain'})[0], 415)
        self.assertEqual(self.http('/api/login', {}, {'CONTENT_LENGTH': '20000'})[0], 413)
        self.assertEqual(self.http('/api/login', [1, 2])[0], 400)

    def test_rate_limit_sign_in(self):
        for _ in range(5):
            self.assertEqual(self.http('/api/login', {'password': 'wrong'})[0], 401)
        self.assertEqual(self.http('/api/login', {'password': self.password})[0], 429)

    def test_money_is_exact_and_invalid_amounts_rejected(self):
        for value, expected in [('0.01', 1), ('125.5', 12550), ('25000.00', 2500000), ('1', 100)]:
            self.assertEqual(cents(value), expected)
        for value in ['NaN', 'Infinity', '-1', '0', '1e2', '1.001', '25,000', '25000.01', ' 1', '01', 100, True]:
            with self.subTest(value=value), self.assertRaises(Problem):
                cents(value)

    def test_drafts_are_idempotent_and_reference_prevents_duplicates(self):
        payload = self.payload()
        first = self.app.draft(payload)
        second = self.app.draft(payload)
        self.assertEqual(first['id'], second['id'])
        with self.assertRaises(Problem):
            self.app.draft({**payload, 'amount': '126'})
        with self.assertRaises(Problem):
            self.app.draft({**payload, 'request_id': str(uuid.uuid4()), 'reference': ' test-INVOICE-001 '})
        with self.app.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM payments').fetchone()[0], 1)

    def test_confirmation_and_amount_are_required(self):
        payment = self.app.draft(self.payload())
        for payload in [{'authorized': False, 'amount': '125.50'}, {'authorized': True, 'amount': '1.00'}]:
            with self.assertRaises(Problem):
                self.app.submit(payment['id'], payload)
        self.assertEqual(self.provider.calls, 0)

    def test_submit_repeated_and_simulated_return(self):
        payment = self.app.draft(self.payload())
        for _ in range(3):
            self.assertEqual(self.approve(payment)['state'], 'pending')
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.app.action(payment['id'], 'simulate', {'state': 'processed'})['state'], 'processed')
        self.assertEqual(self.app.action(payment['id'], 'simulate', {'state': 'returned'})['state'], 'returned')
        with self.assertRaises(Problem):
            self.app.action(payment['id'], 'simulate', {'state': 'processed'})

    def test_concurrent_submission_only_calls_provider_once(self):
        entered, release = threading.Event(), threading.Event()
        original = self.provider.submit
        def slow(payment):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test release did not arrive')
            return original(payment)
        self.provider.submit = slow
        payment = self.app.draft(self.payload())
        results = []
        worker = threading.Thread(target=lambda: results.append(self.approve(payment)))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.approve(payment)['state'], 'submitting')
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['state'], 'pending')
        self.assertEqual(self.provider.calls, 1)

    def test_uncertain_outcome_never_retries_even_after_restart(self):
        self.provider.submit = Mock(side_effect=ProviderError('Timeout after possible acceptance'))
        payment = self.app.draft(self.payload())
        self.assertEqual(self.approve(payment)['state'], 'needs_review')
        self.app = Desk(self.temp.name, self.config, self.provider)
        self.assertEqual(self.approve(payment)['state'], 'needs_review')
        self.assertEqual(self.provider.submit.call_count, 1)

    def test_failed_source_check_is_not_a_submission(self):
        self.provider.check_source = Mock(side_effect=ProviderError('Source is not verified'))
        payment = self.app.draft(self.payload())
        current = self.approve(payment)
        self.assertEqual(current['state'], 'draft')
        self.assertIsNone(current['approved_at'])
        self.assertIn('No transfer was submitted', current['message'])
        self.assertEqual(self.provider.calls, 0)

    def test_crash_during_submission_stays_unconfirmed(self):
        class ProcessCrash(BaseException):
            pass
        self.provider.submit = Mock(side_effect=ProcessCrash())
        payment = self.app.draft(self.payload())
        with self.assertRaises(ProcessCrash):
            self.approve(payment)
        self.app = Desk(self.temp.name, self.config, self.provider)
        self.assertEqual(self.approve(payment)['state'], 'submitting')
        self.assertEqual(self.provider.submit.call_count, 1)

    def test_cancel_only_before_submission(self):
        draft = self.app.draft(self.payload())
        self.assertEqual(self.app.action(draft['id'], 'cancel', {})['state'], 'cancelled')
        self.assertEqual(self.approve(draft)['state'], 'cancelled')
        self.assertEqual(self.provider.calls, 0)
        payment = self.app.draft(self.payload())
        self.approve(payment)
        with self.assertRaises(Problem):
            self.app.action(payment['id'], 'cancel', {})

    def test_bank_numbers_not_stored_and_csv_formulas_escaped(self):
        rid = self.recipient(name='=HYPERLINK("x")', accountNumber='123456789876543', routingNumber='222222226')
        self.app.draft(self.payload(recipient=rid, reference='+FORMULA'))
        self.sign_in()
        status, body, _ = self.http('/api/export.csv')
        self.assertEqual(status, 200)
        row = list(csv.reader(io.StringIO(body.decode())))[1]
        self.assertTrue(row[2].startswith("'="))
        self.assertTrue(row[3].startswith("'+"))
        state = json.dumps(self.http('/api/state')[1])
        self.assertNotIn('123456789876543', state)
        self.assertNotIn('destination_ref', state)

    def test_environment_and_source_binding_prevents_cross_environment_use(self):
        with self.assertRaises(ValueError):
            Desk(self.temp.name, {**self.config, 'provider': 'production'})
        with self.assertRaises(ValueError):
            Desk(self.temp.name, {**self.config, 'provider': 'dwolla_sandbox', 'source_id': str(uuid.uuid4())}, Simulator())

    def test_full_http_workflow_and_assets(self):
        for path in ['/', '/app.js', '/styles.css']:
            status, body, headers = self.http(path)
            self.assertEqual(status, 200)
            self.assertGreater(len(body), 500)
            self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.sign_in()
        rid = self.recipient()
        status, draft, _ = self.http('/api/payments', self.payload(recipient=rid))
        self.assertEqual(status, 200)
        status, pending, _ = self.http('/api/payments/' + draft['id'] + '/submit', {'authorized': True, 'amount': '125.50'})
        self.assertEqual((status, pending['state']), (200, 'pending'))
        status, detail, _ = self.http('/api/payments/' + draft['id'])
        self.assertEqual(status, 200)
        self.assertEqual(len(detail['audit']), 3)

    def test_webhooks_require_signature_dedupe_and_do_not_trust_status(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {**self.config, 'provider': 'dwolla_sandbox', 'source_id': str(uuid.uuid4()), 'webhook_secret': 'test-webhook-secret'}
            provider = CountingProvider()
            provider.check_source = Mock(return_value=None)
            provider_ref = str(uuid.uuid4())
            provider.submit = Mock(return_value=provider_ref)
            app = Desk(directory, config, provider)
            rid = app.add_recipient({'name':'Sample', 'kind':'vendor', 'account_label':'Test bank', 'authorization_ref':'TEST',
                'acknowledged':True, 'customer_id':str(uuid.uuid4()), 'funding_id':str(uuid.uuid4())})['id']
            payment = app.draft(self.payload(recipient=rid))
            app.submit(payment['id'], {'authorized': True, 'amount': '125.50'})
            event = {'id':str(uuid.uuid4()),'topic':'transfer_completed','status':'processed',
                '_links':{'resource':{'href':resource_url('transfers',provider_ref)}}}
            raw = json.dumps(event).encode()
            signature = hmac.new(config['webhook_secret'].encode(), raw, hashlib.sha256).hexdigest()
            with self.assertRaises(Problem):
                app.webhook(raw, 'invalid')
            app.webhook(raw, signature)
            app.webhook(raw, signature)
            with app.connection() as db:
                current = app.payment(db,payment['id'])
                self.assertEqual((current['state'], current['refresh_due']), ('pending', 1))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM webhook_events').fetchone()[0], 1)
            with self.assertRaises(Problem):
                app.action(payment['id'], 'simulate', {'state':'processed'})
            provider.refresh = Mock(return_value='processed')
            self.assertEqual(app.action(payment['id'],'refresh',{})['state'], 'processed')
            provider.refresh.return_value = 'pending'
            with self.assertRaises(Problem):
                app.action(payment['id'],'refresh',{})


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.source = str(uuid.uuid4())
        self.destination = str(uuid.uuid4())
        self.adapter = DwollaSandbox('test-key','test-secret',self.source)
        self.payment = {'id':str(uuid.uuid4()), 'source_ref':self.source, 'destination_ref':self.destination,
            'amount_cents':1, 'provider_ref':str(uuid.uuid4())}

    def test_transfer_payload_uses_exact_cents_and_stable_idempotency(self):
        ref = str(uuid.uuid4())
        self.adapter.request = Mock(return_value=(201,{'Location':resource_url('transfers',ref)},{}))
        self.assertEqual(self.adapter.submit(self.payment),ref)
        args = self.adapter.request.call_args.args
        self.assertEqual(args[2]['amount']['value'],'0.01')
        self.assertEqual(args[3],self.payment['id'])
        self.assertEqual(args[2]['correlationId'],self.payment['id'])
        self.assertNotIn('test-secret',json.dumps(args))

    def test_production_urls_and_redirects_rejected(self):
        for url in ['https://api.dwolla.com/transfers/'+str(uuid.uuid4()),'https://evil.example/transfers/'+str(uuid.uuid4()),resource_url('transfers',str(uuid.uuid4()))+'?redirect=1']:
            with self.assertRaises(ProviderError):
                resource_id(url,'transfers')
        with self.assertRaises(ProviderError):
            self.adapter._exchange('https://api.dwolla.com/transfers')
        with self.assertRaises(ProviderError):
            NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example')

    def test_wrong_customer_and_unverified_source_rejected(self):
        customer = str(uuid.uuid4())
        body = {'type':'bank','removed':False,'status':'unverified','_links':{'customer':{'href':resource_url('customers',str(uuid.uuid4()))}}}
        self.adapter.request = Mock(return_value=(200,{},body))
        with self.assertRaises(ProviderError):
            self.adapter.validate_recipient(customer,self.destination)
        with self.assertRaises(ProviderError):
            self.adapter.check_source(self.source)

    def test_refresh_rejects_mismatched_amount_and_unknown_status(self):
        body={'id':self.payment['provider_ref'],'amount':{'currency':'USD','value':'2.00'},'status':'processed'}
        self.adapter.request=Mock(return_value=(200,{},body))
        with self.assertRaises(ProviderError):
            self.adapter.refresh(self.payment)
        body['amount']['value']='0.01'
        body['_links'] = {'source': {'href':resource_url('funding-sources',self.source)},
            'destination': {'href':resource_url('funding-sources',self.destination)}}
        body['status']='unknown'
        with self.assertRaises(ProviderError):
            self.adapter.refresh(self.payment)
        body['status']='pending'
        self.assertEqual(self.adapter.refresh(self.payment),'pending')


if __name__ == '__main__':
    unittest.main()
