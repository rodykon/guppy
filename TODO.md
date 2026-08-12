# Guppy v2: Deferred Work

Explicitly out of scope for the initial build. Each item was a real decision
point during design, cut deliberately to ship a simpler v1 of this
architecture first — not an oversight.

## Bounded retry loops between pipeline stages

Currently each of the four stages (planner, plan reviewer, implementer,
code reviewer) runs exactly once; a stage that can't produce something
usable emits SKIP and the whole job aborts with a "needs human" comment.

Future enhancement: let the code reviewer (or plan reviewer) kick a job back
to an earlier stage for a bounded number of retries (e.g. implementer redoes
its pass if the reviewer finds the diff broken beyond a direct fix) before
giving up. Needs: a cycle in the job state machine, an iteration cap per
job, and reconsidering how turn budgets compose across retried stages.

## Status/dashboard read layer

Currently the only way to see job status/history is querying the SQLite
store directly (or reading logs/artifacts off disk).

Future enhancement: a thin read-only layer over the same store — could be
as light as a `guppy status` / `guppy logs <job-id>` CLI, or a small local
web dashboard. Should not require changes to how jobs write their state;
this is presentation only.

## Worker network egress allowlisting

Worker containers currently get unrestricted outbound network access.

Future hardening: restrict egress to what's actually needed (GitHub,
package registries the target repo needs, Anthropic's API), so a
compromised or prompt-injected agent run can't make arbitrary outbound
calls (e.g. exfiltrating repo contents). Worth doing before running this
against repos/content you trust less than you do today. Likely needs
per-repo configurability (different repos need different registries).

## Re-trigger on issue edit

Currently each qualifying issue is processed at most once, ever (dedup by
repo + issue number). An issue that was SKIPped and then edited by the
reporter to clarify it will *not* be reprocessed automatically — the
reporter has to close it and open a fresh issue instead.

Future enhancement (not yet requested, noting the tradeoff for later):
track `updated_at` or a body hash per issue so a SKIPped issue can
re-qualify after a meaningful edit, without touching issues that already
resulted in a PR.
