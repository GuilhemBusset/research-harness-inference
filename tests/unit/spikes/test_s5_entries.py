"""How a posting of spike S5 cut short is completed (`spikes/s5_capacity.py entries`): the event
rebuilt from the runs a job printed is the event it sent, and is refused for a commit whose newest
posting of the label is not those runs'. No number here is a measurement; the keys are generated.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import s5_capacity as s5
from rhi_common.keys import generate_key
from rhi_reporter.measure import Verdict, read_event, still_to_post
from test_s5 import COMMIT, ENVIRON, job, settings
from test_s5_postings import LABEL, listed

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch, pool_state: Any) -> tuple[list[s5.Measured], dict[str, Any]]:
    """A job's runs, and the event it sent for them."""
    runs = job()
    entries: list[dict[str, str]] = []
    events = SimpleNamespace(put_events=lambda Entries: entries.extend(Entries) or {"FailedEntryCount": 0})
    monkeypatch.setattr(s5.boto3, "client", lambda service: events)
    pool_state.book()
    monkeypatch.setattr(s5.common, "canary_keys", lambda: SimpleNamespace(students=(generate_key(),)))
    monkeypatch.setattr(s5.config, "read_ssm", settings)
    monkeypatch.setattr(s5.Job, "measure", lambda self: runs)
    assert s5.main(ENVIRON | pool_state.environ) is Verdict.PASS
    (entry,) = entries
    return runs, entry


def printed(runs: list[s5.Measured], tmp_path: Path) -> Path:
    """The job's build log: its own lines, and one JSON line a run."""
    lines = ["pool coursework, run 8bit-baseline-1, streams [1, 8, 16], class of 20, 600 s a run"]
    lines += [line for one in runs for line in (one.to_json(), "[Container] waiting", "{}")]
    path = tmp_path / "build.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def entries(
    log: Path, statuses: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    """Runs `entries` on the build log and the statuses, as `gh api --paginate` writes two pages."""
    pages = json.dumps(statuses[:3]) + json.dumps(statuses[3:])
    monkeypatch.setattr("sys.stdin", io.StringIO(pages))
    argv = ["--runs", str(log), "--commit", COMMIT, "--label", LABEL, "--verdict", "pass"]
    code = s5.entries_main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def test_the_rebuilt_entry_is_the_event_the_job_sent_and_completes_its_posting(
    sent: tuple[list[s5.Measured], dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs, entry = sent
    parts = read_event({"detail": json.loads(entry["Detail"])})
    assert len(parts) > 3
    held = listed(parts[:-2])

    code, out, err = entries(printed(runs, tmp_path), held, monkeypatch, capsys)
    assert code == 0
    (rebuilt,) = json.loads(out)
    assert (rebuilt["Source"], rebuilt["DetailType"], rebuilt["EventBusName"]) == (
        entry["Source"],
        entry["DetailType"],
        entry["EventBusName"],
    )
    assert read_event({"detail": json.loads(rebuilt["Detail"])}) == parts
    assert "2 of the run's parts remain" in err

    # What the reporter posts for it makes the posting whole, which the record reads as one.
    rest = still_to_post(parts, held)
    assert rest == parts[-2:]
    _, postings = s5.postings_of(listed(parts[:-2], rest))
    assert postings[LABEL][0] is s5.Posting.WHOLE


def test_no_entry_is_written_for_a_commit_whose_newest_posting_differs(
    sent: tuple[list[s5.Measured], dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs, entry = sent
    parts = read_event({"detail": json.loads(entry["Detail"])})
    held = listed(parts[:-2])
    held[-2]["description"] = held[-2]["description"].replace("=", "=9", 1)

    code, out, err = entries(printed(runs, tmp_path), held, monkeypatch, capsys)
    assert (code, out) == (1, "")
    assert err.startswith("entries: ")
