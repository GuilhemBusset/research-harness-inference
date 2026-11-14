+++
id = "E3-35"
title = "Spike S3, second part: a Claude Code session shaped like the course's second session"
labels = ["size:M", "kind:test"]
epic = "E3"
blocked_by = ["E3-41"]
+++

## Context

The course's second session runs research skill packs inside the harnesses: a paper-writing pack and
a bundle of science skills (sections 4.2 and 6.1; evidence E25). They add three things that are about
vLLM more than about the model, each a limit to handle and to tell students about (section 4.2):

- they run several agents at once, which the capacity of section 4.7 counts;
- they read PDFs: Claude Code sends a PDF as a document block, which the request profile replaces
  by a line of text saying the endpoint cannot read it, and the student guide says to convert a PDF
  to text first, which the agent can do itself;
- they load skills and add tools in mid-session, which Claude Code carries in system messages placed
  mid-conversation; vLLM 0.33 accepts the blocks, and the request profile changes those messages'
  role. What that does to a skill or a tool has not been measured (section 6.2, step 4);
- Claude Code's built-in web search is expected not to work here, since its vendor's service runs
  it; the course's science skills search through public services from the student's machine. Not
  verified.

Spike S3 includes a run shaped like that session: a skill loaded and a tool added in mid-session, a
PDF read, a pack's parallel agents with a figure, a web search, each on the 8-bit and on the 4-bit
build. Pass, for this part: each step works or has a documented limit and workaround (section 12).
The first part of S3, the task in each harness, is issue E3-41, which this issue adds to: the same
harnesses at the same pinned versions (issue E3-16), and the same record, `docs/measurements/s3.md`.

The packs themselves are not in this repository. The run uses a small skill, a tool and a PDF of its
own that stand for them, generated so that nothing in them identifies anyone.

**The tool added mid-session** is a tool of a small MCP server of the spike's own, run over standard
input and output and written with the standard library only: one tool, echo, which returns the text
it is given. The session's first turn runs without it; the run then resumes the same session with an
MCP configuration that adds the server, so that Claude Code announces the tool in the middle of the
conversation, as a pack that adds a tool does. The step works when the model calls echo and its
answer quotes the text echo returned. The skill is loaded the same way, by the turn that needs it.

Agents build and test the job and the record. Running it needs the account and is the owner's: it
is an obligation here, never a pass. Writing a limit and its workaround for a step that does not work
is the owner's too, in the record.

## Deliverables

- [ ] `spikes/s3_second_session/` holds what the run uses: a small skill, the MCP server of the tool echo written with the standard library only, the program that generates a short PDF in the job, and a task that asks for parallel agents to produce a figure and for a web search.
- [ ] `spikes/s3_second_session.py` runs one Claude Code session with its default settings against a named pool through these steps: load the skill, add the tool echo by resuming the session with the server in its MCP configuration, read the PDF, run parallel agents that produce a figure, search the web.
- [ ] It records for each step whether it worked, by an observable result of the step, and labels the results with the build the pool serves.
- [ ] Its verdict passes when every step worked or the record holds a limit and a workaround for it, and it declares the names of the numbers it posts, one per step, and posts them with the code of issue E3-14.
- [ ] `src/rhi/pipeline/spikes.py` lists the job s3_second_session, which runs `spikes/s3_second_session.py`.
- [ ] `docs/measurements/s3.md` gains the second-session rows, pending until the run, with a place for each step's limit and workaround.
- [ ] `tests/unit/spikes/test_s3_second_session.py` covers the verdict on recorded outcomes and the echo server's answer to one call.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s3_second_session.py tests/unit/test_measurements.py` passes: recorded outcomes in which every step worked pass; a failed PDF step with a limit and a workaround recorded passes; the same failed step without them fails; and the echo server answers a call with the text it was given.
- [ ] `uv run --locked pytest tests/policy/test_pipeline.py tests/synth/test_spike_jobs.py` passes with the job in the list.
- [ ] The second-session run is made on the coursework pool with the 8-bit and the 4-bit build, and `docs/measurements/s3.md` records each step, with a limit and a workaround for each step that did not work (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.2, 6.1, 6.2, 12
- `docs/design/evidence.md`, E25, E28
