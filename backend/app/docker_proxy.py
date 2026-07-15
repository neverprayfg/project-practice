from __future__ import annotations

import http.client
import os
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

_API_PREFIX = r"(?:/v[0-9.]+)?"
_POLICIES = {
    "GET": (
        re.compile(r"^/_ping$"),
        re.compile(r"^/version$"),
        re.compile(rf"^{_API_PREFIX}/images/[^/]+/json$"),
        re.compile(rf"^{_API_PREFIX}/containers/[0-9a-f]+/(?:json|logs)$"),
    ),
    "POST": (
        re.compile(rf"^{_API_PREFIX}/containers/create$"),
        re.compile(rf"^{_API_PREFIX}/containers/[0-9a-f]+/(?:start|wait)$"),
    ),
    "DELETE": (re.compile(rf"^{_API_PREFIX}/containers/[0-9a-f]+$"),),
}


def is_allowed(method: str, target: str) -> bool:
    path = urlsplit(target).path
    return any(pattern.fullmatch(path) for pattern in _POLICIES.get(method, ()))


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout_seconds: int) -> None:
        super().__init__("localhost", timeout=timeout_seconds)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class DockerProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RestrictedDockerProxy/0.1"

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def _forward(self) -> None:
        if not is_allowed(self.command, self.path):
            self.send_error(403, "Docker API operation is not allowed")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > 1_048_576:
            self.send_error(413, "request body is too large")
            return
        body = self.rfile.read(content_length) if content_length else None
        timeout_seconds = max(
            60,
            min(int(os.environ.get("DOCKER_PROXY_TIMEOUT_SECONDS", "1200")), 3600),
        )
        connection = UnixHTTPConnection(
            os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock"),
            timeout_seconds,
        )
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() in {"content-type", "content-length", "user-agent"}
        }
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
        except (OSError, http.client.HTTPException) as exc:
            self.send_error(502, f"Docker Engine unavailable: {exc}")
            return
        finally:
            connection.close()

        self.send_response(response.status)
        self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        print(f"docker-proxy: {format % args}", flush=True)


def main() -> None:
    host = os.environ.get("DOCKER_PROXY_HOST", "0.0.0.0")
    port = int(os.environ.get("DOCKER_PROXY_PORT", "2375"))
    ThreadingHTTPServer((host, port), DockerProxyHandler).serve_forever()


if __name__ == "__main__":
    main()
