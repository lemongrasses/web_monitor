"""Bounded HTTP server (web/server.py): idle connections cannot use up the process."""

import socket
import threading
import time
import unittest
import urllib.request
from unittest import mock


class BoundedServerTest(unittest.TestCase):
    def setUp(self):
        try:
            from aio_system_dashboard.web import server
        except ImportError as e:                      # werkzeug not installed here
            self.skipTest(str(e))
        p = mock.patch.object(server._Handler, "timeout", 1.0)
        p.start()
        self.addCleanup(p.stop)

        def app(environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]
        self.srv = server.BoundedWSGIServer("127.0.0.1", 0, app, max_connections=4, max_per_client=3)
        self.port = self.srv.server_address[1]
        t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.srv.shutdown)

    def idle(self, n):
        socks = []
        for _ in range(n):
            c = socket.create_connection(("127.0.0.1", self.port), timeout=2)
            c.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")          # never finishes
            socks.append(c)
        self.addCleanup(lambda: [c.close() for c in socks])
        time.sleep(0.3)
        return socks

    def held(self, socks):
        n = 0
        for c in socks:
            try:
                c.settimeout(0.05)
                if c.recv(1) != b"":
                    n += 1
            except socket.timeout:
                n += 1
            except OSError:
                pass
        return n

    def test_per_client_cap_and_idle_timeout(self):
        socks = self.idle(10)
        self.assertEqual(self.held(socks), 3)                       # the rest closed at once
        self.assertGreaterEqual(self.srv.refused, 7)
        time.sleep(1.5)                                             # idle timeout
        self.assertEqual(self.held(socks), 0)
        self.assertEqual(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=3).read(), b"ok")
