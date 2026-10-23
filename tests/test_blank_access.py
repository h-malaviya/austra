import pytest

from austra.access import Access


def test_admin_setup_and_private_credentials(tmp_path):
    access = Access(tmp_path)
    assert not access.ready
    with pytest.raises(ValueError, match="passphrase"):
        access.setup("short")

    session = access.setup("long private passphrase")
    assert access.authenticated(session)
    with pytest.raises(ValueError, match="Incorrect"):
        access.login("wrong private passphrase")

    missing = access.save("anthropic", {"ANTHROPIC_API_KEY": "sample-local-key"})
    assert missing == []
    assert access.get("ANTHROPIC_API_KEY") == "sample-local-key"
    assert "sample-local-key" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "sample-local-key" not in (tmp_path / "admin.json").read_text(encoding="utf-8")
    access.logout(session)
    assert not access.authenticated(session)


def test_credentials_reject_arbitrary_names_and_multiline_values(tmp_path):
    access = Access(tmp_path)
    with pytest.raises(ValueError, match="Unsupported"):
        access.save("gmail", {"UNEXPECTED": "value"})
    with pytest.raises(ValueError, match="single lines"):
        access.save("slack", {"SLACK_SIGNING_SECRET": "one\ntwo"})
    with pytest.raises(ValueError, match="team ID"):
        access.save("slack", {"SLACK_ALLOWED_TEAM_ID": "xapp-example-token"})
    assert not (tmp_path / ".env").exists()
