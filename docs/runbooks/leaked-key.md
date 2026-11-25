# A leaked key

Over a term, a student will probably leak a key: pushed in a homework branch, pasted in a chat,
left in a harness's settings file (system design, section 13). A leaked key gives inference in
that student's name, and nothing else: a key carries no other privilege. Once the gateway enforces
the session switch (issue E4-41, see the runbook `sessions.md`), it serves a key only
while a session is on; until that gateway is deployed, it serves the key at any hour. Either way
the key keeps working until it is revoked, or until it expires at the course end, so revoke it at
once, whether a session is on or not. Revoking it is the fix. A revoked key stops working within
60 seconds, which is the gateway's lookup cache (section 6.4).

This runbook shows no key, not even an invented one: the hygiene scanner refuses anything shaped
like one. A key starts with the prefix `rhi_`, followed by random characters and a checksum.

Revoking needs only the admin CLI. Reading the dashboards and logging in again assume the public
HTTPS listener is on, which it is not by default.

**How the steps are written.** A command of this repository (`rhi`, `rhi_admin`, or a script of
the repository) is in a code block and is checked by `tests/unit/test_runbooks.py`. A change to the
configuration in SSM is an edit of your configuration file, kept outside the working tree, followed
by the configuration step of `setup/owner-setup.sh`; this runbook needs none. Anything else (the
AWS console, GitHub, Grafana, `aws`, `gh`, `git`) is written out in full and is tested only by
walking through the runbook.

The admin CLI is run from the repository's root, with your own AWS credentials. The code blocks
call it `rhi_admin`; define that name once in your shell:

```sh
alias rhi_admin='env PYTHONPATH=admin:services/common:services/issuer:src uv run --locked --no-dev python -m rhi_admin.main'
```

