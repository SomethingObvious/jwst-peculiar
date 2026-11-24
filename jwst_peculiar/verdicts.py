"""The verdicts given in the viewer, kept in verdicts.csv in the run folder."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VERDICTS = ("interesting", "artifact", "known", "not_anomalous")
_FIELDS = ["key", "verdict", "star", "note", "updated"]


def read_verdicts(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Read the verdicts saved in a run folder.

    Returns:
        Each judged object's verdict, star, note and when it was saved, along with
        any columns of your own, or {} without a verdicts.csv.
    """
    path = Path(run_dir) / "verdicts.csv"
    if not path.is_file():
        return {}
    # A spreadsheet saving CSV as UTF-8 puts a byte order mark before the header.
    with path.open(newline="", encoding="utf-8-sig") as f:
        return {
            row["key"]: {
                **{k: v for k, v in row.items() if k not in _FIELDS and k},
                "verdict": row["verdict"] or None,
                "star": row["star"] == "1",
                "note": row["note"],
                "updated": row["updated"],
            }
            for row in csv.DictReader(f)
        }


def save_verdict(  # noqa: PLR0917 the server passes all five positionally
    run_dir: Path,
    key: str,
    verdict: str | None,
    star: bool,  # noqa: FBT001 the server passes all five positionally
    note: str,
) -> str:
    """Write one verdict into verdicts.csv.

    It goes through a temporary file and a rename, so a crash mid-write leaves the
    previous file whole.

    Returns:
        When the verdict was saved, as an ISO 8601 UTC timestamp.

    Raises:
        ValueError: `verdict` isn't one of VERDICTS or None.
    """
    if verdict is not None and verdict not in VERDICTS:
        msg = f"Unknown verdict {verdict!r}, expected one of {VERDICTS} or null"
        raise ValueError(msg)
    run_dir = Path(run_dir)
    rows = read_verdicts(run_dir)
    updated = datetime.now(UTC).isoformat(timespec="seconds")
    mine = {k: v for k, v in rows.get(key, {}).items() if k not in _FIELDS}
    if verdict is None and not star and not note and not any(mine.values()):
        rows.pop(key, None)
    else:
        rows[key] = {
            **mine,
            "verdict": verdict,
            "star": star,
            "note": note,
            "updated": updated,
        }
    extra = dict.fromkeys(k for r in rows.values() for k in r if k not in _FIELDS)
    tmp = run_dir / "verdicts.csv.tmp"
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[*_FIELDS, *extra])
        w.writeheader()
        for k in sorted(rows):
            r = rows[k]
            w.writerow({
                **r,
                "key": k,
                "verdict": r["verdict"] or "",
                "star": int(r["star"]),
            })
    tmp.replace(run_dir / "verdicts.csv")
    return updated
