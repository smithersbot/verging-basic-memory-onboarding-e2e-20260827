"""Neutral staging scaffold for the Memory CI onboarding fixture.

This deliberately serves only health and build identity. The customer coding
agent must add the product adapter from the generated onboarding instructions.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            body = json.dumps(
                {
                    "ok": True,
                    "service": "basic-memory-onboarding-fixture",
                    "source": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "local"),
                }
            ).encode()
            self.send_response(200)
        else:
            body = json.dumps({"error": "not found"}).encode()
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
