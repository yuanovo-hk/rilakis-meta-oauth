import json, os, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen

GRAPH="https://graph.facebook.com/v27.0"; LARK="https://open.larksuite.com/open-apis"
BASE=os.environ["LARK_BASE_TOKEN"]; TABLE=os.environ["LARK_TABLE_ID"]
STATUS="flde84a6Iz"; CAPTION="fldoOsLRGd"; COVER="fldbVErWJZ"; MEDIA="fldv1INaWY"; PUB="fldyBfV5m2"; ERR="fldRZbOU5d"

def call(method,url,body=None,headers=None):
    req=Request(url, data=None if body is None else json.dumps(body).encode(), method=method,
                headers={"Content-Type":"application/json", **(headers or {})})
    with urlopen(req,timeout=45) as r: return json.loads(r.read().decode())

def lark_token():
    return call("POST",LARK+"/auth/v3/tenant_access_token/internal",
      {"app_id":os.environ["LARK_APP_ID"],"app_secret":os.environ["LARK_APP_SECRET"]})["tenant_access_token"]

def s(v):
    if isinstance(v,str): return v
    if isinstance(v,list): return "".join(s(x) for x in v)
    if isinstance(v,dict): return str(v.get("text") or v.get("url") or "")
    return "" if v is None else str(v)

def url(v):
    if isinstance(v,list) and v: v=v[0]
    return v.get("url") if isinstance(v,dict) else None

def run():
    lt=lark_token(); auth={"Authorization":"Bearer "+lt}
    rows=call("GET",f"{LARK}/bitable/v1/apps/{BASE}/tables/{TABLE}/records?page_size=500",headers=auth)["data"]["items"]
    out=[]
    for row in rows:
        f=row.get("fields",{}); status=s(f.get(STATUS))
        if status not in ("待發布","待发布"): continue
        image=url(f.get(COVER)); caption=s(f.get(CAPTION))
        if not image: out.append({"record_id":row["record_id"],"status":"skipped","reason":"missing cover URL"}); continue
        try:
            call("PUT",f"{LARK}/bitable/v1/apps/{BASE}/tables/{TABLE}/records/{row['record_id']}",
                 {"fields":{STATUS:["發布中"]}},auth)
            ig={"Authorization":"Bearer "+os.environ["INSTAGRAM_ACCESS_TOKEN"]}
            creation=call("POST",f"{GRAPH}/{os.environ['INSTAGRAM_ACCOUNT_ID']}/media",{"image_url":image,"caption":caption},ig)["id"]
            media=call("POST",f"{GRAPH}/{os.environ['INSTAGRAM_ACCOUNT_ID']}/media_publish",{"creation_id":creation},ig)["id"]
            call("PUT",f"{LARK}/bitable/v1/apps/{BASE}/tables/{TABLE}/records/{row['record_id']}",
                 {"fields":{STATUS:["已發布"],MEDIA:media,PUB:int(time.time()*1000)}},auth)
            out.append({"record_id":row["record_id"],"status":"published","media_id":media})
        except Exception as e:
            call("PUT",f"{LARK}/bitable/v1/apps/{BASE}/tables/{TABLE}/records/{row['record_id']}",
                 {"fields":{STATUS:["失敗"],ERR:str(e)[:500]}},auth)
            out.append({"record_id":row["record_id"],"status":"failed","error":str(e)[:500]})
    return {"processed":len(out),"results":out}

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        p=urlparse(self.path).path
        if p=="/health": self.out(200,{"ok":True})
        elif p=="/auth/callback": self.out(200,{"ok":True,"message":"Meta OAuth callback received"})
        else: self.out(404,{"error":"not found"})
    def do_POST(self):
        if urlparse(self.path).path!="/run": self.out(404,{"error":"not found"}); return
        secret=os.environ.get("RUN_SECRET","")
        if not secret or self.headers.get("X-Run-Secret")!=secret: self.out(401,{"error":"unauthorized"}); return
        try: self.out(200,run())
        except Exception as e: self.out(500,{"error":str(e)})
    def out(self,c,obj):
        b=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(c); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self,*a): pass

HTTPServer(("0.0.0.0",int(os.environ.get("PORT","3000"))),H).serve_forever()
