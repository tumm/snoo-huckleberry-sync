"""SSL certificate helper for Windows environments behind corporate proxies/Zscaler."""

import logging
import os
import ssl
import tempfile

import aiohttp

log = logging.getLogger(__name__)


def setup_grpc_ssl() -> str | None:
    """Export Windows system root and CA certificates to a PEM file.

    Sets the GRPC_DEFAULT_SSL_ROOTS_FILE_PATH environment variable so
    the Google Cloud Firestore client (gRPC) trusts the system root store.
    """
    if os.name != "nt":
        return None

    try:
        temp_dir = tempfile.gettempdir()
        certs_file = os.path.join(temp_dir, "grpc_win_certs.pem")

        with open(certs_file, "wb") as f:
            for store_name in ["ROOT", "CA"]:
                for cert, encoding, _trust in ssl.enum_certificates(store_name):
                    if encoding == "x509_asn":
                        pem = ssl.DER_cert_to_PEM_cert(cert)
                        f.write(pem.encode("ascii"))

        os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = certs_file
        log.info("Configured gRPC to use Windows root certificates at %s", certs_file)
        return certs_file
    except Exception as e:
        log.warning("Failed to configure gRPC Windows certificates: %s", e)
        return None


def get_ssl_context() -> ssl.SSLContext | None:
    """Return an SSLContext configured with Windows system certificates if on Windows, else None."""
    if os.name != "nt":
        return None
    try:
        temp_dir = tempfile.gettempdir()
        certs_file = os.path.join(temp_dir, "grpc_win_certs.pem")
        if os.path.exists(certs_file):
            context = ssl.create_default_context(cafile=certs_file)
            return context
    except Exception as e:
        log.warning("Failed to build SSLContext from Windows certificates: %s", e)
    return None


def make_aiohttp_connector() -> aiohttp.TCPConnector:
    """Build an aiohttp TCPConnector that uses Windows system roots on Windows.

    Falls back to the default (system default SSL context) on other platforms,
    so TLS verification is never disabled - unlike the previous `ssl=False`
    workaround which silently accepted any certificate (MitM risk) on Windows.
    """
    ctx = get_ssl_context()
    return aiohttp.TCPConnector(ssl=ctx) if ctx is not None else aiohttp.TCPConnector()
