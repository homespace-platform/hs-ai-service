"""Unit tests are offline: any non-loopback connection (OpenAI, TomTom, Core, Mongo) fails the test."""
import socket

import pytest

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _host(address):
    return address[0] if isinstance(address, tuple) else address


@pytest.fixture(autouse=True)
def network_guard(monkeypatch):
    connect, create_connection = socket.socket.connect, socket.create_connection

    def guarded_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and _host(address) not in LOOPBACK:
            raise AssertionError(f"Unit test attempted a network connection to {_host(address)}")
        return connect(self, address)

    def guarded_create_connection(address, *args, **kwargs):
        if _host(address) not in LOOPBACK:
            raise AssertionError(f"Unit test attempted a network connection to {_host(address)}")
        return create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    yield
