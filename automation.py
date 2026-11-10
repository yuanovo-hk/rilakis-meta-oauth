"""Lark carousel publisher. Preview is read-only; each run targets one record."""
import hmac
import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

VERSION = "lark-carousel-v2"
LARK = "https://open.larksuite.com/open-apis"
GRAPH = "https://graph.facebook.com/v27.0"
LOCK = threading.Lock()
NAMES = {"fldNhqARYo": "IG輪播圖片", "flde84a6Iz": "IG發布狀態",
         "fldoOsLRGd": "IG貼文", "fldv1INaWY": "IG媒體ID",
         "fldel2Xwpt": "發文批次識別"}


def safe_error(value):
    value = str(value)
    for key in ("INSTAGRAM_ACCESS_TOKEN", "LARK_APP_SECRET", "RUN_SECRET"):
        secret = os.getenv(key)
        if secret:
            value = value.replace(secret, "[redacted]")
    return re.sub(r'https?://[^\s"<>]+', '[URL omitted]', value)[:1000]


def api(method, url, body=None, headers=None):
    request = Request(url, method=method, headers={"Content-Type": "application/json", **(headers or {})},
                      data=None if body is None else json.dumps(body).encode())
    try:
        with urlopen(request, timeout=45) as response:
            result = json.load(response)
    except HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {safe_error(detail)}") from None
    except (URLError, OSError) as error:
        raise RuntimeError(f"{method} {urlparse(url).path}: {safe_error(error)}") from None
    if "code" in result and result["code"] != 0:
        raise RuntimeError(f"Lark {result['code']}: {safe_error(result.get('msg', 'API error'))}")
    if "error" in result:
        raise RuntimeError(safe_error(result["error"]))
    return result


def cell(fields, name):
    if name in fields:
        return fields[name]
    return next((fields[k] for k, v in NAMES.items() if v == name and k in fields), None)


