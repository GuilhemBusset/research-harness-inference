# A leaked key

Over a term, a student will probably leak a key: pushed in a homework branch, pasted in a chat,
left in a harness's settings file (system design, section 13). A leaked key gives inference
during sessions, in that student's name, and nothing else: the gateway serves it only while a
session is on, and a key carries no other privilege. It keeps working until it is revoked, or
until it expires at the course end. Revoking it is the fix. A revoked key stops working within 60
seconds, which is the gateway's lookup cache (section 6.4).

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
   table holds only its SHA-256. When the student reported it, or the key was found in their own
   repository, the student is known. Otherwise ask the person who found it where it was, and whose
   work that was. Find the student's GitHub ID and alias on the roster:

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
   extrapolates at the edges of the range, so the result is an estimate, close enough to tell the
   student what their alias spent.

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
   accepting it until that lookup expires. A request that was accepted but whose body was still on
   its way is checked again, past the cache, before it reserves anything, and is refused then.
   Nothing makes the 60 seconds shorter; they bound the exposure.

   When the student is to lose access altogether rather than log in again, remove the entry
   instead, which revokes their keys as well:

   ```sh
   rhi_admin roster remove 1234567
   ```

2. **Tell the student,** in the course's private channel, never in the place where the key leaked:

   - their keys were revoked, every installation's, and why; what the dashboard shows their alias
     spent since the leak, if anything looked wrong (Check, step 2): token counts, not money, and
     nothing the student owes;
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

1. **The keys are revoked.** Run the revocation again: it revokes only keys that are not revoked
   yet, so it now prints that it revoked 0 keys, unless the student has logged in again in the
   meantime, whose new key it would revoke too. Run it again only before the student has logged in
   again.

   ```sh
   rhi_admin key revoke 1234567
   ```

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