The admin commands name a student by their numeric GitHub ID, the key of the roster. `1234567` in
the code blocks below is an example: replace it with the student's own ID, as `rhi_admin roster
list` prints it.

## When

A leak is noticed in one of these ways:

- **A scanner or a person finds a key.** A string that starts with `rhi_`, followed by random
  characters, in a repository, a branch, a pull request, an issue, a gist, a chat or a screenshot.
  The checksum lets a secret scanner tell a key from text that only starts the same way. A key
  that is public anywhere, even for a moment, even in a commit that was later removed, counts as
  leaked: the history keeps it.
- **A student says so:** they pushed their harness's settings, or lost a laptop with the CLI on it.
- **Usage that does not fit the student.** On the students dashboard, an alias asks for far more
  than its owner could, or asks while its owner is not in the class.

A key that only expired at the course end is not a leak: there is nothing to revoke.

## Check

1. **Whose key it is.** A key does not name its student, and the clear key is in no table: the keys
   table holds only its SHA-256. When the student reported it as their own, the student is known.
   Where the key was found is only a lead: a repository, a chat or a file can hold a key someone
   else pasted there (a teammate's, a classmate's that was copied, a fork of another student's
   work), so its owner is not, by that alone, the key's owner. Ask the person whose repository or
   message it is whose key it is, and where it came from; check it with that student, and with the
   usage of Check, step 2, which shows whose alias asks when the key is used. While the owner is
   still uncertain, revoke the keys of each student it could be (Act, step 1): a revocation costs a
   student only a new login, and a key left working costs more. Find each student's GitHub ID and
   alias on the roster:

   ```sh
   rhi_admin roster list
   ```

   The first column is the GitHub ID that the commands below take; the second is the alias, the one
   name of a student the dashboards show (section 6.6).

2. **What the key spent.** In Grafana, open the dashboard "Students". Each panel has one series per
   alias and pool: "Requests, a second", "Input tokens, a second, cached or not", "Cached input
   tokens, a second", "Output tokens, a second", "Tokens of token counts, a second", and "Errors by
   cause, a second". Set the time range to start before the moment the key could first have
   leaked (the date of the commit that holds it, for instance) and look at the student's alias:
   these panels show *when* the alias asked, as rates a second, not how much it asked in all.

   For a total over the range, use Grafana's Explore on the Prometheus data source, keep the same
   time range, and run, with the student's alias in place of `ALIAS` and the family of the
   quantity you want (`requests`, `input_tokens`, `cached_input_tokens`, `output_tokens`,
   `counted_tokens`) in place of `output_tokens`:

   ```text
   sum by (pool, alias) (increase(rhi_gateway_student_output_tokens_total{alias="ALIAS"}[$__range]))
   ```

   The `increase` is taken on each series first, each gateway task's apart, and only then summed:
   a task's counters restart from zero when it starts, and summing first would hide that. Choose
   the "Instant" query type to get one number per pool for the whole range. Prometheus
   extrapolates at the edges of the range, so the result is an estimate.

   It can also undercount, down to nothing. A gateway task creates an alias's series for a pool
   only when it counts that alias's first request there, so the first sample of a series already
   holds that request, and `increase` sees no change from it: a single request, or the first
   request of each task, can be missing from the total. A zero or an empty result therefore does
   not show that the key was not used. When the result is zero, empty or small, cross-check with
   the gateway's own log, which holds one record per request with the alias, the pool, the time and
   the tokens, and no prompt: in the AWS console, open CloudWatch, then Logs Insights, select the
   log group `/rhi/gateway`, set the same time range, and run, with the student's alias in place
   of `ALIAS`:

   ```text
   filter alias = "ALIAS"
   | stats count(*) as requests, sum(output_tokens) as output by pool
   ```

   The log substantiates how many requests the alias made, when, and the output tokens; not the
   input. A record's `input_tokens` is a generation's input for a generation, but for a token count
   (`POST /v1/messages/count_tokens`, which generates nothing) it is the count asked, and the
   record has no field that tells the two apart: a generation of 5,000 input tokens and a token
   count of 900 sum to 5,900 in the log. Do not add up `input_tokens` from the log. For the input,
   take the dashboard's two families apart, `input_tokens` for generations and `counted_tokens` for
   token counts, and report them to the student apart. The requests the log counts include token
   counts too, which also produce no output.

   When the hours matter, list each request instead:

   ```text
   filter alias = "ALIAS"
   | fields time, pool, input_tokens, output_tokens, status, cause
   | sort time asc
   ```

   The log group keeps each record for 90 days (section 6.6): older requests are no longer in it.

   Read what the panels show as an indicator, never as proof of who sent a request. The dashboard
   counts by alias, not by key, and the student's own key and the leaked key draw under the same
   alias and pool: a student with two installations (section 6.4) also has the usage of both in
   one series. Usage outside the class's hours, or on days the student says they did not work,
   is a sign that the key is used by someone else; it does not show it, and it does not say which
   requests were whose.

3. **Do not wait for the reading to revoke.** The reading of step 2 is for what you tell the
   student, not a condition of acting: a key that is public is revoked whatever it has spent.

## Act

1. **Revoke every key of the student.** The command revokes each of their keys, on every
   installation, whether or not the student is still on the roster, and prints how many it revoked:

   ```sh
   rhi_admin key revoke 1234567
   ```

   Each revoked key may still work for up to 60 seconds: the gateway keeps each lookup for 60
   seconds (section 6.4), so a gateway task that looked the key up just before the revocation keeps
   accepting it until that lookup expires. A request accepted through the cache, once its body has
   arrived, first writes its reservation against the student's budget, then has its key read again
   past the cache; when the key is revoked by then, the gateway withdraws that reservation and
   refuses the request, so it reaches no engine. Nothing makes the 60 seconds shorter; they bound
   the exposure.

   Run the revocation a second time now, before you tell the student anything and before any new
   key can be issued to them (step 2): it revokes only keys that are not revoked yet, so it prints
   that it revoked 0 keys. A number above 0 means a login stored a key between the two runs, and
   that key is revoked too; run it again until it prints 0. Never run it after the student has
   logged in again or been sent a key with `rhi_admin key issue`: it would revoke that replacement
   key as well.

   ```sh
   rhi_admin key revoke 1234567
   ```

   When the student is to lose access altogether rather than log in again, remove the entry
   instead, which revokes their keys as well:

   ```sh
   rhi_admin roster remove 1234567
   ```

2. **Tell the student,** in the course's private channel, never in the place where the key leaked:

   - their keys were revoked, every installation's, and why; what the dashboard shows their alias
     spent since the leak, if anything looked wrong (Check, step 2): numbers of tokens, not money,
     and nothing the student owes, with the input of generations and the tokens of token counts
     given apart, never added together;
   - to log in again on each installation they use, which issues a new key there (the endpoint is
     stored from their first login):

     ```sh
     rhi login
     ```

     Logins are limited to 5 keys per student per UTC day by default. If the student is refused
     for that limit, issue the key yourself; the command prints it once, and you send it to them in
     the private channel, never anywhere else:

     ```sh
     rhi_admin key issue 1234567
     ```

   - to remove the key from where it leaked: delete the file or the message. In a repository,
     removing the file in a new commit leaves the key in the history; the revocation is what makes
     it harmless, so rewriting history is not required for this key;
   - to keep the key in the CLI's store and fetch it through the harness's key helper, which
     `rhi login` configures, rather than pasting it into a settings file that gets committed;
   - if they suspect a key was taken from their machine rather than pushed, they can revoke every
     key of theirs themselves, from any installation, with a fresh device flow, then log in again:

     ```sh
     rhi revoke --all
     ```

## Verify

1. **The keys are revoked, and the replacement key is not.** This check writes nothing, so it
   leaves a replacement key valid, whether from a new login or from `rhi_admin key issue`. Export
   what is kept of the student and read its list `keys`, each with its `issued_at`,
   `expires_at` and `revoked_at`, in seconds since the epoch:

   ```sh
   rhi_admin privacy export 1234567
   ```

   Every key issued before the revocation of Act, step 1 has a `revoked_at` that is a time; a key
   issued after it, the replacement, has `revoked_at` `null`. The export holds the student's data (their usage rows and log
   records as well): read it in the terminal and keep no copy of it. A student removed from the
   roster cannot be exported; for them the second run of the revocation in Act, step 1, which
   printed 0, is the check, and no key can be issued to them any more.

2. **The leaked key no longer works.** The proof is step 1: every key the student held is revoked
   in the keys table, and the gateway refuses a revoked key once its 60-second lookup has
   expired. The dashboard is not the proof. A revoked key's requests are refused before the
   gateway accepts the key, so they carry no alias and draw on no student's series, but a request
   accepted before the revocation can still finish, and count, a little after it. And once the
   student logs in again, their new key draws under the same alias: the alias then shows the
   student's usage and nothing else of a revoked key, but it never says who sent each request.
   If the usage under the alias still looks wrong a few minutes after the student has logged in
   again, start again from Check: another of their keys, or the new one, may be leaking.

3. **The student is back.** The student confirms that `rhi login` succeeded on each of their
   installations and that their harness works during the next session.

Walked through on the deployed system: not yet.
