"""ADR-18 unit tests: sealed tokens, OAuth/Notion settings parsing, authorization URL, result URL hosts."""
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet

from notesagent.common.config import get_settings
from notesagent.common.crypto import CredentialCipher, CredentialError, TokenSealer
from notesagent.gateway.oauth import authorization_url
from notesagent.worker.runner import validate_publish_result

KEY = Fernet.generate_key().decode()
TOKEN = "ntn_unit_access_token_0123456789"
REDIRECT = "https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion"


def test_sealed_token_round_trip_and_hides_the_token():
    sealer = TokenSealer(KEY)
    sealed = sealer.seal(TOKEN, "ws-1")
    assert sealed.startswith("nva1.") and TOKEN not in sealed and TokenSealer.is_sealed(sealed)
    assert sealer.unseal(sealed) == TOKEN
    assert sealer.seal(TOKEN) != sealer.seal(TOKEN)                         # randomised


@pytest.mark.parametrize("mutate", [lambda s: s[:-4] + "AAAA", lambda s: "nva1.garbage", lambda s: s.replace("nva1.", "")])
def test_tampered_or_foreign_sealed_tokens_are_rejected(mutate):
    sealer = TokenSealer(KEY)
    with pytest.raises(CredentialError):
        sealer.unseal(mutate(sealer.seal(TOKEN)))


def test_other_key_and_purpose_separation():
    sealed = TokenSealer(KEY).seal(TOKEN)
    with pytest.raises(CredentialError):
        TokenSealer(Fernet.generate_key().decode()).unseal(sealed)
    # the mailbox cipher (same base key) cannot open a sealed token and vice versa
    with pytest.raises(CredentialError):
        CredentialCipher(KEY).open(uuid.uuid4(), sealed[len("nva1."):].encode())
    mailbox_blob = CredentialCipher(KEY).seal(uuid.uuid4(), TOKEN).decode()
    with pytest.raises(CredentialError):
        TokenSealer(KEY).unseal("nva1." + mailbox_blob)


@pytest.mark.parametrize("value,expected", [
    ("1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d", "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"),
    ("1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d", "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"),
    ("https://www.notion.so/Video-Notes-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d", "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"),
    ("https://www.notion.com/workspace/Video-Notes-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d?pvs=4",
     "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"),
    ("", None),
])
def test_default_parent_accepts_ids_and_page_urls(value, expected):
    assert get_settings(notion_default_parent_id=value).notion_default_parent_id == expected


@pytest.mark.parametrize("value", ["https://www.notion.so/Video-Notes", "not-an-id", "1234"])
def test_invalid_parent_values_are_rejected(value):
    with pytest.raises(SystemExit):
        get_settings(notion_default_parent_id=value)


def test_oauth_settings():
    s = get_settings(notion_client_id="cid", notion_client_secret="secret-value", notion_redirect_uri=REDIRECT)
    assert s.notion_oauth_configured and "secret-value" not in repr(s)
    assert not get_settings(notion_client_id="", notion_client_secret="", notion_redirect_uri="").notion_oauth_configured
    for bad in ("http://localhost/cb", "https://x.example/cb#frag", "not a url"):
        with pytest.raises(SystemExit):
            get_settings(notion_redirect_uri=bad)


def test_authorization_url_parameters():
    s = get_settings(notion_client_id="cid", notion_client_secret="x", notion_redirect_uri=REDIRECT)
    url = authorization_url(s, "STATE123")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://api.notion.com/v1/oauth/authorize"
    assert parse_qs(parts.query) == {"client_id": ["cid"], "response_type": ["code"], "owner": ["user"],
                                     "redirect_uri": [REDIRECT], "state": ["STATE123"]}
    assert "x" not in parse_qs(parts.query).get("client_secret", [])


@pytest.mark.parametrize("url", ["https://www.notion.so/Page-abc", "https://www.notion.com/Page-abc",
                                 "https://acme.notion.site/Page-abc"])
def test_publish_result_accepts_notion_domains(url):
    assert validate_publish_result({"notion_page_url": url, "frames_requested": 1, "frames_published": 1})["notion_page_url"] == url
