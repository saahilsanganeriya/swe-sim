"""Bound Temporal JSON payloads; artifacts and large plans belong in the workspace."""
from __future__ import annotations

import json

MAX_PAYLOAD_BYTES = 256 * 1024


def check_payload(value, label: str = 'payload') -> None:
    # Match the default Temporal JSON encoding (including escaped Unicode), conservatively allowing metadata.
    size = len(json.dumps(value, separators=(',', ':'), sort_keys=True, allow_nan=False).encode('utf-8'))
    if size >= MAX_PAYLOAD_BYTES - 1024:
        raise ValueError(f'{label} is {size} bytes; keep Temporal payloads below 256 KB and pass artifact paths')
