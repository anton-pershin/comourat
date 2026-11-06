"""Shared controlled fixtures for the comourat test suite."""

from __future__ import annotations

import datetime
import ssl
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from comourat.enrichment.page_fetch import ArticleFetchError, PageFetcher
from comourat.enrichment.web_tools import attach_web_tools


def _make_tls_server_context(tmp_path, hostname):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / f"{hostname}.crt"
    key_path = tmp_path / f"{hostname}.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    return context, str(certificate_path)


@pytest.fixture
def tls_server_context():
    return _make_tls_server_context


class _ToolAgent:
    def __init__(self):
        self.tools = {}

    def tool(self, function):
        self.tools[function.__name__] = function
        return function


def _tool_context():
    return SimpleNamespace(
        usage=SimpleNamespace(tool_calls=0),
        usage_limits=SimpleNamespace(tool_calls_limit=None),
    )


@pytest.fixture
def entry_point_factory(monkeypatch):
    """Build one real public article/enrichment entry point for matrix tests."""

    def create(kind, **client_kwargs):
        if kind == "page":
            fetch = PageFetcher(**client_kwargs).fetch

            def fetch_article(url):
                try:
                    return fetch(url)
                except ArticleFetchError as exc:
                    return exc

            return fetch_article
        agent = _ToolAgent()
        attach_web_tools(agent, **client_kwargs)
        if kind == "extract":
            return lambda url: agent.tools["extract_url"](_tool_context(), url)

        def search(url):
            monkeypatch.setattr("comourat.enrichment.web_tools.DEFAULT_SEARCH_URL", url)
            return agent.tools["web_search"](_tool_context(), "controlled query")

        return search

    return create
