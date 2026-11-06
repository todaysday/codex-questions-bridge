import pytest

from cqb.security import redact, redact_json


@pytest.mark.parametrize("text, gone", [
    ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
    ("authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("curl -H 'X-Api-Key: 0123456789abcdef'", "0123456789abcdef"),
    ("Cookie: session=abcdef123456", "abcdef123456"),
    ("got Bearer eyJhbGciOiJIUzI1NiJ9.payloadpayload.signaturepart", "payloadpayload"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----", "AAAA"),
    ("-----BEGIN PRIVATE KEY-----\ntruncated without end", "truncated without end"),
    ("key sk-abcdefghijklmnopqrstuvwx", "sk-abcdefghijklmnopqrstuvwx"),
    ("ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
    ("AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    ("bot 123456789:AAEhBP0av28wZQ0vUUhFiMjF0UuAKHyN-eQ", "AAEhBP0av28wZQ0vUUhFiMjF0UuAKHyN-eQ"),
    ("DB_PASSWORD=hunter2", "hunter2"),
    ('{"api_key": "abc123xyz"}', "abc123xyz"),
    ("client_secret: 's3cr3t value'", "s3cr3t value"),
    ("blob " + "A1b2" * 80, "A1b2A1b2"),
])
def test_secrets_are_masked_and_flagged(text, gone):
    out, changed = redact(text)
    assert gone not in out and changed


@pytest.mark.parametrize("text", [
    "plain sentence about tokens and passwords", "max_tokens=4096", "tokens used: 12345", "git commit abc123",
    "/srv/projects/codex-questions-bridge/src/cqb/security.py", "author: Bob", "short hex deadbeef",
])
def test_ordinary_text_is_untouched(text):
    assert redact(text) == (text, False)


def test_control_characters_are_dropped_and_json_walk_covers_nested_strings():
    assert redact("a\x00b\x07c")[0] == "abc"
    cleaned = redact_json({"k": ["x password=p4ss", {"n": 5, "t": ("ok", "token=abc12345")}], "p": None})
    assert cleaned == {"k": ["x password=[REDACTED]", {"n": 5, "t": ["ok", "token=[REDACTED]"]}], "p": None}
