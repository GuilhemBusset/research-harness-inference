Plan this issue. {{branch_note}}

For **every** ledger item ({{ledger_ids}}) return one entry:
- `reading`: how you read it, and `quote`: words copied from the item that your reading relies on.
- `proof`, one of:
  - `test`: a durable test you will commit (pytest through uv; for infrastructure, assertions on
    the synthesized template);
  - `command`: a one-shot command whose recorded output proves it (for example a
    `cdk synth --lookups=false` that exits 0 with no AWS credentials);
  - `review`: the examiner judges it from the result (prose, documentation, fit with the system
    design, facts);
  - `owner`: only when it needs a capability this host lacks ({{lacks}}). Give that `capability`
    exactly, and the `agent_part`: what agents still build and prove (or `none`, with why). An
    item only a deployed environment, a real account or GPU capacity can show is such an item:
    its `agent_part` is the template and the assertions that can be proven here;
  - `deferred`: only when it needs another **open** issue's deliverable. Give `depends_on` and
    `depends_quote`, words from that issue's title or body that show it delivers what you need.
- `zones`: every path you expect to change, registrations and test files included. A protected
  path (anything under `tools/implementation-loop/`, `tools/hygiene/`, `.github/`, `.githooks/`,
  `tests/policy/`, `.claude/`, `.agents/` or `.codex/`, the gate scripts in `scripts/`, the
  ruleset) is yours only when the issue's
  deliverables name it, or name a protected directory above it. Listed here without that, it is
  not granted: plan around it, or ask for it during the build with a `protected_change` request,
  which the examiner decides.
- `decisions`: the choices that matter, each with its rationale. They are published in the PR.

Keep it proportional: the smallest correct plan that delivers every item as written.

{{feedback}}
