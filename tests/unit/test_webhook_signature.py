from __future__ import annotations

import time

from svu.vc.webhook import MAX_CLOCK_SKEW_SECONDS, verify
from tests.fake_vc import FakeVC


def test_a_signed_body_verifies() -> None:
    fake = FakeVC()
    body = b'{"type":1}'
    headers = fake.sign(body)
    assert verify(fake.public_key_hex, headers["X-Signature-Ed25519"], headers["X-Signature-Timestamp"], body)


def test_a_tampered_body_or_timestamp_does_not_verify() -> None:
    fake = FakeVC()
    body = b'{"type":2,"data":[]}'
    headers = fake.sign(body)
    assert not verify(fake.public_key_hex, headers["X-Signature-Ed25519"], headers["X-Signature-Timestamp"], b'{"type":2,"data":[{}]}')
    assert not verify(fake.public_key_hex, headers["X-Signature-Ed25519"], str(int(headers["X-Signature-Timestamp"]) + 1), body)


def test_another_key_does_not_verify() -> None:
    body = b'{"type":1}'
    headers = FakeVC().sign(body)
    assert not verify(FakeVC().public_key_hex, headers["X-Signature-Ed25519"], headers["X-Signature-Timestamp"], body)


def test_old_deliveries_are_refused() -> None:
    fake = FakeVC()
    body = b'{"type":1}'
    old = int(time.time()) - MAX_CLOCK_SKEW_SECONDS - 5
    headers = fake.sign(body, timestamp=old)
    assert not verify(fake.public_key_hex, headers["X-Signature-Ed25519"], headers["X-Signature-Timestamp"], body)


def test_malformed_headers_do_not_verify() -> None:
    fake = FakeVC()
    for signature, timestamp in ((None, "1"), ("zz", str(int(time.time()))), ("00", "not-a-number"), ("00", None)):
        assert not verify(fake.public_key_hex, signature, timestamp, b"{}")
    assert not verify("not-hex", "00", str(int(time.time())), b"{}")
