"""Synthetic TCP canary, with no filesystem or management API exposed."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys

class Canary(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'runspindle-synthetic-canary\n')
    def log_message(self, *args):
        pass

HTTPServer((sys.argv[1], int(sys.argv[2])), Canary).serve_forever()
