import re, urllib.request, json
TB='http://192.168.8.111:8090'
def login():
    r=urllib.request.Request(TB+'/api/auth/login',
      data=json.dumps({'username':'tenant@thingsboard.org','password':'tenant'}).encode(),
      headers={'Content-Type':'application/json'},method='POST')
    return json.loads(urllib.request.urlopen(r,timeout=15).read())['token']
def did(tok,name):
    devs=json.loads(urllib.request.urlopen(urllib.request.Request(
      TB+'/api/tenant/devices?pageSize=300&page=0',
      headers={'X-Authorization':f'Bearer {tok}'}),timeout=15).read())['data']
    return next(d['id']['id'] for d in devs if d['name']==name)
def rpc(tok,d,path):
    req=urllib.request.Request(TB+f'/api/rpc/twoway/{d}',
      data=json.dumps({'method':'Read','params':{'id':path},'timeout':20000}).encode(),
      headers={'X-Authorization':f'Bearer {tok}','Content-Type':'application/json'},method='POST')
    try:
        r=json.loads(urllib.request.urlopen(req,timeout=30).read() or b'{}')
        v=str(r.get('value',''))
        m=re.search(r'value=([0-9a-fA-F]+).*OPAQUE',v) or re.search(r'OPAQUE.*value=([0-9a-fA-F]+)',v)
        if 'OPAQUE' in v:
            mm=re.search(r'value=([0-9a-fA-F]+)',v)
            return (int(mm.group(1),16) if mm else None, v.strip())
        m=re.search(r'value=(-?\d+)',v)
        return (int(m.group(1)) if m else None, v.strip())
    except Exception as e:
        return (None, f'ERR {e}')
tok=login(); d=did(tok,'ami-esp32c6-1494')
rids={21:'reset_reason',17:'lwm2m_last_err',18:'lwm2m_err_uptime',
      23:'HANG_uptime',24:'HANG_heap_free',25:'HANG_heap_min',26:'HANG_reg_age',
      27:'HANG_lwm2m_state',28:'HANG_thread_role'}
for r,name in rids.items():
    val,raw=rpc(tok,d,f'/33001/0/{r}' if False else f'/33000/0/{r}')
    print(f'  RID {r:2d} {name:18s} = {val}   [{raw[:80]}]')
