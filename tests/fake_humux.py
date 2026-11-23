"""Fake HueMux bridge for the deckstack-lights tests (stdlib only).

Stands in for the lights.lan bridge (huemux) over plain HTTP on 127.0.0.1:<ephemeral>.
The deckstack daemon's host-mediated fetch performs plain-http GET/HEAD only
(crates/ds-daemon/src/plugins/host.rs ``http_fetch``), so the control surface exercised
here is the GET-query API:

    GET /api/rooms                                  -> {"rooms": [...]}
    GET /api/lights?room=<room>                     -> {"room": ..., "lights": [...]}
    GET /api/lights/set?room=&light=&on=&brightness=&color=   -> applies, {"ok": true, ...}
    GET /api/state                                  -> the whole applied state

Every request is recorded in ``requests`` (list of {method, path, query}) so tests can
assert exactly what the plugin asked the bridge to do — including that a refused room
never produced a request.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeHueMux:
    def __init__(self, rooms=None):
        # rooms/light ids are data the tests hand in — the plugin never hardcodes them
        self.rooms = rooms or {"salon": ["ceiling", "desk"], "kitchen": ["pendant"]}
        self.state = {}          # (room, light) -> {"on":..., "brightness":..., "color":...}
        self.requests = []       # every HTTP request, in order
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                with outer.lock:
                    outer.requests.append({"method": "GET", "path": u.path, "query": q})
                if u.path == "/api/rooms":
                    self._json(200, {"rooms": sorted(outer.rooms)})
                elif u.path == "/api/lights":
                    room = q.get("room", "")
                    self._json(200, {"room": room,
                                     "lights": sorted(outer.rooms.get(room, []))})
                elif u.path == "/api/lights/set":
                    room, light = q.get("room"), q.get("light")
                    if room not in outer.rooms or light not in outer.rooms.get(room, []):
                        self._json(404, {"ok": False, "error": "unknown room/light"})
                        return
                    st = outer.state.setdefault((room, light), {"on": True})
                    if "on" in q:
                        st["on"] = q["on"] == "true"
                    if "brightness" in q:
                        st["brightness"] = int(q["brightness"])
                    if "color" in q:
                        st["color"] = q["color"]
                    self._json(200, {"ok": True, "room": room, "light": light,
                                     "state": dict(st)})
                elif u.path == "/api/state":
                    with outer.lock:
                        snap = {"%s/%s" % k: v for k, v in outer.state.items()}
                    self._json(200, {"ok": True, "state": snap})
                else:
                    self._json(404, {"ok": False, "error": "not found"})

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="fake-humux", daemon=True)
        self.thread.start()

    def set_requests(self, path_prefix="/api/lights/set"):
        with self.lock:
            return [r for r in self.requests if r["path"] == path_prefix]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
