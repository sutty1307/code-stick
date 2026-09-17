import copy
import hashlib
import hmac
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import uuid
import zipfile

from app import Desk, Problem, password_record, rotate_password, SCHEMA
from backup import backup, verify_backup
from providers import DwollaSandbox, ProviderError, resource_url, resource_id
import test_app as fixtures


class WorkflowUpgrades(unittest.TestCase):
    # Reuse fixture helpers, not inherited test cases, so counts aren't inflated.
    setUpClass = classmethod(fixtures.DeskTests.setUpClass.__func__)
    setUp = fixtures.DeskTests.setUp
    http = fixtures.DeskTests.http
    sign_in = fixtures.DeskTests.sign_in
    recipient = fixtures.DeskTests.recipient
    payload = fixtures.DeskTests.payload
    approve = fixtures.DeskTests.approve

    def sandbox(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = {**self.config, 'provider':'dwolla_sandbox', 'source_id':str(uuid.uuid4()), 'webhook_secret':'test-only-webhook'}
        self.provider = Mock()
        self.provider.validate_recipient.side_effect = lambda customer, funding: funding
        self.provider.submit.return_value = str(uuid.uuid4())
        self.app = Desk(directory.name, self.config, self.provider)
        rid = self.recipient(customer_id=str(uuid.uuid4()),funding_id=str(uuid.uuid4()))
        return self.app.draft(self.payload(recipient=rid))

    def event(self, ref):
        body = json.dumps({'id':str(uuid.uuid4()),'topic':'transfer_completed',
            '_links':{'resource':{'href':resource_url('transfers',ref)}}}).encode()
        return body, hmac.new(self.config['webhook_secret'].encode(),body,hashlib.sha256).hexdigest()

    def test_reference_casing_preserved_with_unicode_duplicate_protection(self):
        payload = self.payload(reference='Invoice-Straße-0041')
        saved = self.app.draft(payload)
        self.assertEqual(saved['reference'],'Invoice-Straße-0041')
        with self.assertRaises(Problem):
            self.app.draft({**payload,'request_id':str(uuid.uuid4()),'reference':'INVOICE-STRASSE-0041'})
        self.sign_in()
        self.assertIn(b'Invoice-Stra\xc3\x9fe-0041',self.http('/api/export.csv')[1])

    def test_original_schema_migrates_without_losing_records(self):
        payload = self.payload()
        saved = self.app.draft(payload)
        # Rebuild a v1 database from the v1 schema and the unchanged legacy columns.
        with tempfile.TemporaryDirectory() as legacy:
            database = Path(legacy)/'depositdesk.sqlite3'
            with sqlite3.connect(database) as old, self.app.connection() as current:
                old.executescript(SCHEMA)
                for table in ['metadata','recipients','payments','audit']:
                    columns=[row[1] for row in old.execute('PRAGMA table_info('+table+')')]
                    for row in current.execute('SELECT '+','.join(columns)+' FROM '+table):
                        old.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in columns)+')',tuple(row))
            upgraded=Desk(legacy,self.config,self.provider)
            self.assertEqual(upgraded.draft(payload)['id'],saved['id'])
            with self.assertRaises(Problem):
                upgraded.draft({**payload,'request_id':str(uuid.uuid4())})
            with upgraded.connection() as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)

    def test_rotation_revokes_sessions_and_old_password_in_running_workers(self):
        self.sign_in()
        second=Desk(self.temp.name,self.config,self.provider)
        replacement='Replacement test password 456!'
        rotate_password(second,replacement)
        self.assertEqual(self.http('/api/state')[0],401)
        for worker in (self.app,second):
            with self.assertRaises(Problem): worker.login(self.password,'127.0.0.2')
            self.assertTrue(worker.login(replacement,'127.0.0.2')[0])

    def test_rotation_during_password_check_cannot_mint_old_session(self):
        original=hashlib.scrypt
        replacement='New replacement test password 789!'
        changed=False
        def intervening(*args,**kwargs):
            nonlocal changed
            result=original(*args,**kwargs)
            if not changed:
                changed=True
                rotate_password(self.app,replacement)
            return result
        with patch('app.hashlib.scrypt',side_effect=intervening), self.assertRaises(Problem):
            self.app.login(self.password,'127.0.0.1')
        with self.app.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)

    def test_proxy_headers_only_from_configured_peer(self):
        self.app.behind_proxy=True
        self.assertEqual(self.app.client_ip({'REMOTE_ADDR':'203.0.113.3','HTTP_X_FORWARDED_FOR':'1.2.3.4'}),'203.0.113.3')
        self.assertEqual(self.app.client_ip({'REMOTE_ADDR':'127.0.0.1','HTTP_X_FORWARDED_FOR':'1.2.3.4, 198.51.100.2'}),'198.51.100.2')
        self.assertEqual(self.app.client_ip({'REMOTE_ADDR':'127.0.0.1','HTTP_X_FORWARDED_FOR':'garbage'}),'127.0.0.1')

    def test_non_ascii_csrf_is_rejected_not_server_error(self):
        self.sign_in()
        self.assertEqual(self.http('/api/logout',{}, {'HTTP_X_CSRF_TOKEN':'\u2603'})[0],403)

    def test_health_and_readiness_work_without_host_override(self):
        self.assertEqual(self.http('/healthz',headers={'HTTP_HOST':'probe.internal'})[0],200)
        self.assertEqual(self.http('/readyz',headers={'HTTP_HOST':'probe.internal'})[0],200)
        self.assertEqual(self.http('/api/state',headers={'HTTP_HOST':'probe.internal'})[0],403)

    def test_same_source_and_destination_rejected(self):
        self.sandbox()
        with self.assertRaises(Problem):
            self.recipient(customer_id=str(uuid.uuid4()),funding_id=self.config['source_id'])

    def test_attached_adapter_destination_urls_migrate_to_same_uuid(self):
        payment=self.sandbox()
        with self.app.connection() as db:
            original=self.app.payment(db,payment['id'])['destination_ref']
            address=resource_url('funding-sources',original)
            db.execute('UPDATE recipients SET destination_ref=? WHERE id=?',(address,payment['recipient_id']))
            db.execute('UPDATE payments SET destination_ref=? WHERE id=?',(address,payment['id']))
        self.app=Desk(self.app.directory,self.config,self.provider)
        with self.app.connection() as db:
            self.assertEqual(self.app.payment(db,payment['id'])['destination_ref'],original)
        self.assertEqual(self.approve(payment)['state'],'pending')

    def test_destination_is_revalidated_before_submission(self):
        payment=self.sandbox()
        self.provider.validate_recipient.side_effect=ProviderError('Removed recipient')
        self.assertEqual(self.approve(payment)['state'],'draft')
        self.provider.submit.assert_not_called()

    def test_event_arriving_during_refresh_is_not_cleared(self):
        payment=self.approve(self.sandbox())
        def refresh(_):
            self.app.webhook(*self.event(payment['provider_ref']))
            return 'pending'
        self.provider.refresh.side_effect=refresh
        updated=self.app.action(payment['id'],'refresh',{})
        self.assertEqual(updated['refresh_due'],1)
        self.provider.refresh.side_effect=None
        self.provider.refresh.return_value='processed'
        self.assertEqual(self.app.action(payment['id'],'refresh',{})['refresh_due'],0)

    def test_event_before_submission_response_is_matched_afterwards(self):
        payment=self.sandbox()
        ref=str(uuid.uuid4())
        def submit(_):
            self.app.webhook(*self.event(ref))
            return ref
        self.provider.submit.side_effect=submit
        self.assertEqual(self.approve(payment)['refresh_due'],1)

    def test_recovery_links_existing_transfer_without_resubmitting(self):
        payment=self.sandbox()
        self.provider.submit.side_effect=TimeoutError()
        payment=self.approve(payment)
        self.assertTrue(payment['can_reconcile'])
        self.provider.verify_transfer.return_value='pending'
        ref=str(uuid.uuid4())
        saved=self.app.action(payment['id'],'reconcile',{'provider_ref':ref})
        self.assertEqual((saved['state'],saved['provider_ref']),('pending',ref))
        self.provider.verify_transfer.assert_called_once()
        self.assertEqual(self.provider.submit.call_count,1)
        self.approve(saved)
        self.assertEqual(self.provider.submit.call_count,1)

    def test_unverified_recovery_never_changes_record(self):
        payment=self.sandbox()
        self.provider.submit.side_effect=TimeoutError()
        self.approve(payment)
        self.provider.verify_transfer.side_effect=ProviderError('Wrong transfer')
        with self.assertRaises(ProviderError):
            self.app.action(payment['id'],'reconcile',{'provider_ref':str(uuid.uuid4())})
        with self.app.connection() as db:
            self.assertIsNone(self.app.payment(db,payment['id'])['provider_ref'])

    def test_active_submission_cannot_be_reconciled(self):
        payment=self.sandbox()
        with self.app.connection() as db:
            db.execute("UPDATE payments SET state='submitting' WHERE id=?",(payment['id'],))
        with self.assertRaises(Problem):
            self.app.action(payment['id'],'reconcile',{'provider_ref':str(uuid.uuid4())})
        self.provider.verify_transfer.assert_not_called()

    def test_backup_restores_records_and_password_but_no_sessions(self):
        saved=self.app.draft(self.payload())
        self.sign_in()
        Path(self.temp.name,'owner.json').write_text(json.dumps(self.config))
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/"owner's backup.zip"
            backup(self.temp.name,target)
            self.assertEqual(target.stat().st_mode & 0o777,0o600)
            self.assertTrue(verify_backup(target)['sessions_removed'])
            restored=Path(directory)/'restored'
            with zipfile.ZipFile(target) as archive:
                archive.extractall(restored)
            desk=Desk(restored,json.loads((restored/'owner.json').read_text()))
            with desk.connection() as db:
                self.assertEqual(desk.payment(db,saved['id'])['amount_cents'],12550)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)
            self.assertTrue(desk.login(self.password,'127.0.0.1')[0])
            with self.assertRaises(FileExistsError): backup(self.temp.name,target)

    def test_backup_before_upgrade_preserves_legacy_bootstrap_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'legacy';source.mkdir()
            (source/'owner.json').write_text(json.dumps(self.config))
            with sqlite3.connect(source/'depositdesk.sqlite3') as db:
                db.executescript(SCHEMA)
                db.execute("INSERT INTO metadata VALUES('environment','simulator:simulated:business')")
            archive_path=Path(directory)/'legacy.zip'
            backup(source,archive_path)
            restored=Path(directory)/'restore'
            with zipfile.ZipFile(archive_path) as archive:archive.extractall(restored)
            desk=Desk(restored,json.loads((restored/'owner.json').read_text()))
            self.assertTrue(desk.login(self.password,'127.0.0.1')[0])


