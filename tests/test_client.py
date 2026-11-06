from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from qev.client import Client, Noul


PAYLOAD = {"model": "test", "answers": {"ok": {"type": "noul", "noul": 0.5}}}


def test_query_values_are_url_encoded():
    client = Client()
    with patch.object(client, "_post", return_value=(PAYLOAD, {})) as post:
        client.system_one(None, {"ok": Noul()}, note="a &b=2/#中文", debug=False)
    query = parse_qs(urlsplit(post.call_args.args[0]).query)
    assert query == {"note": ["a &b=2/#中文"], "debug": ["false"]}


def test_request_id_header_is_case_insensitive():
    client = Client()
    headers = Message()
    headers["X-Typesafe-Request-Id"] = "request-123"
    with patch.object(client, "_post", return_value=(PAYLOAD, dict(headers))):
        response = client.system_one(None, {"ok": Noul()})
    assert response.request_id == "request-123"


@pytest.mark.parametrize("payload", [None, [], "error", {}, {"model": "test", "answers": []},
                                     {"model": [], "answers": {}}, {"model": "test", "answers": {"ok": None}}])
def test_malformed_response_shapes_raise_a_value_error(payload):
    with patch.object(Client, "_post", return_value=(payload, {})):
        with pytest.raises(ValueError, match="response"):
            Client().system_one("hello", {"ok": Noul()})


@pytest.mark.parametrize("method", ["_get", "_post"])
def test_truncated_http_response_is_a_retryable_transport_error(method):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b'{"status":')
            self.close_connection = True

        do_POST = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", timeout=2)
        with pytest.raises(URLError):
            getattr(client, method)("/health", *([{}] if method == "_post" else []))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
