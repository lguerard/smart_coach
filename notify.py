#!/usr/bin/env python3
"""Push a coaching message to a phone via ntfy.sh.

Near-verbatim port of garmin-coach's coach.notify().
"""

import json
import os
import urllib.request


def _build_request(
    text: str, topic: str, title: str,
) -> urllib.request.Request:
    """Construct the ntfy publish request (split out for testing).

    Parameters:
        text (str): Message body.
        topic (str): ntfy topic.
        title (str): Notification title.

    Returns:
        urllib.request.Request: Ready to send with ``urlopen``.
    """
    return urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=text.encode(),
        headers={"Title": title, "Tags": "muscle"},
    )


def notify(text: str, title: str = "Smart Coach", topic: str | None = None) -> None:
    """Send a push notification via ntfy.

    Parameters:
        text (str): Message body.
        title (str): Notification title.
        topic (str | None): ntfy topic -- pass a user's own
            ``ntfy_topic`` setting so each family member's messages go
            to their own phone; falls back to the deployment-wide
            ``NTFY_TOPIC`` env var if not given.
    """
    topic = topic or os.environ["NTFY_TOPIC"]
    request = _build_request(text, topic, title)
    urllib.request.urlopen(request, timeout=30)


def _build_action_request(
    text: str, topic: str, title: str, actions: list[dict],
    click: str | None = None,
) -> urllib.request.Request:
    """ntfy's JSON publish, which carries buttons (at most 3).

    Parameters:
        actions (list[dict]): ntfy actions, e.g. ``{"action": "http",
            "label": "Trop facile", "url": ..., "method": "POST",
            "clear": True}``.
        click (str | None): URL opened when the notification is tapped.
    """
    payload = {"topic": topic, "message": text, "title": title,
               "tags": ["muscle"], "actions": actions[:3]}
    if click:
        payload["click"] = click
    return urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )


def notify_with_actions(
    text: str, actions: list[dict], title: str = "Smart Coach",
    topic: str | None = None, click: str | None = None,
) -> None:
    """Send a notification with buttons via ntfy (see ``notify``)."""
    topic = topic or os.environ["NTFY_TOPIC"]
    request = _build_action_request(text, topic, title, actions, click)
    urllib.request.urlopen(request, timeout=30)


if __name__ == "__main__":
    request = _build_request("hello", "my-topic", "Test Title")
    assert request.full_url == "https://ntfy.sh/my-topic"
    assert request.data == b"hello"
    assert request.get_header("Title") == "Test Title"
    assert request.get_header("Tags") == "muscle"
    buttons = _build_action_request(
        "Comment c'etait ?", "t", "Smart Coach",
        [{"action": "http", "label": str(n), "url": f"https://x/{n}",
          "method": "POST"} for n in range(4)], click="https://x/sessions",
    )
    body = json.loads(buttons.data)
    assert buttons.full_url == "https://ntfy.sh/" and body["topic"] == "t"
    assert len(body["actions"]) == 3 and body["click"] == "https://x/sessions"
    print("notify.py: all checks passed (no live push sent)")
