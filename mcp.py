"""Tiny client for the Gearmulator plugin MCP server (localhost)."""
import json, urllib.error, urllib.request

class Mcp:
    def __init__(self, port=13710, resolve=None):
        """resolve() -> current port: after a refused connection (plugin reloaded, e.g. an
        Ableton restart moved it 13711 -> 13710 on 2026-10-14) re-resolve and reconnect."""
        self.resolve = resolve
        self.connect(port)

    def connect(self, port):
        self.url = f"http://127.0.0.1:{port}/message"
        self.n = 0
        self._rpc("initialize", {"protocolVersion": "2024-11-05",
                                 "clientInfo": {"name": "mdmm-bsp", "version": "1"}, "capabilities": {}})

    def rpc(self, method, params=None):
        try:
            return self._rpc(method, params)
        except urllib.error.URLError as e:
            if not (self.resolve and isinstance(e.reason, ConnectionRefusedError)):
                raise
            self.connect(self.resolve())       # raises again while the plugin is still away
            print(f"MCP reconnected: {self.url}", flush=True)
            return self._rpc(method, params)

    def _rpc(self, method, params=None):
        self.n += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read() or b"null")

    def tool(self, _tool, **args):
        res = self.rpc("tools/call", {"name": _tool, "arguments": args})
        if res and "error" in res:
            raise RuntimeError(res["error"])
        content = (res or {}).get("result", {}).get("content", [])
        text = "".join(c.get("text", "") for c in content)
        try:
            return json.loads(text)
        except ValueError:
            return text
