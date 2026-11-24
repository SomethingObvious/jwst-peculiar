"""Tests for the command line entry point."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from jwst_peculiar import cli

if TYPE_CHECKING:
    import pytest


def test_a_path_the_output_cannot_encode_prints_escaped_not_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr("sys.stdout", out)
    assert cli.main(["--version"]) == 0
    print("list in C:\\日本\\rank.csv")  # noqa: T201 the print being tested
    out.flush()
    said = out.buffer.getvalue()
    assert b"list in C:\\\\u65e5\\u672c\\rank.csv" in said
