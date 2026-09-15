"""scripts/github_app_token.py - the one App-token minter (ADR 0063)."""
import io
import json
import urllib.error
from unittest import mock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from scripts import github_app_token as gat


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return pem, pub


def _response(payload: dict):
    m = mock.MagicMock()
    m.read.return_value = json.dumps(payload).encode()
    m.__enter__.return_value = m
    return m


def _http_error(status: int, message: str):
    return urllib.error.HTTPError(
        url="https://api.github.com/x", code=status, msg="err", hdrs=None,
        fp=io.BytesIO(json.dumps({"message": message}).encode()),
    )


def test_app_jwt_claims_and_algorithm(keypair):
    pem, pub = keypair
    token = gat.app_jwt("4945586", pem, now=1_000_000)
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(token, pub, algorithms=["RS256"], issuer="4945586",
                        options={"verify_exp": False})
    # iat backdated for clock skew, exp inside GitHub's 10-minute cap
    assert claims["iat"] == 1_000_000 - gat.CLOCK_SKEW_SECONDS
    assert claims["exp"] == 1_000_000 + gat.JWT_TTL_SECONDS
    assert claims["exp"] - claims["iat"] <= 600
    # a trailing-newline-stripped PEM (vault_key().strip()) still signs
    assert gat.app_jwt("4945586", pem.strip(), now=1_000_000)


def test_installation_token_request_shape():
    urlopen = mock.Mock(return_value=_response({"token": "ghs_x", "expires_at": "2026-09-15T13:00:00Z"}))
    out = gat.installation_token("JWT", 12345, repositories=["veggies"],
                                 permissions={"contents": "read"}, urlopen=urlopen)
    req = urlopen.call_args[0][0]
    assert req.full_url == "https://api.github.com/app/installations/12345/access_tokens"
    assert req.method == "POST"
    assert req.headers["Authorization"] == "Bearer JWT"
    assert req.headers["X-github-api-version"] == gat.API_VERSION
    assert json.loads(req.data) == {"repositories": ["veggies"], "permissions": {"contents": "read"}}
    assert out == {"token": "ghs_x", "expires_at": "2026-09-15T13:00:00Z"}


def test_installation_token_unscoped_sends_empty_body():
    urlopen = mock.Mock(return_value=_response({"token": "t", "expires_at": "2026-01-01T00:00:00Z"}))
    gat.installation_token("JWT", "1", urlopen=urlopen)
    assert json.loads(urlopen.call_args[0][0].data) == {}


def test_http_error_surfaces_status_and_message_without_credential():
    urlopen = mock.Mock(side_effect=_http_error(422, "The permissions requested are not granted to this installation."))
    with pytest.raises(gat.GitHubAppError) as exc:
        gat.installation_token("SECRET-JWT", 1, permissions={"administration": "write"}, urlopen=urlopen)
    assert exc.value.status == 422
    assert "not granted" in exc.value.message
    assert "SECRET-JWT" not in str(exc.value)
    assert "/app/installations/1/access_tokens" in str(exc.value)


def test_request_timeout_outlives_the_pod_squid_first_connect_stall():
    # measured 35-40s on 2026-09-15; anything under that fails every first call
    assert gat.REQUEST_TIMEOUT_S >= 60
    urlopen = mock.Mock(return_value=_response({"slug": "x"}))
    gat.app_get("JWT", "/app", urlopen=urlopen)
    assert urlopen.call_args.kwargs["timeout"] == gat.REQUEST_TIMEOUT_S


def test_app_get_uses_jwt_bearer():
    urlopen = mock.Mock(return_value=_response({"slug": "veggies-harness"}))
    assert gat.app_get("JWT", "/app", urlopen=urlopen)["slug"] == "veggies-harness"
    req = urlopen.call_args[0][0]
    assert req.method == "GET" and req.data is None
    assert req.full_url == "https://api.github.com/app"


def test_mint_composes_jwt_and_exchange(keypair):
    pem, pub = keypair
    urlopen = mock.Mock(return_value=_response({"token": "t", "expires_at": "2026-01-01T00:00:00Z"}))
    gat.mint("4945586", 7, pem, repositories=["r"], urlopen=urlopen)
    bearer = urlopen.call_args[0][0].headers["Authorization"].split(" ", 1)[1]
    assert jwt.decode(bearer, pub, algorithms=["RS256"], issuer="4945586")["iss"] == "4945586"


def test_expires_at_epoch_parses_github_zulu():
    assert gat.expires_at_epoch("1970-01-01T01:00:00Z") == 3600.0
