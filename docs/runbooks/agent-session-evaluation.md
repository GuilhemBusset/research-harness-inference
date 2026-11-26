# An agent-session evaluation screened, qualified and compared

The evaluation measures whether an already-deployed pool serves a class of coding agents, for
capacity and for course quality, and puts the result on one public GitHub issue per experiment
([design](../design/agent-session-evaluation.md), "How the operator runs it"). It is an explicit,
on-demand experiment run by the operator, outside CI/CD. Nothing in the pipeline starts it, and a
merge does not run it.

There are two entry points to one controller (`tools/evaluate/controller.py`):

- **the terminal**: `scripts/evaluate.sh run …`, in the operator's shell with the credentials below;
- **a coding agent**: the paired `evaluate` skill (`/evaluate` in Claude Code, `$evaluate` in
  Codex). It submits a typed request to a driver the operator started and shows the run ID, the
  experiment issue and the status. The agent holds no credential. Ending the chat leaves the run
  going in the driver.

What a run measures is what the pool already serves. The model, the instance type and the engine
settings are deployed through the normal process ([image and engine changes](image-and-engine-upgrade.md)),
never by the evaluation. A run books the pool's hosts through the same session record as a class
([sessions](sessions.md)), so never run one on a pool a class is using.

**How the steps are written.** Commands of this repository are in `sh` blocks and are run from the
repository's root. A skill invocation is in a `text` block. The commands name what a run prints by
shell variables of your own:

- `$experiment_issue` is the experiment issue's number, at the end of the `issue:` URL a first run
  prints;
- `$run_id` is a run's ID, which `run` prints and `inspect` shows. `$reference_run` and
  `$candidate_run` are the two IDs of a comparison;
- `$submission_id` is what `submit` prints first;
- `$name`, `$reference` and `$candidate` are names of folders under `docs/evaluations/`, in lower
  case with hyphens.

In prose they are `<run-id>` and the like. `owner/name` stands for the experiment's repository. A
run's private files are under `.cache/eval/runs/<run-id>/`, which git ignores: its journal, its bundle and, for a
driver's run, `driver.log`. Never paste them, a booking time or any text AWS produced into an
issue, a pull request or a chat.

## When

- **Before adopting a configuration**: a `screen` is the short check that the path works end to
  end. It runs one replay repetition and one attempt of each core task on the
  anchor harness.
- **To qualify a configuration** for a class size: the `qualify` profile, in one booking or in
  several batches.
- **To compare two configurations** on the same experiment: a reference run and a candidate run
  that differ in one deployed lever, such as the host count, then their comparison.
- **Never during a class**: a managed run books the pool and stops its own booking at the end.

## Check

1. **The pool is deployed and serves the configuration under test.** Its release, model and
   engine settings are already deployed. A candidate that changes weights or engine flags is
   deployed first ([image and engine changes](image-and-engine-upgrade.md)). For a managed booking
   (`--for`), nobody else holds the pool's session: a run that finds one, or loses the race for
   it, stops with `pool-already-booked`. For `--session existing`, book a dedicated session
   yourself first ([sessions](sessions.md), Act, step 1). The run then checks its host count and
   the time left, and leaves its lifetime to you.
2. **The scoped prerequisites of the driver**, which only the operator holds:
   - an AWS session of your own in the driver's shell. It must be able to read the pools stack's
     scaler, two Parameter Store parameters, the outputs of the gateway and observability stacks,
     the spikes' canary keys and the metrics workspace. The two parameters are the pool's release
     (`/rhi/pools/<pool>/release`) and the deployment's session limit
     (`/rhi/config/session_max_hours`). Admission reads the limit to check `--for`; when it is not
     set, the default applies. The session must also be able to book, invoke the scaler for and
     stop the selected pool's session. Agents never receive it.
   - canary students with keys in the spikes' secret. Reissuing them revokes the old keys, so it
     is an owner setup operation done ahead of time, not a step of a run.
   - a GitHub token that can read and write the issues of the experiment's repository and nothing
     else, given to the driver only, as `RHI_EVALUATE_GITHUB_TOKEN`.
3. **The load driver's host.** A Linux host outside the fleet, with uv, the locked environment and
   a container runtime. Keep its location, CPU allocation and network path the same for every run
   you will compare. Set `RHI_EVALUATE_PATH_CATEGORY` (`in_region`, `cross_region` or
   `public_internet`, the default) to where it stands.
