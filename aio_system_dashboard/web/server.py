"""HTTP server for the two sites: a thread per connection, but bounded.

Found by the adversarial run: 1100 connections that never finished their request (werkzeug waits
for the headers for ever, and the access list is only checked after them) used up the process's
1024 file descriptors. Both sites stopped answering and the collectors failed to read /proc; the
start-up coordinator would not have been able to save its state or start AIO NAV either.

* a client that sends nothing for IDLE_TIMEOUT_S is dropped (slow or idle connections);
* at most MAX_CONNECTIONS at once per site and MAX_PER_CLIENT per client address; a connection
  over the limit is closed at once, before a thread is started.
The core keeps its file descriptors whatever happens on the web ports.
"""

import logging
import threading
from collections import Counter

from werkzeug.serving import ThreadedWSGIServer, WSGIRequestHandler

logger = logging.getLogger(__name__)

MAX_CONNECTIONS = 64
MAX_PER_CLIENT = 16
IDLE_TIMEOUT_S = 20.0


class _Handler(WSGIRequestHandler):
    timeout = IDLE_TIMEOUT_S        # socket timeout for reading the request and writing the answer


class BoundedWSGIServer(ThreadedWSGIServer):
    def __init__(self, host: str, port: int, app, max_connections: int = MAX_CONNECTIONS,
                 max_per_client: int = MAX_PER_CLIENT):
        super().__init__(host, port, app, handler=_Handler)
        self.max_connections, self.max_per_client = max_connections, max_per_client
        self._active = Counter()                     # client address -> open connections
        self._lock = threading.Lock()
        self.refused = 0

    def process_request(self, request, client_address):
        ip = client_address[0] if client_address else ""
        with self._lock:
            full = (sum(self._active.values()) >= self.max_connections
                    or self._active[ip] >= self.max_per_client)
            if not full:
                self._active[ip] += 1
            else:
                self.refused += 1
        if full:
            if self.refused % 100 == 1:
                logger.warning("web connection from %s refused: too many open connections", ip)
            self.shutdown_request(request)           # closed before any thread is started
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._done(ip)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._done(client_address[0] if client_address else "")

    def _done(self, ip: str) -> None:
        with self._lock:
            self._active[ip] -= 1
            if self._active[ip] <= 0:
                del self._active[ip]
