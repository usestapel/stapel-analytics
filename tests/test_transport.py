"""The webhook adapter's POST transport — the URL policy, without a socket.

The collector URL comes from settings rather than from a row, so this is a
far less hostile dial than a webhook subscription target. It still carries
the IP guard, because a settings typo is exactly how a background job ends
up POSTing a deployment's whole event stream at the cloud metadata endpoint.
"""
import ipaddress
import socket

import pytest

from stapel_analytics import transport
from stapel_analytics.transport import TransportError, post_json

BODY = b'{"events": []}'
HEADERS = {"Content-Type": "application/json"}


class FakeResponse:
    def __init__(self, status=200, body=b"ok"):
        self.status = status
        self._body = body

    def read(self, size):
        return self._body[:size]


class FakeConnection:
    """Records the request instead of dialling."""

    last = None

    def __init__(self, status=200, body=b"ok", error=None):
        self.status = status
        self.body = body
        self.error = error
        self.requests = []
        self.closed = False
        FakeConnection.last = self

    def request(self, method, path, body=None, headers=None):
        if self.error is not None:
            raise self.error
        self.requests.append(
            {"method": method, "path": path, "body": body, "headers": headers}
        )

    def getresponse(self):
        return FakeResponse(self.status, self.body)

    def close(self):
        self.closed = True


