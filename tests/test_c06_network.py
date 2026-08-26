"""C6.1 network/Telegram fake-server corpus."""
from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch


class FakeRSSHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if "redirect" in self.path:
            self.send_response(302)
            self.send_header("Location", "/rss.xml")
            self.end_headers()
            return
        if "large" in self.path:
            self.send_response(200)
            self.send_header("Content-Length", "999999999")
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            self.wfile.write(b"x"*100)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.end_headers()
        self.wfile.write(b'<?xml version="1.0"?><rss><channel><item><title>Test</title><link>https://example.com/a</link></item></channel></rss>')
    def log_message(self, *a, **kw): pass


class NetworkCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeRSSHandler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_fetch_success(self):
        from meco_news.network import BoundedHTTPClient
        from meco_news.config import CollectionLimits, NetworkPolicy
        client = BoundedHTTPClient(CollectionLimits(), NetworkPolicy(require_https=False), allow_private_for_tests=True)
        resp = client.fetch(f"http://127.0.0.1:{self.port}/rss.xml")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"Test", resp.payload)

    def test_redirect(self):
        from meco_news.network import BoundedHTTPClient
        from meco_news.config import CollectionLimits, NetworkPolicy
        # same_host redirect should succeed, cross-host without allowlist should fail
        client = BoundedHTTPClient(CollectionLimits(max_redirects=2), NetworkPolicy(same_host_redirects_only=True, require_https=False), allow_private_for_tests=True)
        # This will redirect to /rss.xml on same host — should succeed
        resp = client.fetch(f"http://127.0.0.1:{self.port}/redirect")
        self.assertEqual(resp.status, 200)

    def test_large_content_length_rejected(self):
        from meco_news.network import BoundedHTTPClient, ResponseTooLarge
        from meco_news.config import CollectionLimits, NetworkPolicy
        client = BoundedHTTPClient(CollectionLimits(response_bytes=10), NetworkPolicy(require_https=False), allow_private_for_tests=True)
        with self.assertRaises(ResponseTooLarge) as error:
            client.fetch(f"http://127.0.0.1:{self.port}/large")
        self.assertEqual(error.exception.reason_code, "response_too_large")

    def test_telegram_fake_server(self):
        from meco_news.telegram import TelegramClient, TelegramSendError
        import json as js

        class TGHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = js.loads(body) if body else {}
                if "getMe" in self.path:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(js.dumps({"ok": True, "result": {"username": "testbot"}}).encode())
                elif "sendMessage" in self.path:
                    text = data.get("text", "")
                    if "rate" in text:
                        self.send_response(429)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(js.dumps({"ok": False, "error_code": 429, "parameters": {"retry_after": 5}}).encode())
                    elif "fail" in text:
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(b"error")
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(js.dumps({"ok": True, "result": {"message_id": 123}}).encode())
                else:
                    self.send_response(404)
                    self.end_headers()
            def log_message(self, *a, **kw): pass

        s = HTTPServer(("127.0.0.1", 0), TGHandler)
        port = s.server_port
        t = threading.Thread(target=s.serve_forever, daemon=True)
        t.start()
        try:
            client = TelegramClient("123456:fake-token-12345678901234567890", "123", timeout=5)
            # Patch base_url to point to fake server
            client.base_url = f"http://127.0.0.1:{port}/bot123456:fake-token-12345678901234567890"
            # getMe
            me = client.get_me()
            self.assertEqual(me["username"], "testbot")
            # send success
            mid = client.send_html("<b>hello</b>")
            self.assertEqual(mid, "123")
            # rate limited
            with self.assertRaises(TelegramSendError) as cm:
                client.send_html("rate test")
            self.assertEqual(cm.exception.reason_code, "telegram_rate_limited")
            # 5xx -> ambiguous
            with self.assertRaises(TelegramSendError) as cm:
                client.send_html("fail test")
            self.assertEqual(cm.exception.reason_code, "telegram_ambiguous")
        finally:
            s.shutdown()

    def test_url_policy(self):
        from meco_news.urls import validate_url, URLPolicyError
        with self.assertRaises(URLPolicyError):
            validate_url("https://user:pass@example.com/")
        with self.assertRaises(URLPolicyError):
            validate_url("https://example.com:99999/")
        with self.assertRaises(URLPolicyError):
            validate_url("https://[invalid]/")
        # allowed private
        self.assertIsNotNone(validate_url("http://127.0.0.1/test", allow_private=True, allow_http=True))
