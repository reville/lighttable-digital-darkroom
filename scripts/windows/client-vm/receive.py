"""Bounded evidence receiver, listening inside a disposable VM container only."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import time

ROOT = Path('/evidence')


class Handler(BaseHTTPRequestHandler):
    def do_PUT(self):
        relative = self.path.lstrip('/')
        path = (ROOT / relative).resolve()
        size = int(self.headers.get('Content-Length', '0'))
        if not path.is_relative_to(ROOT) or size > 16 * 1024 * 1024:
            self.send_error(400)
            return
        if relative != 'complete' and path.suffix not in ('.json', '.png', '.log', '.tif'):
            self.send_error(400)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.rfile.read(size))
        self.send_response(200)
        self.end_headers()


server = HTTPServer(('0.0.0.0', 18080), Handler)
server.timeout = 5
deadline = time.monotonic() + 3300
while time.monotonic() < deadline and not (ROOT / 'complete').exists():
    server.handle_request()
server.server_close()
