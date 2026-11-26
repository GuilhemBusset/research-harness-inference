+++
id = "E7-34"
title = "Make the evaluate skill report a cancellation request as a request"
labels = ["size:S", "kind:docs"]
epic = 744
+++

## Context

The evaluate skill tells the operator that a run that ended before its issue was confirmed "was cancelled" when `cancel_requested` is true. A run can fail its preflight, reach exit 2 and only then receive a cancellation request, so the skill misreports why it ended. The skill is kept byte-identical in two places that CI compares, and both are in the agents' own configuration, which an issue must name to change.

## Deliverables

- [ ] `.claude/skills/evaluate/SKILL.md` says `cancel_requested: true` means cancellation was requested, and attributes the end to cancellation only when the run record confirms that cause.
- [ ] `.agents/skills/evaluate/SKILL.md` carries the same text, byte for byte.

## Acceptance criteria

- [ ] `grep -n "was cancelled" .claude/skills/evaluate/SKILL.md` prints nothing.
- [ ] `cmp .claude/skills/evaluate/SKILL.md .agents/skills/evaluate/SKILL.md` exits 0.
- [ ] `scripts/check.sh all` exits 0.

## References

- `docs/design/agent-session-evaluation.md`
