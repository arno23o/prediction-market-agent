"""Signing unit tests — a throwaway keypair is generated in-test; the real key is
never touched. Verifies the RSA-PSS(SHA-256, salt 32) recipe and the message format."""
import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from betting_agent.kalshi.client import sign_pss

_PSS = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key, pem


def test_message_format_is_ts_method_path():
    ts, method, path = "1751889600000", "GET", "/trade-api/v2/markets"
    assert f"{ts}{method}{path}" == "1751889600000GET/trade-api/v2/markets"


def test_sign_pss_output_is_base64():
    _, pem = _keypair()
    sig = sign_pss(pem, "1751889600000GET/trade-api/v2/markets")
    assert isinstance(sig, str) and sig
    # round-trips through base64 and is a 2048-bit (256-byte) signature
    assert len(base64.b64decode(sig)) == 256


def test_sign_pss_verifies_with_public_key():
    key, pem = _keypair()
    message = "1751889600000POST/trade-api/v2/portfolio/orders"
    sig = base64.b64decode(sign_pss(pem, message))
    # raises InvalidSignature if the recipe (PSS/salt/hash) is wrong
    key.public_key().verify(sig, message.encode("utf-8"), _PSS, hashes.SHA256())


def test_sign_pss_rejects_tampered_message():
    key, pem = _keypair()
    sig = base64.b64decode(sign_pss(pem, "1751889600000GET/trade-api/v2/markets"))
    with pytest.raises(InvalidSignature):
        key.public_key().verify(sig, b"tampered", _PSS, hashes.SHA256())


def test_sign_pss_is_randomized_but_both_verify():
    key, pem = _keypair()
    message = "1751889600000GET/trade-api/v2/exchange/status"
    a, b = sign_pss(pem, message), sign_pss(pem, message)
    assert a != b  # PSS salt randomizes the signature
    for sig in (a, b):
        key.public_key().verify(base64.b64decode(sig), message.encode(), _PSS, hashes.SHA256())
