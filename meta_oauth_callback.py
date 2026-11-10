#!/usr/bin/env python3
"""Small callback receiver for Meta OAuth testing."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import html
import os

HOST = os.getenv("OAUTH_HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", os.getenv("OAUTH_PORT", "3000")))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("oauth-callback: request received")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/auth/callback":
            self.send_error(404, "Not found")
            return
        query = parse_qs(parsed.query)
        if query.get("error"):
            title = "Meta 授權未完成"
            body = html.escape(query.get("error_description", [query["error"][0]])[0])
        elif query.get("code"):
            title = "Meta 授權回呼成功"
            body = "已收到 authorization code，請回到自動化程式繼續交換。"
        else:
            title = "缺少授權參數"
            body = "沒有收到 Meta 的 code 或 error。"
        page = f"<!doctype html><meta charset='utf-8'><title>{title}</title><h1>{title}</h1><p>{body}</p>"
        data = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print(f"OAuth callback listening on port {PORT}")
    HTTPServer((HOST, PORT), Handler).serve_forever()
