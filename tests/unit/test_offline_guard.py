import socket

import pytest


def test_network_guard_blocks_external_hosts():
    with pytest.raises(AssertionError, match="network connection"):
        socket.create_connection(("api.tomtom.com", 443), timeout=0.01)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw, pytest.raises(AssertionError):
        raw.connect(("8.8.8.8", 443))
