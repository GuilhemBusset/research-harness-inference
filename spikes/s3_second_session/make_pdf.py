"""The short PDF spike S3's second session asks Claude Code to read (`spikes/s3_second_session.py`).

`python make_pdf.py <file> <word>` writes a one-page PDF whose text gives `word` as its code word,
with the standard library only. The text is in an uncompressed stream, in a standard font, so that
an agent told the endpoint cannot read the PDF can convert it to text itself, as the student guide
says to. Nothing in it identifies anyone: its words are these and the job's random word.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _escaped(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf(lines: list[str]) -> bytes:
    """A one-page PDF of `lines` of ASCII text, one under the other."""
    shown = " ".join(f"({_escaped(line)}) Tj 0 -18 Td" for line in lines)
    stream = f"BT /F1 12 Tf 72 720 Td {shown} ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R /Resources << /Font << /F1 4 0 R >> >> >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    table = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, table)
    return bytes(out)


def notes(word: str) -> list[str]:
    """The text of the PDF: a few lines of notes, and the code word."""
    return [
        "Field notes",
        "This short document stands for a paper that a research skill pack reads.",
        "It holds one fact that matters.",
        f"The code word is {word}.",
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: make_pdf.py <file> <word>", file=sys.stderr)
        return 2
    Path(argv[0]).write_bytes(pdf(notes(argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
