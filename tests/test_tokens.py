from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from hermesi import Hermesi, mint_subscriber_token

from .conftest import BASE, KEY

SECRET = "hm_sk_prod_abc123"


def decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_the_payload_names_the_subscriber_the_environment_and_an_expiry() -> None:
    token = mint_subscriber_token(SECRET, "user_1", "env_1", ttl_seconds=600, now=1_700_000_000)

    payload, _ = token.split(".")
    assert json.loads(decode(payload)) == {"sub": "user_1", "env": "env_1", "exp": 1_700_000_600}


def test_the_signature_is_an_hmac_keyed_with_the_sha256_hex_digest_of_the_secret_key() -> None:
    token = mint_subscriber_token(SECRET, "user_1", "env_1", now=1_700_000_000)

    payload, signature = token.split(".")
    key_hash = hashlib.sha256(SECRET.encode()).hexdigest()
    expected = hmac.new(key_hash.encode("ascii"), payload.encode("ascii"), hashlib.sha256).digest()
    assert decode(signature) == expected
    # Signing with the raw key is the mistake that makes Hermesi reject every token.
    wrong = hmac.new(SECRET.encode(), payload.encode("ascii"), hashlib.sha256).digest()
    assert decode(signature) != wrong


def test_the_token_is_url_safe_and_unpadded() -> None:
    token = mint_subscriber_token(SECRET, "user/with+odd chars", "env_1")

    assert all(ch.isalnum() or ch in "-_." for ch in token)
    assert "=" not in token


def test_the_same_inputs_give_the_same_token_and_a_different_secret_does_not() -> None:
    a = mint_subscriber_token(SECRET, "u", "e", now=0)

    assert a == mint_subscriber_token(SECRET, "u", "e", now=0)
    assert a != mint_subscriber_token("hm_sk_prod_other", "u", "e", now=0)
    assert a != mint_subscriber_token(SECRET, "u2", "e", now=0)


def test_defaults_to_an_hour_and_never_more() -> None:
    token = mint_subscriber_token(SECRET, "u", "e", now=1000)
    assert json.loads(decode(token.split(".")[0]))["exp"] == 1000 + 3600

    with pytest.raises(ValueError, match="ttl_seconds"):
        mint_subscriber_token(SECRET, "u", "e", ttl_seconds=3601)
    with pytest.raises(ValueError, match="ttl_seconds"):
        mint_subscriber_token(SECRET, "u", "e", ttl_seconds=0)


def test_refuses_what_cannot_make_a_token() -> None:
    with pytest.raises(ValueError, match="secret key"):
        mint_subscriber_token("hm_pk_prod_public", "u", "e")
    with pytest.raises(ValueError, match="external_id"):
        mint_subscriber_token(SECRET, "", "e")
    with pytest.raises(ValueError, match="environment_id"):
        mint_subscriber_token(SECRET, "u", "")


def test_the_client_mints_with_its_own_key_and_makes_no_request() -> None:
    with Hermesi(SECRET, base_url=BASE) as hermesi:
        token = hermesi.tokens.mint("user_1", environment_id="env_1")

    payload, signature = token.split(".")
    key_hash = hashlib.sha256(SECRET.encode()).hexdigest()
    assert decode(signature) == hmac.new(key_hash.encode("ascii"), payload.encode("ascii"), hashlib.sha256).digest()


def test_the_client_key_in_the_fixture_is_a_secret_key() -> None:
    assert KEY.startswith("hm_sk_")