def text(value):
    if isinstance(value, list):
        return "".join(text(x) for x in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    return "" if value is None else str(value)


class Lark:
    def __init__(self):
        result = api("POST", LARK + "/auth/v3/tenant_access_token/internal",
                     {"app_id": os.environ["LARK_APP_ID"], "app_secret": os.environ["LARK_APP_SECRET"]})
        self.auth = {"Authorization": "Bearer " + result["tenant_access_token"]}
        self.base = f"{LARK}/bitable/v1/apps/{os.environ['LARK_BASE_TOKEN']}"
        self.records_url = self.base + f"/tables/{os.environ['LARK_TABLE_ID']}/records"

    def records(self):
        rows, page = [], ""
        while True:
            query = {"page_size": 500}
            if page:
                query["page_token"] = page
            data = api("GET", self.records_url + "?" + urlencode(query), headers=self.auth)["data"]
            rows.extend(data.get("items", []))
            if not data.get("has_more"):
                return rows
            page = data["page_token"]

    def update(self, rid, fields):
        # Raw OpenAPI single-select values are strings (CLI arrays are normalized by CLI).
        api("PUT", self.records_url + "/" + rid, {"fields": fields}, self.auth)

    def short_url(self, attachment, rid):
        source = attachment.get("tmp_url", "")
        parsed = urlparse(source)
        query = parse_qs(parsed.query)
        if parsed.hostname == "open.larksuite.com" and parsed.path.endswith("/batch_get_tmp_download_url"):
            params = {k: v[0] for k, v in query.items()}
        else:
            # Build the documented Bitable permission context when tmp_url is absent.
            meta = api("GET", self.base, headers=self.auth)["data"]["app"]
            params = {"file_tokens": attachment["file_token"], "extra": json.dumps({"bitablePerm": {
                "tableId": os.environ["LARK_TABLE_ID"], "rev": meta["revision"],
                "attachments": {"fldNhqARYo": {rid: [attachment["file_token"]]}}}})}
        result = api("GET", LARK + "/drive/v1/medias/batch_get_tmp_download_url?" + urlencode(params), headers=self.auth)
        for item in result["data"].get("tmp_download_urls", []):
            if item["file_token"] == attachment["file_token"]:
                url = item["tmp_download_url"]
                if urlparse(url).scheme != "https":
                    raise RuntimeError("Lark returned a non-HTTPS URL")
                return url
        raise RuntimeError("Lark did not return a temporary download URL")


def choose(rows, rid=None):
    eligible = [r for r in rows if text(cell(r["fields"], "IG發布狀態")) == "待發布"
                and not text(cell(r["fields"], "IG媒體ID"))
                and not text(cell(r["fields"], "發文批次識別"))]
    eligible.sort(key=lambda r: (r["fields"].get("發布順序", 1e9), r["record_id"]))
    return [r for r in eligible if r["record_id"] == rid] if rid else eligible[:1]


def prepare(lark, row):
    fields, rid = row["fields"], row["record_id"]
    attachments = cell(fields, "IG輪播圖片") or []
    if not 2 <= len(attachments) <= 10:
        raise RuntimeError("IG輪播圖片 must contain 2–10 images")
    caption = text(cell(fields, "IG貼文"))
    if not caption or len(caption) > 2200:
        raise RuntimeError("IG貼文 must contain 1–2200 characters")
    images = []
    for attachment in attachments:
        try:
            url = lark.short_url(attachment, rid)
        except Exception as error:
            raise RuntimeError(f"Resolve {attachment['name']}: {safe_error(error)}") from None
        # Deliberately omit Authorization: proves this URL works for a public client.
        for attempt in range(2):
            try:
                with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"}), timeout=30) as response:
                    mime = response.headers.get_content_type()
                    response.read(32)
                break
            except (URLError, OSError) as error:
                if attempt == 1:
                    raise RuntimeError(f"Public download {attachment['name']} ({urlparse(url).hostname}): {safe_error(error)}") from None
        if not mime.startswith("image/"):
            raise RuntimeError("Lark public download did not return an image")
        images.append({"name": attachment["name"], "mime": mime, "url": url})
    return caption, images


def wait_ready(ig, container):
    for _ in range(30):
        status = api("GET", f"{GRAPH}/{container}?fields=status_code,status", headers=ig)
        if status.get("status_code") == "FINISHED":
            return
        if status.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(safe_error(status))
        time.sleep(2)
    raise RuntimeError("Instagram processing timed out; check container before retrying")


def execute(payload):
    lark = Lark()
    rows = choose(lark.records(), payload.get("record_id"))
    if payload.get("publish") is not True:
        previews = []
        for row in rows:
            caption, images = prepare(lark, row)
            previews.append({"record_id": row["record_id"], "caption_length": len(caption),
                             "images": [{k: i[k] for k in ("name", "mime")} for i in images]})
        return {"version": VERSION, "mode": "preview", "records": previews}
    if not payload.get("record_id") or len(rows) != 1:
        raise RuntimeError("Publishing requires one explicit eligible record_id")
    row, stage = rows[0], "prepare"
    rid = row["record_id"]
    caption, images = prepare(lark, row)
    # Claim must be successfully written AND read back before any Instagram writes.
    batch = "run:" + uuid.uuid4().hex
    lark.update(rid, {"IG發布狀態": "發布中", "發文批次識別": batch, "錯誤紀錄": ""})
    claimed = api("GET", lark.records_url + "/" + rid, headers=lark.auth)["data"]["record"]["fields"]
    if text(cell(claimed, "發文批次識別")) != batch:
        raise RuntimeError("Lark claim readback failed; publishing stopped")
    ig = {"Authorization": "Bearer " + os.environ["INSTAGRAM_ACCESS_TOKEN"]}
    try:
        stage = "create_children"
        children = []
        for image in images:
            children.append(api("POST", f"{GRAPH}/{os.environ['INSTAGRAM_ACCOUNT_ID']}/media",
                                {"image_url": image["url"], "is_carousel_item": True}, ig)["id"])
        for child in children:
            wait_ready(ig, child)
        parent = api("POST", f"{GRAPH}/{os.environ['INSTAGRAM_ACCOUNT_ID']}/media",
                     {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption}, ig)["id"]
        lark.update(rid, {"發文批次識別": "container:" + parent})
        wait_ready(ig, parent)
        stage = "publish"
        # Never automatically repeat this non-idempotent call.
        media = api("POST", f"{GRAPH}/{os.environ['INSTAGRAM_ACCOUNT_ID']}/media_publish", {"creation_id": parent}, ig)["id"]
        stage = "writeback"
        lark.update(rid, {"IG媒體ID": media, "IG發布狀態": "已發布", "實際發布時間": int(time.time()*1000)})
        try:
            link = api("GET", f"{GRAPH}/{media}?fields=permalink", headers=ig).get("permalink")
            if link:
                lark.update(rid, {"IG貼文連結": {"link": link, "text": "Instagram"}})
        except Exception:
            pass  # Media ID and published status are already saved; never republish.
        return {"record_id": rid, "media_id": media, "status": "published"}
    except Exception as error:
        detail = stage + ": " + safe_error(error)
        try:
            lark.update(rid, {"IG發布狀態": "需核對" if stage in ("publish", "writeback") else "失敗", "錯誤紀錄": detail})
        except Exception as write_error:
            detail += "; writeback: " + safe_error(write_error)
        raise RuntimeError(detail) from None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, obj):
        blob = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            self.respond(200, {"ok": True, "version": VERSION})
        elif urlparse(self.path).path == "/auth/callback":
            self.respond(200, {"message": "Callback received; token exchange is not implemented here"})
        else:
            self.respond(404, {"error": "not found"})

    def do_POST(self):
        secret = os.getenv("RUN_SECRET", "")
        if not secret or not hmac.compare_digest(self.headers.get("X-Run-Secret", ""), secret):
            self.respond(401, {"error": "unauthorized"})
            return
        if urlparse(self.path).path != "/run":
            self.respond(404, {"error": "not found"})
            return
        if not LOCK.acquire(blocking=False):
            self.respond(409, {"error": "A run is already in progress"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 <= length <= 4096:
                raise ValueError("Invalid body length")
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.respond(200, execute(payload))
        except Exception as error:
            self.respond(500, {"error": safe_error(error), "version": VERSION})
        finally:
            LOCK.release()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "3000"))), Handler).serve_forever()
