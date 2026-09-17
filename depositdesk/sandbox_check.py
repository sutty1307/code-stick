"""Opt-in Dwolla sandbox acceptance harness. No live endpoint is supported.

Default: validate configured sandbox source and recipient only.
--submit --confirm 0.01: save/reuse one acceptance-test instruction, then submit
it only if it is still a draft. Reruns never retry an unconfirmed instruction.
"""
import argparse
import json
import os
import uuid

from app import create_app
from providers import identifier


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--submit',action='store_true')
    parser.add_argument('--confirm',help='Retype 0.01 to authorize the sandbox test instruction')
    args=parser.parse_args()
    if args.submit and args.confirm!='0.01':
        parser.error('The sandbox test requires --submit --confirm 0.01.')
    if os.environ.get('DEPOSITDESK_PROVIDER')!='dwolla_sandbox':
        parser.error('Set DEPOSITDESK_PROVIDER=dwolla_sandbox and a separate persistent test data directory.')
    app=create_app()
    customer=identifier(os.environ.get('DWOLLA_SANDBOX_CUSTOMER_ID',''))
    destination=identifier(os.environ.get('DWOLLA_SANDBOX_DESTINATION_ID',''))
    app.provider.check_source(app.source)
    app.provider.validate_recipient(customer,destination)
    if destination==app.source:
        parser.error('The source and destination must differ.')
    if not args.submit:
        print(json.dumps({'environment':'dwolla_sandbox','source_validated':True,'recipient_validated':True,'transfer_submitted':False}))
        return
    with app.connection() as db:
        existing=db.execute('SELECT id FROM recipients WHERE destination_ref=?',(destination,)).fetchone()
    recipient=existing['id'] if existing else app.add_recipient({'name':'Sandbox acceptance recipient','kind':'vendor',
        'account_label':'Sandbox bank','authorization_ref':'SANDBOX-ACCEPTANCE-ONLY','acknowledged':True,
        'customer_id':customer,'funding_id':destination})['id']
    # Stable across reruns and restarts in this data directory. Retain the data
    # directory; deleting it also deletes the local duplicate-prevention record.
    request=str(uuid.uuid5(uuid.NAMESPACE_URL,'depositdesk:sandbox-acceptance:'+app.source+':'+destination))
    payment=app.draft({'request_id':request,'recipient_id':recipient,'amount':'0.01','reference':'SANDBOX-ACCEPTANCE-001'})
    before=payment['state']
    payment=app.submit(payment['id'],{'authorized':True,'amount':'0.01'})
    verification='not_attempted'
    if payment['provider_ref']:
        try:
            payment=app.action(payment['id'],'refresh',{})
            verification='matched'
        except Exception:
            verification='unconfirmed; inspect transfer legs in the sandbox dashboard'
    print(json.dumps({'environment':'dwolla_sandbox','payment_id':payment['id'],'provider_ref':payment['provider_ref'],
        'previous_state':before,'state':payment['state'],'transfer_verification':verification},indent=2))
    if verification!='matched':
        raise SystemExit(2)


if __name__=='__main__':
    main()
