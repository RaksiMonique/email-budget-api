"""Local harness: run the extraction pipeline on a .eml file and print the result.

    python -m app.extraction.run_fixture path/to/email.eml

No cloud, no DB — this is the Phase 1 dev/debug loop (PLAN.md Phase 1).
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.extraction.models import Field
from app.extraction.pipeline import run


def _fmt_fields(fields: dict[str, Field]) -> str:
    if not fields:
        return "    (none)"
    return "\n".join(
        f"    {key:<17}{f.value!r:<26} [{f.method}]  {f.snippet!r}"
        for key, f in fields.items()
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = Path(argv[1])
    if not path.exists():
        print(f"no such file: {path}")
        return 2

    r = run(path.read_bytes())
    s = r.resolved_sender
    print(f"file            {path}")
    print(f"resolved sender {s.domain}  (source={s.source.value}, conf={s.confidence})")
    print(
        f"classification  is_financial={r.classification.is_financial}  "
        f"type={r.classification.email_type.value}  ({r.classification.method})"
    )
    print("fields:")
    print(_fmt_fields(r.fields))
    print(f"merchant        {r.merchant_normalized}  ->  {r.category_suggestion}")
    print(f"confidence      {r.extraction_confidence}  band={r.confidence_band}")
    print(f"status          {r.status.value}")
    print(f"fingerprint     {r.fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
