import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/implementation-loop/tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/implementation-loop"))

from implement_loop.agents import FakeAgents, Probe
from implement_loop.model import Phase
from test_engine import EngineCase

INVALID = "checks/" + "p" * 512 + ".sh"


class RefusedProbeExaminer(FakeAgents):
    def examine(self, *args, **kwargs):
        result = super().examine(*args, **kwargs)
        if result.probes:
            result.probes.append(Probe(INVALID, "D1", "bash " + INVALID,
                                       content="test -f docs/subject.txt\n"))
            for finding in result.findings:
                finding.probe = INVALID
        return result


class TestExaminationRecovery(EngineCase):
    def test_rejected_probe_does_not_crash_or_lose_a_blocking_finding(self):
        self.add_work(2, "docs/subject.txt", "ready", findings=1)
        self.epic(2)
        self.run_engine(agents=RefusedProbeExaminer(self.scripts))
        first = self.events("examined", 2)[0]
        assert INVALID in {entry["file"] for entry in first["rejected"]}
        assert INVALID not in first["adopted"]
        assert "checks/probe_2.sh" in first["adopted"]
        assert "open" in first["findings"].values()
        assert not self.events("step_crashed")
        assert self.phase(2) == Phase.DONE
        assert any(f["status"] == "closed" and f["probe"] is None
                   for f in self.store.issue(2).findings)
