import re, urllib.request, json, time
TB='http://192.168.8.111:8090'
CODE={0:'(power-on/HW/ext)',1:'boot-watchdog',2:'mesh-alone',3:'conn-mon-no-first-tick',
      4:'conn-mon-WEDGED',5:'max-recover-attempts',6:'lwm2m-device-reboot',7:'shell',
      8:'ip6-enable-fail',9:'thread-enable-fail',10:'dns-sd-boot-fail',11:'PANIC',99:'other'}
def login():
    r=urllib.request.Request(TB+'/api/auth/login',
      data=json.dumps({'username':'tenant@thingsboard.org','password':'tenant'}).encode(),
      headers={'Content-Type':'application/json'},method='POST')
    return json.loads(urllib.request.urlopen(r,timeout=15).read())['token']
tok=login()
devs=json.loads(urllib.request.urlopen(urllib.request.Request(
  TB+'/api/tenant/devices?pageSize=300&page=0',
  headers={'X-Authorization':f'Bearer {tok}'}),timeout=15).read())['data']
did={n:next((x['id']['id'] for x in devs if x['name']==f'ami-esp32c6-{n}'),None) for n in ('1494','f7b4','fbb8')}
def rint(d,p):
    req=urllib.request.Request(TB+f'/api/rpc/twoway/{d}',
      data=json.dumps({'method':'Read','params':{'id':p},'timeout':10000}).encode(),
      headers={'X-Authorization':f'Bearer {tok}','Content-Type':'application/json'},method='POST')
    try:
        r=json.loads(urllib.request.urlopen(req,timeout=18).read() or b'{}'); v=str(r.get('value',''))
        if 'OPAQUE' in v: m=re.search(r'value=([0-9a-fA-F]+)',v); return int(m.group(1),16) if m else None
        m=re.search(r'value=(-?\d+)',v); return int(m.group(1)) if m else None
    except Exception: return None
prev={}
for k in range(26):  # ~19 min @ 45s
    parts=[]
    for n in ('1494','f7b4','fbb8'):
        up=rint(did[n],'/33000/0/10'); tr=rint(did[n],'/33000/0/22'); rc=rint(did[n],'/33000/0/37')
        flag=''
        if tr is not None and prev.get(n) is not None and tr>prev[n]:
            flag=f' <<RESET! code={rc}={CODE.get(rc,rc)}'
        if tr is not None: prev[n]=tr
        parts.append(f'{n}:up={up}/TR={tr}/rbcode={rc}({CODE.get(rc,rc)}){flag}')
    print(f'[r{k:02d}] '+' | '.join(parts),flush=True)
    if k<25: time.sleep(45)
