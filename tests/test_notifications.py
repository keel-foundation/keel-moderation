from unittest.mock import patch

from app.notifications import notify_channel


def test_notify_channel_posts_to_core(monkeypatch):
    monkeypatch.setenv("KEELTUBE_CORE_URL", "http://core.example")
    monkeypatch.setenv("KEELTUBE_CORE_INTERNAL_TOKEN", "internal-token")

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch("app.notifications.urlopen", return_value=Response()) as urlopen:
        assert notify_channel(
            "ch_demo",
            kind="moderation.action",
            title="Action recorded",
            body="A reviewer recorded an action.",
            href="/studio/moderation/cases/case_1",
            dedupe_key="moderation:action:act_1",
        )
    request = urlopen.call_args.args[0]
    assert request.full_url == "http://core.example/internal/v1/notifications"
    assert request.get_header("Authorization") == "Bearer internal-token"
    assert b"ch_demo" in request.data


def test_notify_channel_is_disabled_without_configuration(monkeypatch):
    monkeypatch.delenv("KEELTUBE_CORE_URL", raising=False)
    assert not notify_channel(
        "ch_demo",
        kind="moderation.action",
        title="Action recorded",
        body="A reviewer recorded an action.",
        href="/studio/moderation/cases/case_1",
        dedupe_key="moderation:action:act_1",
    )
