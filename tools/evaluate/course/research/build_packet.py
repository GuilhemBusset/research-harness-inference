"""Write the frozen source packet of the grounded-research task, and the search tool's index of it.

The packet is four short sources, all invented for the course and free to redistribute: a
laboratory report as a three-page PDF (`S1`), and three text sources (`S2` to `S4`). Every word
and every number of them is in `SOURCES` below. From it this program writes, under the folder of
the research task:

- `packet/S1.pdf` and `packet/S2.md` to `packet/S4.md`, what a trial reads;
- `tools/search/index.json`, the passages the local search tool ranks, and the grader's map of
  the packet: each passage with its source, its page (in the PDF) and its table.

The output depends on nothing but `SOURCES`: no date, no random identifier, no font file. Run
again, it writes the same bytes, which `tests/test_research.py` holds the committed files to. To
change the packet, change `SOURCES` and run it:

    uv run --locked python tools/evaluate/course/research/build_packet.py
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKET = HERE / "packet"
INDEX = HERE / "tools" / "search" / "index.json"


@dataclass(frozen=True)
class Table:
    number: str
    caption: str
    header: tuple[str, str]
    rows: tuple[tuple[str, str], ...]


type Block = str | Table


@dataclass(frozen=True)
class Source:
    source_id: str
    title: str
    file: str
    # A PDF has pages; a text source is one page that has no number.
    pages: tuple[tuple[Block, ...], ...]

    @property
    def paged(self) -> bool:
        return self.file.endswith(".pdf")


NOTICE = "Written for a course exercise. The panels, the laboratory and every value are invented."
CONDUCTIVITY = ("Panel", "λ, W/(m·K)")
DENSITY = ("Panel", "ρ, kg/m³")

SOURCES = (
    Source(
        "S1",
        "Laboratory report S1: thermal conductivity of four bio-based insulation panels",
        "S1.pdf",
        (
            (
                NOTICE,
                (
                    "Four insulation panels were tested: a wood fibre panel, an expanded cork panel, a hemp "
                    "fibre panel and a compressed straw panel, all 60 mm thick. Their thermal conductivity "
                    "was measured with a guarded hot plate on two specimens of each panel, and the mean of "
                    "the two is reported."
                ),
                (
                    "The specimens were first measured dry, after drying at 70 °C to constant mass, at a "
                    "mean temperature of 10 °C. They were measured again at the same mean temperature "
                    "after conditioning at 23 °C and 80 % relative humidity, and finally dry at a mean "
                    "temperature of 30 °C. The densities of the same specimens are given in note S2."
                ),
            ),
            (
                (
                    "Results at a mean temperature of 10 °C. Tables 1 and 2 give the conductivity of each "
                    "panel dry and after conditioning at 80 % relative humidity."
                ),
                Table(
                    "1",
                    "Table 1. Thermal conductivity, dry, mean temperature 10 °C.",
                    CONDUCTIVITY,
                    (("wood fibre", "0.038"), ("cork", "0.040"), ("hemp fibre", "0.042"), ("straw", "0.052")),
                ),
                Table(
                    "2",
                    "Table 2. Thermal conductivity, conditioned at 23 °C and 80 % relative humidity, mean 10 °C.",
                    CONDUCTIVITY,
                    (("wood fibre", "0.044"), ("cork", "0.043"), ("hemp fibre", "0.049"), ("straw", "0.061")),
                ),
            ),
            (
                (
                    "Results at a mean temperature of 30 °C. Table 3 gives the conductivity of each panel "
                    "dry, measured after the conditioned series."
                ),
                Table(
                    "3",
                    "Table 3. Thermal conductivity, dry, mean temperature 30 °C.",
                    CONDUCTIVITY,
                    (("wood fibre", "0.041"), ("cork", "0.046"), ("hemp fibre", "0.047"), ("straw", "0.057")),
                ),
                (
                    "Moisture raised the conductivity of every panel, the fibre panels most. The cork panel "
                    "changed least, which agrees with its low moisture uptake (note S2, Table 2)."
                ),
            ),
        ),
    ),
    Source(
        "S2",
        "Note S2: density and moisture of the panels tested in report S1",
        "S2.md",
        (
            (
                NOTICE,
                (
                    "The specimens of report S1 were weighed after drying at 70 °C to constant mass and "
                    "again after conditioning at 23 °C and 80 % relative humidity. Table 1 gives the "
                    "oven-dry density, Table 2 the moisture content after conditioning."
                ),
                Table(
                    "1",
                    "Table 1. Oven-dry density of the specimens of report S1.",
                    DENSITY,
                    (("wood fibre", "160"), ("cork", "110"), ("hemp fibre", "45"), ("straw", "100")),
                ),
                Table(
                    "2",
                    "Table 2. Moisture content after conditioning at 23 °C and 80 % relative humidity.",
                    ("Panel", "moisture content, % of dry mass"),
                    (("wood fibre", "9.5"), ("cork", "6.0"), ("hemp fibre", "12.0"), ("straw", "14.5")),
                ),
            ),
        ),
    ),
    Source(
        "S3",
        "Review S3: conductivities of bio-based insulation reported in earlier studies",
        "S3.md",
        (
            (
                NOTICE,
                (
                    "Earlier studies of similar panels report conductivities a little apart from one "
                    "another, as densities and test conditions differ. Table 1 gives the median of the "
                    "values they report, dry, at mean temperatures between 10 °C and 23 °C. They are not "
                    "measurements of the panels of report S1."
                ),
                Table(
                    "1",
                    "Table 1. Median thermal conductivity reported in earlier studies, dry.",
                    CONDUCTIVITY,
                    (("wood fibre", "0.039"), ("cork", "0.045"), ("hemp fibre", "0.044"), ("straw", "0.055")),
                ),
            ),
        ),
    ),
    Source(
        "S4",
        "Sheet S4: properties the suppliers of the panels declare",
        "S4.md",
        (
            (
                NOTICE,
                (
                    "The suppliers declare a nominal density and a design conductivity for each panel. A "
                    "declared value is a production target, not a measurement of the specimens of report S1."
                ),
                Table(
                    "1",
                    "Table 1. Declared nominal density.",
                    DENSITY,
                    (("wood fibre", "150"), ("cork", "120"), ("hemp fibre", "40"), ("straw", "105")),
                ),
                Table(
                    "2",
                    "Table 2. Declared design thermal conductivity.",
                    CONDUCTIVITY,
                    (("wood fibre", "0.040"), ("cork", "0.041"), ("hemp fibre", "0.041"), ("straw", "0.050")),
                ),
            ),
        ),
    ),
)


# --- The search tool's index -----------------------------------------------------------------


def _table_text(table: Table) -> str:
    return " ".join([table.caption, *(f"{left} | {right}." for left, right in (table.header, *table.rows))])


def index() -> dict[str, object]:
    """The sources of the packet and its passages: a paragraph, or a whole table."""
    passages = []
    for source in SOURCES:
        for number, blocks in enumerate(source.pages, 1):
            page = number if source.paged else None
            for block in blocks:
                table, text = (block.number, _table_text(block)) if isinstance(block, Table) else (None, block)
                passages.append({"source": source.source_id, "page": page, "table": table, "text": text})
    return {
        "sources": {source.source_id: {"title": source.title, "file": source.file} for source in SOURCES},
        "passages": passages,
    }


# --- The text sources ------------------------------------------------------------------------


def markdown(source: Source) -> str:
    lines = [f"# {source.title}", ""]
    for block in (block for blocks in source.pages for block in blocks):
        if isinstance(block, Table):
            lines += [f"**{block.caption}**", "", f"| {block.header[0]} | {block.header[1]} |", "| --- | ---: |"]
            lines += [f"| {left} | {right} |" for left, right in block.rows]
        else:
            lines.append(block)
        lines.append("")
    return "\n".join(lines)


# --- The PDF ---------------------------------------------------------------------------------
#
# A PDF 1.4 file of the standard Helvetica fonts, which every reader has, with WinAnsiEncoding,
# which holds every character of the report but its symbol λ: the PDF writes "conductivity".
# Its content streams are not compressed, so a reader or a search finds its text as written.

_WIDTH, _HEIGHT, _MARGIN = 595, 842, 60
_LINE = 15
_WRAP = 88


def _literal(text: str) -> str:
    """A PDF string of a text, in WinAnsiEncoding, every byte outside ASCII written in octal."""
    out = []
    for byte in text.encode("cp1252"):
        if chr(byte) in "\\()":
            out.append("\\" + chr(byte))
        elif 32 <= byte < 127:
            out.append(chr(byte))
        else:
            out.append(f"\\{byte:03o}")
    return "(" + "".join(out) + ")"


def _show(x: int, y: int, text: str, *, bold: bool = False, size: float = 10) -> str:
    return f"BT /{'F2' if bold else 'F1'} {size:g} Tf {x} {y} Td {_literal(text)} Tj ET\n"


def _page_stream(source: Source, number: int, blocks: tuple[Block, ...]) -> bytes:
    y = _HEIGHT - _MARGIN
    ops = []
    if number == 1:
        ops.append(_show(_MARGIN, y, source.title, bold=True, size=12))
        y -= 2 * _LINE
    for block in blocks:
        if isinstance(block, Table):
            ops.append(_show(_MARGIN, y, block.caption, bold=True))
            y -= _LINE
            for row, (left, right) in enumerate((block.header, *block.rows)):
                ops.append(_show(_MARGIN + 20, y, left.replace("λ", "conductivity"), bold=row == 0))
                ops.append(_show(_MARGIN + 220, y, right.replace("λ", "conductivity"), bold=row == 0))
                y -= _LINE
        else:
            for line in textwrap.wrap(block, _WRAP):
                ops.append(_show(_MARGIN, y, line))
                y -= _LINE
        y -= _LINE
    ops.append(_show(_MARGIN, _MARGIN - 20, f"{source.source_id}, page {number} of {len(source.pages)}", size=8))
    return "".join(ops).encode("ascii")


def pdf(source: Source) -> bytes:
    """The bytes of a source as a PDF of one page for each of its pages."""
    count = len(source.pages)
    kids = " ".join(f"{5 + 2 * page} 0 R" for page in range(count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
    ]
    for page, blocks in enumerate(source.pages):
        stream = _page_stream(source, page + 1, blocks)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_WIDTH} {_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {6 + 2 * page} 0 R >>".encode("ascii")
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)
    return bytes(out)


# --- Writing it ------------------------------------------------------------------------------


def build(packet: Path, index_file: Path) -> None:
    """Write the packet's files into one folder and the search tool's index as another file."""
    packet.mkdir(parents=True, exist_ok=True)
    for source in SOURCES:
        if source.paged:
            (packet / source.file).write_bytes(pdf(source))
        else:
            (packet / source.file).write_text(markdown(source), encoding="utf-8")
    index_file.parent.mkdir(parents=True, exist_ok=True)
    index_file.write_text(json.dumps(index(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build(PACKET, INDEX)
