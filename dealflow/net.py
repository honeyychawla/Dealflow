"""The TLS trust store used for every outbound HTTPS request (Instagram, OpenRouter).

Python installed from python.org on macOS does not read the system keychain
and ships with an empty certificate store until its "Install Certificates"
script is run. On such an install every HTTPS request fails with
CERTIFICATE_VERIFY_FAILED. Rather than ask for that step (or, worse, switch
verification off), this adds the operating system's own CA bundle when the
default store turns out to be empty. Verification always stays on.
"""

from __future__ import annotations

import os
import ssl
import threading

# Root CA bundles shipped by the operating system, most common first.
SYSTEM_BUNDLES = (
    "/etc/ssl/cert.pem",  # macOS, Alpine, FreeBSD
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",  # openSUSE
)

_lock = threading.Lock()
_context: ssl.SSLContext | None = None
_source = ""


def _build() -> tuple[ssl.SSLContext, str]:
    context = ssl.create_default_context()  # CERT_REQUIRED + hostname checking
    if context.cert_store_stats().get("x509_ca", 0) > 0:
        return context, "python default"
    paths = ssl.get_default_verify_paths()
    if paths.capath and os.path.isdir(paths.capath) and os.listdir(paths.capath):
        return context, "python default"  # a hashed directory loads lazily, so 0 is expected
    for bundle in SYSTEM_BUNDLES:
        if os.path.isfile(bundle):
            try:
                context.load_verify_locations(cafile=bundle)
            except (ssl.SSLError, OSError):
                continue
            if context.cert_store_stats().get("x509_ca", 0) > 0:
                return context, bundle
    try:
        import certifi  # optional; used only if it happens to be installed

        context.load_verify_locations(cafile=certifi.where())
        return context, "certifi"
    except Exception:  # noqa: BLE001 - not installed, or unreadable
        pass
    return context, "none"


def ssl_context() -> ssl.SSLContext:
    global _context, _source
    with _lock:
        if _context is None:
            _context, _source = _build()
        return _context


def trust_source() -> str:
    """Where the root certificates came from; "none" means HTTPS cannot work."""
    ssl_context()
    return _source


def explain_tls_failure(error: object) -> str | None:
    """A fix-it message for certificate errors, or None if `error` is something else."""
    text = str(error)
    if "CERTIFICATE_VERIFY_FAILED" not in text:
        return None
    if trust_source() == "none":
        return (
            "This Python has no root certificates, so it cannot verify any HTTPS site. "
            "On macOS run “/Applications/Python 3.x/Install Certificates.command”, "
            "or set SSL_CERT_FILE to a CA bundle, then restart the app."
        )
    return (
        "The server's TLS certificate could not be verified. If you are behind a VPN or a "
        f"corporate proxy that inspects traffic, set SSL_CERT_FILE to its CA bundle. [{text[:160]}]"
    )
