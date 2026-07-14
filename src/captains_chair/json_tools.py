from __future__ import annotations

import json


def decode_first_json(value: str) -> object:
    """Decode the first JSON object or array from output with harmless prefix text."""
    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character not in "[{":
            continue
        try:
            decoded, _ = decoder.raw_decode(value[index:])
            return decoded
        except json.JSONDecodeError:
            continue
    raise ValueError("output did not contain valid JSON")
