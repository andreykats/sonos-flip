"""HTTP API.

  GET  /health    200 once the speakers are found, 503 (with the reason) until then
  GET  /status    current mode and the raw bond state
  GET  /soundbar  what the soundbar is playing (to check tv_source_prefix)
  POST /desk      switch to desk (stereo pair)
  POST /theater   switch to theater (soundbar surrounds)

Responses are JSON. Errors: {"error": "..."} with 404/405 (bad request), 503 (not ready yet),
502 (a speaker failed or didn't answer), 500 (a bug; the traceback is in the log).
"""
import json
import logging
import time
from http.server import BaseHTTPRequestHandler

from . import __version__
from .sonos import SonosError

log = logging.getLogger(__name__)


def make_handler(flip):
    actions = {
        ("GET", "/status"): flip.state,
        ("GET", "/soundbar"): flip.soundbar_transport,
        ("POST", "/desk"): flip.to_desk,
        ("POST", "/theater"): flip.to_theater,
    }
    usage = "GET /health, GET /status, GET /soundbar, POST /desk, POST /theater"

    class Handler(BaseHTTPRequestHandler):
        server_version = f"sonos-flip/{__version__}"

        def _send(self, code, obj):
            data = json.dumps(obj, indent=2).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                log.debug("Client went away before the response to %s %s", self.command, self.path)

        def _route(self, method):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/health":
                if flip.ready:
                    return self._send(200, {"status": "ok", "version": __version__})
                return self._send(503, {"status": "starting", "version": __version__,
                                        "error": flip.startup_error or "looking for the speakers"})
            fn = actions.get((method, path))
            if fn is None:
                other = [m for m, p in actions if p == path]
                if other:
                    return self._send(405, {"error": f"use {other[0]} {path}"})
                return self._send(404, {"error": f"unknown path {path}; endpoints: {usage}"})
            if not flip.ready:
                return self._send(503, {"error": "not ready: " + (flip.startup_error or "looking for the speakers")})
            if method == "POST":
                flip.last_request = time.monotonic()   # newer than any pending automatic switch to desk
                log.info("%s %s requested by %s", method, path, self.client_address[0])
            try:
                self._send(200, fn())
            except SonosError as e:
                if method == "GET":                     # switches log their own failures
                    log.warning("%s %s failed: %s", method, path, e)
                self._send(502, {"error": str(e)})
            except Exception as e:
                if method == "GET":
                    log.exception("%s %s failed unexpectedly", method, path)
                self._send(500, {"error": f"internal error: {e!r} (details in the log)"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

    return Handler
