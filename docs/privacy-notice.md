# Privacy notice: the course's inference endpoint

This notice tells students of the course what the inference endpoint records about them, where,
for how long, and how to get a copy or have it deleted. It follows the system design, sections
6.4 and 6.6.

Values in `[[double brackets]]` are the owner's to fill in (decision 9 of the design, section 15).
This file keeps them as placeholders, because the region and the owner's details stay out of the
public repository: the owner fills them in and signs the copy given to students.

## Who is responsible

The controller of this data is `[[controller's name, to be filled in by the owner]]`, who runs
the endpoint for the course. Contact: `[[contact address, to be filled in by the owner]]`.

## What is recorded

- **Your login.** You sign in with your GitHub account. The course roster keeps only your
  numeric GitHub user ID, not your handle, name or e-mail. The API key you receive is stored only
  as a hash.
- **Usage metadata, per student.** For each request: your roster alias, when, which model pool
  and route, the token counts, timings, the status and the cause of a failure.
- **Source addresses.** The network address your requests come from, in the load balancer, WAF
  (firewall) and gateway logs.
- **Per-student series.** Usage counters for dashboards, labelled with your roster alias, not
  your GitHub ID.

## What is not recorded

Your prompts and the model's responses are not stored. Neither is your GitHub token: it is used
once, at login, to read your user ID, and then discarded.

## Where it is kept

In an AWS account run for the course, in the region `[[AWS region, to be chosen by the owner:
US East by default; an EU region, chosen before the first login, if students are in the EU]]`.
Login goes through GitHub, which handles the sign-in under its own privacy terms.

## For how long

Nothing is kept past the end of the course plus 90 days. Every store has that retention set:
the roster and key records, the logs and the per-student series.

## Who can see it

The course's administrators, through the course's AWS account and its monitoring dashboards,
which sit behind a sign-in. Other students cannot see your data. AWS and GitHub process it as
service providers.

## Whether it counts toward grading

`[[The owner states whether usage of the endpoint counts toward grading, and how.]]`

## How to ask for a copy or for deletion

Write to the contact above. The administrators can export the data held about you, and erase it.
An erasure removes your rows (your roster entry and your keys) at once and cuts the link between
your roster alias and you. Logs and per-student series that carry the alias are not edited: they
expire with their retention, at the latest at the end of the course plus 90 days.

---

Signed: `[[owner's signature and date]]`