4. **The trial image is prepared and verified on this host.** The command checks the committed
   inventory, builds the image (or reuses the one with the recipe's digest) and verifies every
   harness, SDK and solver it holds with no network. See `setup/README.md`, "The evaluation's
   trial image".

   ```sh
   uv sync --locked
   uv run --locked python tools/evaluate/image
   ```

5. **The plan fits the booking.** `plan` is offline. It prints the cells, the agents and canary
   identities needed at once, the host-hours, a price estimate labelled with its rate's source
   (`example` or `supplied`), and the batches with the minimum window of each:

   ```sh
   scripts/evaluate.sh plan --profile screen --students 8 --instances 1 --for 3h
   scripts/evaluate.sh plan --suite course-v1 --profile qualify --students 8 --instances 1
   ```

## Act

1. **Start the driver** in a terminal of your own, with the AWS session of Check, step 2. The token
   is read without echo, so that it stays out of your shell's history:

   ```sh
   read -rs RHI_EVALUATE_GITHUB_TOKEN
   export RHI_EVALUATE_GITHUB_TOKEN
   scripts/evaluate.sh driver
   ```

   It listens on a socket only your user may reach (`RHI_EVALUATE_SOCKET`, or one in your runtime
   directory) and resumes any run a previous driver left unfinished. It keeps the token: a client
   never sends one. The experiment's repository comes with each request, so set it where the
   requests are made, in the shell the coding agent runs in:

   ```sh
   export RHI_EVALUATE_REPOSITORY=owner/name
   ```

   The skill otherwise takes this checkout's `origin`, and `submit` takes `--repository owner/name`.

2. **Screen a configuration**, from the coding agent:

   ```text
   /evaluate run --profile screen --pool coursework --students 8 --instances 1 --for 3h --issue new
   ```

   In Codex, write `$evaluate` instead of `/evaluate`. The skill submits the request and reports
   the submission ID, the run ID and, once GitHub has confirmed it, the experiment issue's URL. The
   same submission from a terminal, with no credential:

   ```sh
   scripts/evaluate.sh submit --profile screen --pool coursework --students 8 --instances 1 --for 3h --issue new --repository owner/name
   scripts/evaluate.sh inspect --submission "$submission_id"
   ```

   If `submit` reports `unanswered`, run it again with `--submission <submission-id>`, which returns
   the same run. Or run the screen in the terminal itself, with the credentials of Check, step 2 in
   that shell and `RHI_EVALUATE_REPOSITORY` set, while no other run uses the pool:

   ```sh
   scripts/evaluate.sh run --profile screen --pool coursework --students 8 --instances 1 --for 3h --issue new
   ```

   `--for` bounds the whole invocation, including the wait for capacity. If the hosts are not in
   service in time, the run records `capacity-unavailable` and releases its booking. A new issue's
   public question is `--question "…"`, and this configuration's label in it is `--target`
   (`reference` by default).

3. **Qualify in batches** when the plan does not fit one booking. Plan with the longest booking
   you will make. It must hold the longest cell, which `plan` prints as the minimum window, and is
   at most the deployed `session_max_hours`:

   ```sh
   scripts/evaluate.sh plan --profile qualify --students 8 --instances 1 --for 6h
   ```

   Each batch's command is printed with `"$pool"` and its own minimum window as `--for`. Give
   each batch that window or a longer one, up to the window you planned with. Run the first batch
   as printed. It opens the experiment with `--issue new`:

   ```sh
   scripts/evaluate.sh run --profile qualify --pool coursework --students 8 --instances 1 --for 6h --issue new
   ```

   It prints `issue: <url>`. Keep the number at its end, here 42, as `$experiment_issue`. Every
   later batch resumes that run on that issue, with a booking of its own and its own `--for`:

   ```sh
   experiment_issue=42
   scripts/evaluate.sh run --resume --profile qualify --pool coursework --students 8 --instances 1 --for 6h --issue "$experiment_issue"
   ```

   Completed cells are kept while the workload and the serving identity match. The run picks the
   remaining whole cells that fit the window, and an interrupted cell starts again. A bare
   `--resume` with `--issue new` is refused: there is nothing to resume yet.

4. **Measure in a session you booked.** Book a dedicated session of the pool
   ([sessions](sessions.md)). Then:

   ```sh
   scripts/evaluate.sh run --profile screen --pool coursework --students 8 --instances 1 --session existing --issue "$experiment_issue"
   ```

   From the coding agent:

   ```text
   /evaluate run --profile screen --pool coursework --students 8 --instances 1 --session existing --issue 42
   ```

   The run never stops that session. Stop it yourself when you are done ([sessions](sessions.md),
   Act, step 4).

5. **Compare two configurations on one experiment.** Measure the reference on the experiment's
   issue (step 2 or 3). Then change one deployed lever and measure the candidate on the same
   issue, with the same profile, students and seeds. The first comparison is one host against two
   (the pool's `max_instances` must allow two):

   ```sh
   scripts/evaluate.sh run --profile screen --pool coursework --students 8 --instances 2 --for 3h --issue "$experiment_issue"
   ```

   `--target` only labels a configuration when it creates an issue. A second run with the same
   pool, host count and release measures the same configuration again. To compare a model,
   quantization or engine change instead, deploy it between the two runs
   ([image and engine changes](image-and-engine-upgrade.md)) and keep the host count. Then post
   the comparison of the two runs, by their run IDs, on the issue:

   ```sh
   scripts/evaluate.sh compare --issue "$experiment_issue" "$reference_run" "$candidate_run"
   ```

6. **Cancel** a run. Its admissions stop, its trial containers end and the booking it made is
   released. A run cancelled before its issue creates and books nothing. From the coding agent,
   `/evaluate cancel <submission-id>`, which is:

   ```sh
   scripts/evaluate.sh cancel --socket --submission "$submission_id"
   ```

   A run in the terminal is cancelled from another terminal by its ID:

   ```sh
   scripts/evaluate.sh cancel "$run_id"
   ```

7. **Resume** a run that was interrupted: the driver or the host stopped, or its window ran out
   before its cells. A driver's runs are resumed by starting the driver again (step 1). A
   terminal run is resumed with its ID, the same request and a new window:

   ```sh
   scripts/evaluate.sh run --resume "$run_id" --profile screen --pool coursework --students 8 --instances 1 --for 90m --issue "$experiment_issue"
   ```

   It reuses the issue and the measured cells, and books again for the new `--for` only. A
   driver's run that resumes and finds its booking released books no other.

8. **Recover the publication.** A run's release of its booking never waits on GitHub. When GitHub
   did not confirm a write, `status` shows the publication `pending`, and `publish --resume`
   delivers what waits, once, with no booking and no GPU:

   ```sh
   scripts/evaluate.sh status "$run_id"
   scripts/evaluate.sh publish --resume "$run_id"
   ```

   The outbound comment and its run-ID marker are saved before each post. After an ambiguous
   answer, the marker is looked up on the issue, so a retry never posts a second scorecard. It
   exits 0 once nothing is pending, and 2 otherwise.

9. **Compare offline.** Each run's bundle is in `.cache/eval/runs/<run-id>/bundle`. `report`
   regenerates its scorecard and digest. `compare` prints the treatment diff, then the comparison
   (`--json` for one JSON document). Neither needs a credential or the network:

   ```sh
   scripts/evaluate.sh report .cache/eval/runs/"$run_id"/bundle
   scripts/evaluate.sh compare .cache/eval/runs/"$reference_run"/bundle .cache/eval/runs/"$candidate_run"/bundle
   ```

10. **Export the public evidence.** The export checks every value against the public format,
    regenerates the derived files and runs the hygiene scanner before it writes a new folder under
    `docs/evaluations/` ([evaluation bundles](../evaluations/README.md)). Land it through a normal
    pull request (the `ship` skill). Anyone can then regenerate and compare the exported bundles
    in a fresh checkout:

    ```sh
    scripts/evaluate.sh export .cache/eval/runs/"$run_id"/bundle --to docs/evaluations/"$name"
    scripts/evaluate.sh report docs/evaluations/"$name"
    scripts/evaluate.sh compare docs/evaluations/"$reference" docs/evaluations/"$candidate"
    ```

    `report` leaves an exported folder as it is, its `summary.sha256` included, which is the
    digest on the run's comment: `git diff --exit-code docs/evaluations/<name>` shows nothing.

## Verify

1. **The issue came before the booking.** A driver's run logs to
   `.cache/eval/runs/<run-id>/driver.log`, and a terminal run prints the same lines. The line
   `issue: <url>` comes before the first `progress: booking`, and that URL is the experiment's
   issue:

   ```sh
   grep -n -e '^issue: ' -e '^progress: booking' .cache/eval/runs/"$run_id"/driver.log
   ```

   The log is private. Read it here, and copy nothing of it elsewhere.
2. **The owned booking was released.** The run's `cleanup:` line, its status and the terminal
   comment read `released` for a managed booking. A session the run did not book reads
   `not-owned`: you stop it yourself. The pool's session is gone ([sessions](sessions.md),
   Verify).

   ```sh
   scripts/evaluate.sh status "$run_id"
   ```

3. **The scorecard is on the same issue.** The run's terminal comment carries its run ID, the
   configuration, the requested and served host counts, and the capacity and quality verdicts for
   both class shapes (hands-on and several-agents). It also carries p95 first token, p50 and p10
   stream speed, failures, task passes by harness, host-hours, the cleanup outcome and the
   bundle's digest. It shows only relative durations, never a booking time, an identifier or tool
   output. `status` reads the publication as `delivered`.
4. **The exit code is the verdict.** `0` means a completed profile met its floors at every load it
   measured. `1` is a measured miss. `2` means invalid, incomplete or inconclusive evidence, a
   failed or interrupted invocation, or a refusal. A pending publication changes no exit code.
5. **A comparison is on the issue.** `compare --issue` prints the comparison scorecard, with the
   limiting rows of each verdict or the evidence missing for one, then `issue: <url>` and
   `publication: delivered`. It refuses two runs of different issues or experiments
   (`not-comparable`) and posts nothing. On the issue, the final comparison comment names both
   run IDs and gives the candidate's capacity and quality verdict for each shape against the
   reference: `candidate-better`, `no-difference`, `candidate-worse`, or `inconclusive` where the
   evidence does not decide. Under the capacity verdicts, each shape has its limiting row in both
   runs: the floor with the least headroom (such as `speed_p10`), `met` or `missed`, its headroom
   and its repetition, or `no floor read` where a run has no evidence for that shape. Under the
   quality verdicts, a shape that decides nothing gives the reason (such as `missing-pairs` or
   `load-unshown`). It also gives the comparison's bundle digest.
