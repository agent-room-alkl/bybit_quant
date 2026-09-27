"""Read-only audit export. Credentials are read locally and never printed."""
from pathlib import Path
from datetime import datetime, timezone, timedelta
import json, urllib.parse, urllib.request, hmac, hashlib, time
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'data' / 'audit_2026-09-27'
OUT.mkdir(parents=True, exist_ok=True)
env, bare = {}, []
for line in (ROOT / '.env').read_text(encoding='utf-8-sig').splitlines():
    line = line.strip()
    if not line or line.startswith('#'): continue
    if '=' in line:
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    else: bare.append(line)
KEY = env.get('BYBIT_API_KEY') or bare[0]
SECRET = env.get('BYBIT_API_SECRET') or bare[1]
HOST = 'https://api.bybit.com'
server = json.load(urllib.request.urlopen(HOST + '/v5/market/time', timeout=20))
END = int(server['time'])
OFFSET = END - int(time.time()*1000)
START = int(datetime(2026, 7, 26, 12, tzinfo=timezone.utc).timestamp()*1000)

def save(name, data):
    (OUT / (name + '.json')).write_text(json.dumps(data, ensure_ascii=False, default=str), encoding='utf-8')

def get(path, params, private=True):
    qs = urllib.parse.urlencode(sorted(params.items()))
    for attempt in range(3):
        ts = str(int(time.time()*1000)+OFFSET)
        headers = {}
        if private:
            sig=hmac.new(SECRET.encode(),(ts+KEY+'10000'+qs).encode(),hashlib.sha256).hexdigest()
            headers={'X-BAPI-API-KEY':KEY,'X-BAPI-TIMESTAMP':ts,'X-BAPI-RECV-WINDOW':'10000','X-BAPI-SIGN':sig}
        try:
            req=urllib.request.Request(HOST+path+'?'+qs, headers=headers)
            data=json.load(urllib.request.urlopen(req,timeout=25))
            if data.get('retCode') == 0: return data
            if data.get('retCode') not in (10006,10016):
                raise RuntimeError('API retCode='+str(data.get('retCode')))
        except Exception:
            if attempt == 2: raise
        time.sleep(1+attempt)
    raise RuntimeError('API retry exhausted')

def export_db():
    raw=env.get('POSTGRES_URL_NON_POOLING') or env['POSTGRES_URL']
    p=urllib.parse.urlsplit(raw)
    q={k:v for k,v in urllib.parse.parse_qsl(p.query) if k in ('sslmode','connect_timeout')}
    q['sslmode']='require'
    dsn=urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,urllib.parse.urlencode(q),''))
    with psycopg.connect(dsn,connect_timeout=15,options='-c default_transaction_read_only=on -c statement_timeout=60000',row_factory=dict_row) as con:
        with con.cursor() as cur:
            cur.execute("SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='bybit_bot' ORDER BY table_name,ordinal_position")
            save('db_columns',cur.fetchall())
            summary={}
            for table in ('trades','signals'):
                cur.execute(f'SELECT COUNT(*) AS n, MIN(ts_ms) AS first_ms, MAX(ts_ms) AS last_ms FROM bybit_bot.{table}')
                summary[table]=cur.fetchone()
                cur.execute(f'SELECT * FROM bybit_bot.{table} WHERE ts_ms >= %s AND ts_ms <= %s ORDER BY ts_ms',(START,END))
                rows=cur.fetchall();save('db_'+table,rows)
                summary[table]['window_rows']=len(rows)
            cur.execute("SELECT key FROM bybit_bot.meta ORDER BY key")
            keys=[r['key'] for r in cur.fetchall()]
            save('db_meta_keys',keys)
            save('db_summary',summary)
            print(json.dumps({'database':summary}),flush=True)

def export_execs():
    rows=[]; windows=[]
    # Include earlier inventory history, distinct from the review window.
    start=START-180*86400000
    while start<END:
        end=min(start+7*86400000-1,END)
        params={'category':'spot','startTime':start,'endTime':end,'limit':100}
        count=0;seen=set()
        while True:
            data=get('/v5/execution/list',params)['result']
            batch=data.get('list',[]);rows.extend(batch);count+=len(batch)
            cursor=data.get('nextPageCursor')
            if not cursor or not batch:break
            if cursor in seen:raise RuntimeError('Repeated cursor')
            seen.add(cursor);params['cursor']=cursor
        windows.append({'start':start,'end':end,'rows':count})
        start=end+1
        if len(windows)%5==0:print(json.dumps({'execution_windows':len(windows),'rows':len(rows)}),flush=True)
    unique={r['execId']:r for r in rows}
    rows=sorted(unique.values(),key=lambda r:(int(r['execTime']),r['execId']))
    save('executions',rows);save('execution_coverage',windows)
    print(json.dumps({'executions_total':len(rows),'executions_window':sum(START<=int(r['execTime'])<=END for r in rows),'symbols':sorted(set(r['symbol'] for r in rows))}),flush=True)

if __name__ == '__main__':
    save('window',{'start_ms':START,'end_ms':END,'start_utc':datetime.fromtimestamp(START/1000,timezone.utc).isoformat(),'end_utc':datetime.fromtimestamp(END/1000,timezone.utc).isoformat()})
    for name,fn in [('database',export_db),('executions',export_execs),('wallet',lambda:save('wallet',get('/v5/account/wallet-balance',{'accountType':'UNIFIED'}))),('ticker',lambda:save('ticker',get('/v5/market/tickers',{'category':'spot','symbol':'SOLUSDT'},False)))]:
        try: fn()
        except Exception as exc: print(json.dumps({'step':name,'error_type':type(exc).__name__,'code':str(exc) if isinstance(exc,RuntimeError) else None}),flush=True)
