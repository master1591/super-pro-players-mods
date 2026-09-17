"""Strict SUPER PRO PLAYERS victory-music event protocol.

The transport is BombSquad's reliable chat history.  Events deliberately map
only to six bundled cue identifiers; no path, URL, or command is accepted from
the server.
"""

from __future__ import print_function

import re


PROTOCOL_VERSION = "SPP1"
EVENT_SENDER = "SPP MUSIC"
SERIES_TARGET = 3
MAX_FRAME_CHARS = 132
HELLO_PREFIX = "/sppmod hello SPP1 "

_EVENT_RE = re.compile(
    r"^SPP MUSIC: (SUPER|PRO) wins round ([123])/3! \| "
    r"SPP1\|(SUPER|PRO)\|([123])\|3\|"
    r"([0-9a-f]{12})\|([0-9a-f]{16})\|([0-9a-f]{12})$",
    re.ASCII,
)
_HEX_12_RE = re.compile(r"^[0-9a-f]{12}$", re.ASCII)
_HEX_16_RE = re.compile(r"^[0-9a-f]{16}$", re.ASCII)
_DISCOVERY_RE = re.compile(
    r"^SPP MUSIC: SPP1\|DISCOVER\|([0-9a-f]{12})$", re.ASCII
)
_READY_RE = re.compile(
    r"^SPP MUSIC: SPP1\|READY\|([0-9a-f]{12})\|([0-9a-f]{16})$",
    re.ASCII,
)
_STOP_RE = re.compile(
    r"^SPP MUSIC: SPP1\|STOP\|([0-9a-f]{12})\|([0-9a-f]{16})\|([0-9a-f]{12})$",
    re.ASCII,
)


def parse_control_message(message):
    """Parse fixed discovery, registration and cue-stop frames.

    These fixed frames identify a cooperating host, not a cryptographically
    trusted identity. The runtime additionally requires a live connection,
    fresh chat history and a matching, randomly generated client nonce.
    """
    if not isinstance(message, str) or len(message) > MAX_FRAME_CHARS:
        return None
    match = _DISCOVERY_RE.fullmatch(message)
    if match is not None:
        return {"kind": "discover", "session_nonce": match.group(1)}
    match = _READY_RE.fullmatch(message)
    if match is not None:
        return {
            "kind": "ready",
            "session_nonce": match.group(1),
            "client_nonce": match.group(2),
        }
    match = _STOP_RE.fullmatch(message)
    if match is not None:
        return {
            "kind": "stop",
            "session_nonce": match.group(1),
            "client_nonce": match.group(2),
            "event_id": match.group(3),
        }
    return None


def build_payload(team, stage, session_nonce, client_nonce, event_id):
    """Build the server-side payload (without the displayed sender prefix)."""
    normalized_team = str(team).upper()
    try:
        normalized_stage = int(stage)
    except (TypeError, ValueError):
        raise ValueError("Victory stage must be an integer.")
    if normalized_team not in ("SUPER", "PRO"):
        raise ValueError("Unknown victory team.")
    if normalized_stage not in (1, 2, 3):
        raise ValueError("Victory stage must be 1, 2, or 3.")
    session_nonce = str(session_nonce)
    client_nonce = str(client_nonce)
    event_id = str(event_id)
    if not _HEX_12_RE.fullmatch(session_nonce):
        raise ValueError("Invalid session nonce.")
    if not _HEX_12_RE.fullmatch(event_id):
        raise ValueError("Invalid event id.")
    if not _HEX_16_RE.fullmatch(client_nonce):
        raise ValueError("Invalid client nonce.")
    return (
        "%s wins round %d/3! | %s|%s|%d|3|%s|%s|%s"
        % (
            normalized_team,
            normalized_stage,
            PROTOCOL_VERSION,
            normalized_team,
            normalized_stage,
            session_nonce,
            client_nonce,
            event_id,
        )
    )


def build_hello(client_nonce):
    """Build the filtered client registration command for this connection."""
    client_nonce = str(client_nonce)
    if not _HEX_16_RE.fullmatch(client_nonce):
        raise ValueError("Invalid client nonce.")
    return HELLO_PREFIX + client_nonce


def parse_chat_message(message):
    """Return a validated event dict or ``None`` for ordinary chat."""
    if not isinstance(message, str) or len(message) > MAX_FRAME_CHARS:
        return None
    match = _EVENT_RE.fullmatch(message)
    if match is None:
        return None
    (
        visible_team,
        visible_stage,
        wire_team,
        wire_stage,
        nonce,
        client_nonce,
        event_id,
    ) = match.groups()
    if visible_team != wire_team or visible_stage != wire_stage:
        return None
    return {
        "team": wire_team.lower(),
        "stage": int(wire_stage),
        "target": SERIES_TARGET,
        "session_nonce": nonce,
        "client_nonce": client_nonce,
        "event_id": event_id,
    }
