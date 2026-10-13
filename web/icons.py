#!/usr/bin/env python3
"""Inline SVG icons and session-type badges for the templates.

The shapes follow the Lucide icon set (ISC licence, see
library/NOTICE.md); the kettlebell is drawn here, Lucide has none. They
are inlined rather than loaded from a CDN so they inherit
``currentColor`` and cost no request.
"""

from markupsafe import Markup, escape

ICONS = {
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "trending-up": '<polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/>',
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "dumbbell": '<path d="M14.4 14.4 9.6 9.6"/><path d="M18.66 21.49a2 2 0 1 1-2.83-2.83l-1.77 1.77a2 2 0 1 1-2.83-2.83l6.36-6.36a2 2 0 1 1 2.83 2.83l-1.77 1.77a2 2 0 1 1 2.83 2.83z"/><path d="m21.5 21.5-1.4-1.4"/><path d="M3.9 3.9 2.5 2.5"/><path d="M6.4 12.77a2 2 0 1 1-2.83-2.83l1.77-1.77a2 2 0 1 1-2.83-2.83l2.83-2.83a2 2 0 1 1 2.83 2.83l1.77-1.77a2 2 0 1 1 2.83 2.83z"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>',
    "trophy": '<path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22"/><path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22"/><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/>',
    "lock": '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "sliders": '<path d="M21 4h-7M10 4H3M21 12h-9M8 12H3M21 20h-5M12 20H3M14 2v4M8 10v4M16 18v4"/>',
    "more": '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
    "footprints": '<path d="M4 16v-2.38C4 11.5 2.97 10.5 3 8c.03-2.72 1.49-6 4.5-6C9.37 2 10 3.8 10 5.5c0 3.11-2 5.66-2 8.68V16a2 2 0 1 1-4 0Z"/><path d="M20 20v-2.38c0-2.12 1.03-3.12 1-5.62-.03-2.72-1.49-6-4.5-6C14.63 6 14 7.8 14 9.5c0 3.11 2 5.66 2 8.68V20a2 2 0 1 0 4 0Z"/><path d="M16 17h4M4 13h4"/>',
    "heart-pulse": '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/><path d="M3.22 12H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"/>',
    "droplet": '<path d="M12 22a7 7 0 0 0 7-7c0-2-1-3.9-3-5.5s-3.5-4-4-6.5c-.5 2.5-2 4.9-4 6.5C6 11.1 5 13 5 15a7 7 0 0 0 7 7z"/>',
    "battery": '<rect width="16" height="10" x="2" y="7" rx="2"/><path d="M22 11v2M6 11v2M10 11v2"/>',
    "gauge": '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    "zap": '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
    "flame": '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    "timer": '<path d="M10 2h4M12 14l3-3"/><circle cx="12" cy="14" r="8"/>',
    "target": '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    "thermometer": '<path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/>',
    "log-out": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><path d="M21 12H9"/>',
    "message": '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
    "mountain": '<path d="m8 3 4 8 5-5 5 15H2L8 3z"/>',
    "map-pin": '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
    "laptop": '<path d="M20 16V7a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v9m16 0H4m16 0 1.28 2.55a1 1 0 0 1-.9 1.45H3.62a1 1 0 0 1-.9-1.45L4 16"/>',
    "walk": '<circle cx="13" cy="4" r="2"/><path d="m9 20 2.5-5.5L14 17v4M8 11l3-3 3 1 2 3h3M11 8l.5 6.5"/>',
    "bed": '<path d="M2 4v16M2 8h18a2 2 0 0 1 2 2v10M2 17h20M6 8v9"/>',
    "shield": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
    "kettlebell": '<path d="M8.5 10.5V7a3.5 3.5 0 0 1 7 0v3.5"/><circle cx="12" cy="15" r="6"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "calendar": '<rect width="18" height="18" x="3" y="4" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
}

# Session type -> (icon, colour token). Effort-type colours, so the
# badge on the dashboard matches the one on the Sessions page.
SESSION_STYLE = {
    "treadmill": ("footprints", "orange"),
    "lower_body": ("dumbbell", "violet"),
    "upper_body": ("dumbbell", "blue"),
    "calisthenics": ("activity", "green"),
    "kettlebell": ("kettlebell", "magenta"),
    "recovery": ("walk", "aqua"),
    "rest": ("bed", "muted"),
}


def icon(name: str, size: int = 18, cls: str = "") -> Markup:
    """One icon as inline SVG.

    Parameters:
        name (str): Key of ``ICONS``.
        size (int): Width and height in px.
        cls (str): Extra CSS classes.

    Returns:
        Markup: Safe ``<svg>`` markup; stroke follows ``currentColor``.
    """
    return Markup(
        f'<svg class="ic {escape(cls)}" width="{int(size)}" height="{int(size)}" '
        'viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true">{ICONS[name]}</svg>'
    )


def session_badge(kind: str | None, label: str) -> Markup:
    """Pastel pill for a session type, e.g. ``kettlebell``.

    Parameters:
        kind (str | None): Session type, ``recovery`` or ``rest``;
            anything unknown gets the accent colour and a target icon.
        label (str): Text shown, escaped.

    Returns:
        Markup: Safe ``<span class="badge">`` markup.
    """
    name, colour = SESSION_STYLE.get(kind or "", ("target", "accent"))
    return Markup(
        f'<span class="badge" style="--badge: var(--{colour});">'
        f'{icon(name, 14)}{escape(label)}</span>'
    )


if __name__ == "__main__":
    import re

    for key in ICONS:
        svg = str(icon(key))
        assert svg.startswith('<svg class="ic ') and svg.endswith("</svg>"), key
        # Only drawing elements: nothing a template could not trust.
        tags = set(re.findall(r"<(\w+)", ICONS[key]))
        assert tags <= {"path", "circle", "rect", "ellipse", "polyline"}, (key, tags)
    for kind, (name, _) in SESSION_STYLE.items():
        assert name in ICONS, kind
    badge = str(session_badge("kettlebell", "Kettlebell <maison>"))
    assert "var(--magenta)" in badge and "&lt;maison&gt;" in badge, badge
    assert "var(--accent)" in str(session_badge(None, "Autre"))
    assert 'width="22"' in str(icon("sun", 22.7))
    try:
        icon("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("unknown icon must fail loudly")
    print("web/icons.py: all checks passed")
