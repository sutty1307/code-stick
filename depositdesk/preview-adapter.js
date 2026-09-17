// Offline preview only. This file is never loaded by the Python application.
// It intercepts the demo's API calls locally. No account, banking or network access.
(() => {
  const now = () => new Date().toISOString();
  const uid = () => crypto.randomUUID();
  const rid = uid();
  const demo = {
    csrf:'offline-preview', mode:'simulator', live_enabled:false, preview_mode:true,
    business_name:'Northline Studio · Demo', source_label:'Simulated business account',
    recipients:[{id:rid,name:'Sample contractor',kind:'contractor',account_label:'Example checking',authorization_ref:'TEST-AUTH-001',created_at:now()}],
    payments:[]
  };
  const history = new Map(), requests = new Map();
  const record = (id, detail) => {if(!history.has(id))history.set(id,[]);history.get(id).push({action:'demo_event',detail,created_at:now()});};
  const fail = message => {throw new Error(message);};
  const amount = value => {
    if(typeof value!=='string'||! /^(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,2})?$/.test(value))fail('Enter a dollar amount with at most two decimal places.');
    const [whole,fraction='']=value.split('.'), n=Number(whole)*100+Number(fraction.padEnd(2,'0'));
    if(n<1||n>2500000)fail('Use a test amount between $0.01 and $25,000.00.');
    return n;
  };
  const draft = data => {
    const signature=JSON.stringify(data),existing=requests.get(data.request_id);
    if(existing){if(existing.signature!==signature)fail('This request already belongs to another draft.');return demo.payments.find(p=>p.id===existing.id);}
    const recipient=demo.recipients.find(r=>r.id===data.recipient_id);
    if(!recipient)fail('Choose a test recipient.');
    const reference=String(data.reference||'').trim();
    if(!reference)fail('Enter a payment reference.');
    if(demo.payments.some(p=>p.recipient_id===recipient.id&&p.reference.toLocaleLowerCase()===reference.toLocaleLowerCase()))fail('This recipient already has a payment with that reference.');
    const payment={id:uid(),recipient_id:recipient.id,recipient_name:recipient.name,account_label:recipient.account_label,
      authorization_ref:recipient.authorization_ref,amount_cents:amount(data.amount),reference,memo:data.memo||'',
      state:'draft',provider_ref:null,message:'',refresh_due:0,created_at:now(),updated_at:now(),approved_at:null};
    demo.payments.unshift(payment);requests.set(data.request_id,{id:payment.id,signature});record(payment.id,'Demo draft saved for review. No submission.');return payment;
  };
  draft({request_id:uid(),recipient_id:rid,amount:'125.50',reference:'TEST-INVOICE-001',memo:'Example payment for reviewing the workflow.'});
  const samples = [
    ['Studio Juniper','INV-2026-118','2450.00','pending'],
    ['Ellis Morgan','SEP-PAY-004','875.50','draft'],
    ['Northline Tools','PO-00487','349.95','processed'],
    ['Studio Juniper','INV-2026-104','1280.00','processed'],
    ['Ellis Morgan','SEP-REIMB-02','160.00','returned'],
    ['Ridge Supply','PO-00492','620.00','pending'],
    ['Ridge Supply','PO-00493','94.25','failed']
  ];
  samples.forEach(([name,reference,value,status],index)=>{
    let r=demo.recipients.find(item=>item.name===name);
    if(!r){r={id:uid(),name,kind:'vendor',account_label:'Fictional checking',authorization_ref:'DEMO-AUTH-'+(index+1),created_at:now()};demo.recipients.push(r);}
    const p=draft({request_id:uid(),recipient_id:r.id,amount:value,reference,memo:'Fictional example record.'});
    p.state=status;p.created_at=new Date(Date.now()-(index+1)*86400000).toISOString();
    if(status!=='draft'){p.provider_ref='simulated:'+p.id;p.approved_at=p.created_at;record(p.id,'Simulated '+status+' outcome. No bank transaction occurred.');}
  });
  window.fetch=async(path,options={})=>{
    let body={},status=200;
    try{
      if(typeof path!=='string'||!path.startsWith('/api/'))fail('External requests are disabled in this preview.');
      const data=options.body?JSON.parse(options.body):{};
      if(path==='/api/state')body=demo;
      else if(path==='/api/recipients'){
        if(!data.acknowledged)fail('Confirm the test authorization reference.');
        for(const key of ['name','account_label','authorization_ref'])if(!String(data[key]||'').trim())fail('Complete all recipient fields.');
        body={id:uid(),name:String(data.name).trim(),kind:data.kind,account_label:String(data.account_label).trim(),authorization_ref:String(data.authorization_ref).trim(),created_at:now()};demo.recipients.unshift(body);
      }else if(path==='/api/payments')body=draft(data);
      else{
        const match=path.match(/^\/api\/payments\/([0-9a-f-]+)(?:\/(submit|cancel|simulate))?$/);
        if(!match)fail('This action is unavailable in the offline preview.');
        const p=demo.payments.find(p=>p.id===match[1]);if(!p)fail('Test payment not found.');
        if(!match[2])body={payment:p,audit:history.get(p.id)||[]};
        else{
          if(match[2]==='submit'){
            if(!data.authorized||amount(data.amount)!==p.amount_cents)fail('Review the exact amount first.');
            if(p.state==='draft'){p.state='pending';p.approved_at=now();p.provider_ref='simulated:'+p.id;p.message='Demo submission recorded. No bank transaction occurred.';record(p.id,p.message);}
          }else if(match[2]==='cancel'){
            if(p.state!=='draft')fail('Only an unsubmitted draft can be cancelled.');
            p.state='cancelled';p.message='Demo draft cancelled before submission.';record(p.id,p.message);
          }else{
            const transitions={pending:['processed','failed'],processed:['returned']};
            if(!(transitions[p.state]||[]).includes(data.state))fail('This simulated transition is not allowed.');
            p.state=data.state;p.message='Simulated '+data.state+' outcome. No bank transaction occurred.';record(p.id,p.message);
          }
          p.updated_at=now();body=p;
        }
      }
    }catch(error){status=400;body={error:error.message};}
    const copied=JSON.parse(JSON.stringify(body));
    return {ok:status===200,status,json:async()=>copied};
  };
  document.addEventListener('click',event=>{
    const a=event.target.closest('a');if(!a||a.getAttribute('href')!=='/api/export.csv')return;
    event.preventDefault();
    const safe=value=>{let s=String(value);if(/^[=+@-]/.test(s.trimStart()))s="'"+s;return '"'+s.replace(/"/g,'""')+'"';};
    const rows=[['environment','payment_id','recipient','reference','amount_usd','status'],...demo.payments.map(p=>['offline_demo',p.id,p.recipient_name,p.reference,(p.amount_cents/100).toFixed(2),p.state])];
    const url=URL.createObjectURL(new Blob([rows.map(r=>r.map(safe).join(',')).join('\r\n')],{type:'text/csv'}));
    const download=document.createElement('a');download.href=url;download.download='depositdesk-demo-payments.csv';download.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  document.addEventListener('DOMContentLoaded',()=>{
    document.getElementById('sign-out').hidden=true;
    document.getElementById('env-badge').textContent='Offline demo';
    document.getElementById('mode-banner').textContent='Interactive demo · Fictional records only. Reloading resets all changes. No bank connection or saved account.';
  });
})();
