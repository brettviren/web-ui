"""A tiny HTTP backend for tests: echoes the request head as JSON.

A request with ``Upgrade:`` gets ``101`` and then has its bytes echoed back.
Usage: echo_backend.py --socket PATH | --port N
"""

import argparse
import json
import socketserver


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            line = self.rfile.readline()
            if not line:
                return
            head += line
        lines = head.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ")
        headers = {}
        for line in lines[1:]:
            if line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        if "upgrade" in headers:
            self.wfile.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: test\r\nConnection: Upgrade\r\n\r\n")
            self.wfile.flush()
            while data := self.rfile.read1(4096):
                self.wfile.write(data)
                self.wfile.flush()
            return
        n = int(headers.get("content-length", 0))
        body_in = self.rfile.read(n) if n else b""
        body = json.dumps({"method": method, "target": target, "headers": headers, "body": body_in.decode()}).encode()
        self.wfile.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )


class Unix(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class TCP(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    srv = Unix(a.socket, Handler) if a.socket else TCP(("127.0.0.1", a.port), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
