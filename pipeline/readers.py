"""Read source files exactly as received: every CSV value stays text,
and a structurally broken JSON file is salvaged instead of crashing."""
import json
import re
from pathlib import Path

import pandas as pd


def read_json_salvaging(text: str):
    """Parse a JSON array of objects. If the document is broken (e.g. a
    missing comma), recover every object that still parses on its own.

    Returns (records, error_message, lost_fragments). lost_fragments holds
    any text that was not part of a recovered object, so nothing is
    silently dropped.
    """
    try:
        return json.loads(text), "", []
    except json.JSONDecodeError as exc:
        error = f"{exc.msg} at line {exc.lineno}, column {exc.colno}"
    decoder, records, lost, pos = json.JSONDecoder(), [], [], 0
    while (start := text.find("{", pos)) != -1:
        gap = text[pos:start]
        if re.sub(r"[\s,\[\]]", "", gap):          # junk between objects
            lost.append(gap.strip())
        try:
            obj, end = decoder.raw_decode(text, start)
            records.append(obj)
            pos = end
        except json.JSONDecodeError:
            next_start = text.find("{", start + 1)
            lost.append(text[start: next_start if next_start != -1 else len(text)].strip())
            pos = next_start if next_start != -1 else len(text)
    return records, error, lost


def read_source(path: Path):
    """Return (rows: list[dict], structural_error: str, lost_fragments: list[str])."""
    if path.suffix == ".json":
        return read_json_salvaging(path.read_text(encoding="utf-8"))
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return df.to_dict("records"), "", []
