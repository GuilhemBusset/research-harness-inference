"""Stand-in for tools/hygiene/scan.py in the backlog tests.

Same interface: `--paths P...`; exit 0 clean, 1 findings (`path:line: rule: excerpt` on stdout),
2 usage error. It knows one rule: a line that holds the word in $FAKE_SCAN_WORD is a finding.
With $FAKE_SCAN_LOG set, every file it reads is recorded there, so a test can see exactly what
was scanned. With $FAKE_SCAN_EXIT set, it exits with that status without scanning, which is how
a test stands in for a scanner that cannot run.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if os.environ.get("FAKE_SCAN_EXIT"):
        print("hygiene: error: forced by the test", file=sys.stderr)
        return int(os.environ["FAKE_SCAN_EXIT"])
    if len(argv) < 2 or argv[0] != "--paths":
        print("usage: scan.py --paths P...", file=sys.stderr)
        return 2
    word = os.environ.get("FAKE_SCAN_WORD", "")
    log = os.environ.get("FAKE_SCAN_LOG")
    findings = 0
    for given in argv[1:]:
        path = Path(given)
        if not path.is_file():
            print(f"hygiene: error: no such file or directory: {given}", file=sys.stderr)
            return 2
        text = path.read_text(encoding="utf-8")
        if log:
            with Path(log).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"path": given, "text": text}) + "\n")
        for number, line in enumerate(text.splitlines(), 1):
            if word and word in line:
                print(f"{given}:{number}: fake-rule: [redacted]")
                findings += 1
    print(f"hygiene: {len(argv) - 1} files scanned, {findings} findings", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
