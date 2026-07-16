#!/usr/bin/env python3
"""Push a coaching message to a phone via ntfy.sh.

Near-verbatim port of garmin-coach's coach.notify().
"""

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


def notify(text: str, title: str = "Smart Sport", topic: str | None = None) -> None:
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


if __name__ == "__main__":
    request = _build_request("hello", "my-topic", "Test Title")
    assert request.full_url == "https://ntfy.sh/my-topic"
    assert request.data == b"hello"
    assert request.get_header("Title") == "Test Title"
    assert request.get_header("Tags") == "muscle"
    print("notify.py: all checks passed (no live push sent)")
