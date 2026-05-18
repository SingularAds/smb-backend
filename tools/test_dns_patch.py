"""Test the DNS patch resolves correctly."""
import socket as _socket

_OVERRIDES = {"91-99-169-109.sslip.io": "91.99.169.109"}
_orig = _socket.getaddrinfo

def _patched(host, port, *args, **kwargs):
    host = _OVERRIDES.get(host, host)
    return _orig(host, port, *args, **kwargs)

_socket.getaddrinfo = _patched

# Test resolution
results = _socket.getaddrinfo("91-99-169-109.sslip.io", 443)
ip = results[0][4][0]
print(f"Resolved 91-99-169-109.sslip.io → {ip}")
assert ip == "91.99.169.109", f"Expected 91.99.169.109 got {ip}"
print("DNS patch OK")
