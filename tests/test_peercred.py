import os
import socket

from web_ui.peercred import _decode, tcp_peer_uid, unix_peer_uid


def test_decode():
    addr, port = _decode("0100007F:1F90")
    assert str(addr) == "127.0.0.1" and port == 8080
    addr, port = _decode("00000000000000000000000001000000:0050")
    assert str(addr) == "::1" and port == 80


def test_unix_peer_uid():
    a, b = socket.socketpair()
    with a, b:
        assert unix_peer_uid(a) == os.getuid()


def test_tcp_peer_uid():
    with socket.create_server(("127.0.0.1", 0)) as srv:
        with socket.create_connection(srv.getsockname()):
            conn, _ = srv.accept()
            with conn:
                assert tcp_peer_uid(conn) == os.getuid()
