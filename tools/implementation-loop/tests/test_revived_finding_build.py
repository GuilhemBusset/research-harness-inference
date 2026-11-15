"""Regression for the engine fault 8a5fa42bd3 (#317): #85 failed with `ceiling` on engine 1.2.4.

Its last answered call was an examination that left a blocking finding open, after a build whose
requests the other vendor had refused. Then every build was refused by the vendor (the usage
limit, which 1.2.7 repairs on the safety surface) until the infrastructure budget was spent. A
new engine version revives such an issue once, back into `build`. The decisions on the last
build's requests reached neither the build after the failed examination, which replaced the
feedback, nor the revived build, whose feedback the revival replaced again.

The engine runs are real (git, the verifier, merges into a bare remote); agents and GitHub are
the fakes of `test_verifier_feedback`."""

import pytest

from test_verifier_feedback import ISSUE, L, Loop, Phase

REFUSAL = "the path is outside what the issue asks for"
FINDING = f"blocking finding E{ISSUE}-1-1"   # the engine numbers it by round


class Agents(L.agents.FakeAgents):
    """The first build asks for a path outside its zones, refused with `REFUSAL`; the first
    examination leaves a blocking finding. While `refusing`, every build after that examination
    fails as #85's did, before any answer."""

    def __init__(self, scripts):
        super().__init__(scripts)
        self.refusing = True

    def build(self, vendor, req, plan, worktree, feedback):
        if self.refusing and self._examined.get(ISSUE):
            self.calls.append(("build", vendor, ISSUE, tuple(feedback)))
            return self._metered(ISSUE, "build", vendor, None, fail="timeout")
        result = super().build(vendor, req, plan, worktree, feedback)
        if self._built[ISSUE] == 1:
            result.requests.append(L.agents.Request("paths", "docs/b.txt as well", paths=["docs/b.txt"]))
        return result

    def decide(self, vendor, req, worktree, requests):
        super().decide(vendor, req, worktree, requests)
        return [L.agents.Decision(r["id"], False, REFUSAL) for r in requests]

    def feedback(self, k):
        return "\n".join([c for c in self.calls if c[0] == "build"][k - 1][3])


@pytest.fixture
def loop():
    run = Loop()
    yield run
    run.repo.cleanup()


def test_the_build_after_a_failed_examination_knows_its_refused_requests(loop):
    agents = Agents({ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"}, findings=1)})
    agents.refusing = False
    st = loop.run(agents)

    assert FINDING in agents.feedback(2) and REFUSAL in agents.feedback(2)
    assert st.phase == Phase.DONE, loop.failures()


def test_85_revived_after_its_ceiling_keeps_what_the_failed_round_told_it(loop, monkeypatch):
    agents = Agents({ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"}, findings=1)})
    with monkeypatch.context() as old:
        old.setattr(L.engine, "VERSION", "1.2.4")
        st = loop.run(agents)
    assert (st.phase, st.fail["cause"], st.fail["version"]) == (Phase.FAILED, "ceiling", "1.2.4")
    locked = dict(st.locked)
    assert locked, "the first examination locked the tests"

    agents.refusing = False
    calls = len(agents.calls)
    st = loop.run(agents)   # this engine's version: revived once, with a fresh ceiling

    (revived, *_) = [c[3] for c in agents.calls[calls:] if c[0] == "build"]
    assert revived[0].startswith("An earlier attempt failed (ceiling)")
    told = "\n".join(revived)
    assert REFUSAL in told, "the decision on the last answered build's request"
    assert FINDING in told, "the examination that failed the round"
    assert [e for e in loop.store.events() if e["kind"] == "revived"]
    assert set(locked) <= set(st.locked), "the locks survive the revival"
    assert st.phase == Phase.DONE, loop.failures()
