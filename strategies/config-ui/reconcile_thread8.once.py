"""Reconcile one verified, manually closed Analyze position; submits no orders."""
import fcntl,json,os,shutil,time
from pathlib import Path
from urllib.request import Request,urlopen
from runtime_control import BASE,LOCK,CONFIG,INTENTS,config_lock,atomic_json
from strategy_config import read_configuration,normalize_configuration
for raw in (BASE/'.env').read_text().splitlines():
    line=raw.strip()
    if line and not line.startswith('#') and '=' in line:
        key,value=line.split('=',1)
        os.environ.setdefault(key.strip(),value.strip().strip('"\''))
key=os.environ['OPENALGO_API_KEY']
name='THREAD-8';symbol='NIFTY13OCT2622550CE'
def read_api(endpoint,**payload):
    request=Request('http://127.0.0.1:5000/api/v1/'+endpoint,
        data=json.dumps({'apikey':key,**payload}).encode(),headers={'Content-Type':'application/json'},method='POST')
    with urlopen(request,timeout=20) as response:data=json.load(response)
    if data.get('status')!='success' or data.get('mode')!='analyze':
        raise RuntimeError('Reconciliation requires a successful Analyze-mode response')
    return data
with LOCK.open('a') as process_lock:
    fcntl.flock(process_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with config_lock():
        config=read_configuration(CONFIG)
        intents=json.loads(INTENTS.read_text())
        frozen=intents[name]
        row=next(w for w in config['workers'] if w['name']==name)
        assert frozen['underlying']=='NIFTY' and frozen['expiry_date']=='13OCT26' and frozen['strike_price']==22550 and frozen['option_type']=='CE'
        assert {**frozen,'enabled':True}=={**row,'enabled':True}
        position=read_api('openposition',strategy='NIFTY_22550_13OCT26_CE',symbol=symbol,exchange='NFO',product='NRML')
        assert float(position['quantity'])==0,'Position is still open; recovery flag retained'
        book=read_api('orderbook')['data']
        if isinstance(book,dict):book=book.get('orders',[])
        assert isinstance(book,list)
        orders=[r for r in book if r.get('symbol')==symbol and r.get('exchange')=='NFO' and r.get('product')=='NRML']
        terminal={'complete','cancelled','canceled','rejected','expired'}
        assert orders and all(str(r.get('order_status',r.get('status',''))).lower() in terminal for r in orders),'An order is unresolved; recovery flag retained'
        exit_order=next(r for r in orders if r.get('orderid')=='26100908463290')
        assert exit_order.get('order_status')=='complete' and exit_order.get('action')=='SELL' and int(exit_order.get('filled_quantity',0))==130
        # Recheck immediately before making the local configuration cleanup.
        assert float(read_api('openposition',strategy='NIFTY_22550_13OCT26_CE',symbol=symbol,exchange='NFO',product='NRML')['quantity'])==0
        backup=Path('/home/ubuntu/openalgo-change-backups')/('reconcile-thread8-'+str(int(time.time())))
        backup.mkdir()
        shutil.copy2(CONFIG,backup/'workers.json')
        shutil.copy2(INTENTS,backup/'runtime_stop_intents.json')
        audit={'worker':name,'symbol':symbol,'mode':'analyze','quantity':0,'working_orders':0,'verified_exit_order':'26100908463290','exit_filled_units':130,'checked_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
        atomic_json(backup/'audit.json',audit)
        row['enabled']=False
        atomic_json(CONFIG,normalize_configuration(config))
        intents.pop(name)
        atomic_json(INTENTS,intents)
        print(json.dumps({**audit,'recovery_flag_cleared':True,'enabled_at_next_start':False,'backup':str(backup)}))
