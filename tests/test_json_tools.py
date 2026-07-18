from __future__ import annotations

import pytest

from make_it_so.json_tools import decode_first_json


def test_decode_first_json_skips_invalid_prefix_candidates() -> None:
    assert decode_first_json("notice {not-json} then [1, 2]") == [1, 2]
    assert decode_first_json("prefix {\"ok\": true} trailing") == {"ok": True}


def test_decode_first_json_rejects_output_without_an_object_or_array() -> None:
    with pytest.raises(ValueError, match="valid JSON"):
        decode_first_json("plain text")