@pytest.fixture
def public_dns(monkeypatch):
    """Resolve every host to a routable public address."""

    def fake_getaddrinfo(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture
def connection(monkeypatch):
    """Substitute the network at the ``_connect`` seam."""

    def install(**kwargs):
        def fake_connect(host, ip, port, *, timeout):
            return FakeConnection(**kwargs)

        monkeypatch.setattr(transport, "_connect", fake_connect)
        return fake_connect

    return install


class TestUrlPolicy:
    def test_http_is_refused(self):
        with pytest.raises(TransportError) as exc:
            post_json("http://collector.example/e", BODY, HEADERS)
        assert "non-https" in str(exc.value)

    def test_a_url_without_a_host_is_refused(self):
        with pytest.raises(TransportError):
            post_json("https:///e", BODY, HEADERS)

    def test_an_unresolvable_host_is_refused(self, monkeypatch):
        def boom(host, port, **kwargs):
            raise socket.gaierror("nope")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        with pytest.raises(TransportError) as exc:
            post_json("https://collector.example/e", BODY, HEADERS)
        assert "cannot resolve" in str(exc.value)

    def test_a_host_with_no_addresses_is_refused(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
        with pytest.raises(TransportError) as exc:
            post_json("https://collector.example/e", BODY, HEADERS)
        assert "no addresses" in str(exc.value)


class TestIpGuard:
    @pytest.mark.parametrize(
        "address",
        ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"],
    )
    def test_a_non_public_address_is_refused(self, monkeypatch, address):
        family = (
            socket.AF_INET6 if ":" in address else socket.AF_INET
        )

        def fake_getaddrinfo(host, port, **kwargs):
            return [(family, socket.SOCK_STREAM, 6, "", (address, port))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(TransportError) as exc:
            post_json("https://collector.example/e", BODY, HEADERS)
        assert "non-public" in str(exc.value)

    def test_a_mixed_answer_is_refused_whole(self, monkeypatch):
        """A name answering with public AND private records is hostile,
        not 'pick the good one'."""

        def fake_getaddrinfo(host, port, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
            ]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(TransportError):
            post_json("https://collector.example/e", BODY, HEADERS)

    def test_the_guard_is_cores_definition(self):
        """Imported, never restated: the IP policy must not drift."""
        from stapel_core.net.safe_fetch import ip_is_forbidden

        assert transport.ip_is_forbidden is ip_is_forbidden
        assert ip_is_forbidden(ipaddress.ip_address("169.254.169.254"))


class TestRequest:
    def test_a_2xx_returns_its_status(self, public_dns, connection):
        connection(status=204)
        assert post_json("https://collector.example/e", BODY, HEADERS) == 204

    def test_the_body_and_headers_are_sent(self, public_dns, connection):
        connection()
        post_json("https://collector.example/e", BODY, HEADERS)
        request = FakeConnection.last.requests[0]
        assert request["method"] == "POST"
        assert request["body"] == BODY
        assert request["headers"]["Content-Type"] == "application/json"

    def test_the_host_header_is_the_real_hostname(self, public_dns, connection):
        """TCP goes to the validated IP; SNI, the certificate and Host stay
        the real name, so rebinding buys nothing."""
        connection()
        post_json("https://collector.example/e", BODY, HEADERS)
        assert FakeConnection.last.requests[0]["headers"]["Host"] == (
            "collector.example"
        )

    def test_the_query_string_survives(self, public_dns, connection):
        connection()
        post_json("https://collector.example/e?k=v", BODY, HEADERS)
        assert FakeConnection.last.requests[0]["path"] == "/e?k=v"

    def test_a_pathless_url_posts_to_root(self, public_dns, connection):
        connection()
        post_json("https://collector.example", BODY, HEADERS)
        assert FakeConnection.last.requests[0]["path"] == "/"

    def test_the_connection_is_always_closed(self, public_dns, connection):
        connection()
        post_json("https://collector.example/e", BODY, HEADERS)
        assert FakeConnection.last.closed is True


class TestFailures:
    def test_a_4xx_is_an_error_with_the_body(self, public_dns, connection):
        connection(status=422, body=b"bad payload")
        with pytest.raises(TransportError) as exc:
            post_json("https://collector.example/e", BODY, HEADERS)
        assert "422" in str(exc.value) and "bad payload" in str(exc.value)

    def test_a_5xx_is_an_error(self, public_dns, connection):
        connection(status=503)
        with pytest.raises(TransportError):
            post_json("https://collector.example/e", BODY, HEADERS)

    def test_a_redirect_is_not_an_address(self, public_dns, connection):
        connection(status=302)
        with pytest.raises(TransportError) as exc:
            post_json("https://collector.example/e", BODY, HEADERS)
        assert "redirect" in str(exc.value)

    def test_a_socket_error_becomes_a_transport_error(self, public_dns, connection):
        connection(error=OSError("connection reset"))
        with pytest.raises(TransportError) as exc:
            post_json("https://collector.example/e", BODY, HEADERS)
        assert "OSError" in str(exc.value)

    def test_the_connection_is_closed_after_a_failure(self, public_dns, connection):
        connection(status=500)
        with pytest.raises(TransportError):
            post_json("https://collector.example/e", BODY, HEADERS)
        assert FakeConnection.last.closed is True

    def test_a_response_body_is_capped(self, public_dns, connection):
        connection(status=500, body=b"x" * 100_000)
        with pytest.raises(TransportError) as exc:
            post_json("https://collector.example/e", BODY, HEADERS)
        assert len(str(exc.value)) < 500


class TestConnect:
    def test_a_failed_tls_handshake_closes_the_raw_socket(self, monkeypatch):
        closed = {"value": False}

        class RawSocket:
            def close(self):
                closed["value"] = True

        monkeypatch.setattr(
            socket, "create_connection", lambda *a, **k: RawSocket()
        )

        class BrokenContext:
            def wrap_socket(self, sock, server_hostname=None):
                raise OSError("handshake failed")

        monkeypatch.setattr(
            transport.ssl, "create_default_context", lambda: BrokenContext()
        )
        with pytest.raises(OSError):
            transport._connect("collector.example", "93.184.216.34", 443, timeout=1)
        assert closed["value"] is True

    def test_the_socket_is_pinned_and_sni_is_the_hostname(self, monkeypatch):
        seen = {}

        class RawSocket:
            def close(self):
                pass

        class WrappedSocket:
            pass

        def fake_create_connection(address, timeout=None):
            seen["address"] = address
            return RawSocket()

        class Context:
            def wrap_socket(self, sock, server_hostname=None):
                seen["sni"] = server_hostname
                return WrappedSocket()

        monkeypatch.setattr(socket, "create_connection", fake_create_connection)
        monkeypatch.setattr(transport.ssl, "create_default_context", lambda: Context())
        connection = transport._connect(
            "collector.example", "93.184.216.34", 443, timeout=1
        )
        assert seen["address"] == ("93.184.216.34", 443)
        assert seen["sni"] == "collector.example"
        assert connection.sock is not None