class ProviderUpgrades(unittest.TestCase):
    def setUp(self):
        self.source,self.destination,self.ref=[str(uuid.uuid4()) for _ in range(3)]
        self.adapter=DwollaSandbox('test-key','test-secret',self.source)
        self.payment={'id':str(uuid.uuid4()),'amount_cents':12550,'source_ref':self.source,'destination_ref':self.destination,'provider_ref':None}
        self.body={'id':self.ref,'amount':{'currency':'USD','value':'125.50'},'status':'pending','correlationId':self.payment['id'],
            '_links':{'source':{'href':resource_url('funding-sources',self.source)},'destination':{'href':resource_url('funding-sources',self.destination)}}}
        self.adapter.request=Mock(return_value=(200,{},self.body))

    def test_impostor_sandbox_hosts_and_url_variants_rejected(self):
        for base in ['https://api-sandbox.example.invalid','https://api-sandbox.dwolla.com@evil.example','https://api.dwolla.com',
            'https://api-sandbox.dwolla.com:443','https://api-sandbox.dwolla.com/']:
            with self.subTest(base=base),self.assertRaises(ValueError):
                DwollaSandbox('key','secret',self.source,base=base)
        for base in ['https://api.dwolla.com','https://evildwolla.com','https://another.dwolla.com']:
            with self.subTest(base=base),self.assertRaises(ProviderError):resource_id(base+'/transfers/'+self.ref,'transfers')

    def test_reconciliation_requires_matching_identity_amount_accounts_and_correlation(self):
        self.assertEqual(self.adapter.verify_transfer(self.payment,self.ref),'pending')
        cases=[('id',str(uuid.uuid4())),('amount',{'currency':'EUR','value':'125.50'}),('amount',{'currency':'USD','value':'125.51'}),
            ('amount',[]),('correlationId',None),('correlationId',str(uuid.uuid4())),('_links',{}),('_links',[])]
        for key,value in cases:
            body=copy.deepcopy(self.body);body[key]=value
            self.adapter.request.return_value=(200,{},body)
            with self.subTest(key=key,value=value),self.assertRaises(ProviderError):self.adapter.verify_transfer(self.payment,self.ref)

    def test_wrong_destination_rejected_even_when_correlation_matches(self):
        self.body['_links']['destination']['href']=resource_url('funding-sources',str(uuid.uuid4()))
        with self.assertRaises(ProviderError):self.adapter.verify_transfer(self.payment,self.ref)

    def test_recovery_accepts_legacy_payment_metadata_only_when_matching(self):
        self.body.pop('correlationId')
        self.body['metadata']={'payment_id':self.payment['id']}
        self.assertEqual(self.adapter.verify_transfer(self.payment,self.ref),'pending')
        self.body['metadata']['payment_id']=str(uuid.uuid4())
        with self.assertRaises(ProviderError):self.adapter.verify_transfer(self.payment,self.ref)

    def test_transport_rejects_malformed_and_oversized_responses(self):
        for raw in (b'[]',b'{',b'x'*2_000_001):
            response=Mock(status=200);response.read.return_value=raw;response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            self.adapter.http.open=Mock(return_value=response)
            with self.subTest(size=len(raw)),self.assertRaises(ProviderError):self.adapter._exchange('/transfers/'+self.ref)

    def test_token_expiry_rejects_nan_boolean_and_bad_token(self):
        for token,expiry in [('valid',float('nan')),('valid',True),('valid',0),('',3600),('bad\r\nheader',3600)]:
            self.adapter._exchange=Mock(return_value=(200,{}, {'access_token':token,'expires_in':expiry}))
            with self.subTest(expiry=expiry),self.assertRaises(ProviderError):self.adapter._token()

    def test_transfer_post_is_never_automatically_retried(self):
        self.adapter.request=Mock(side_effect=ProviderError('Uncertain response'))
        with self.assertRaises(ProviderError):self.adapter.submit(self.payment)
        self.assertEqual(self.adapter.request.call_count,1)


if __name__=='__main__':
    unittest.main()
