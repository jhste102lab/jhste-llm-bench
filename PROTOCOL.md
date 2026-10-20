# Protocol v2 — fixed-delegation worker benchmark

This file is the human-readable contract for `protocol/v2.json`. Any change that can move a measured number requires a new protocol version; results from different versions are never ranked together.

## Scope

- Harness: [omp](https://github.com/can1357/oh-my-pi), pinned version in `protocol/v2.json`. Models run on subscription logins, not API keys.
- Roles: `task` (implementation worker) and `scout` (read-only exploration worker). Earlier v1 results (bulk lookup, web search, code review, and the head+review chain) stay published as v1 and are not re-measured.
- Claim: fixed-delegation worker performance on a small sample of tasks from a private repository. It is not a general model ranking.

## One run

1. A fixed head model (`head` in the protocol) receives the pack's frozen delegation and must call the task tool exactly once with exactly those arguments, then stop. No reviewer, no repair loop.
2. The candidate model runs as the omp subagent of the role under test (`task.agentModelOverrides`), with the worker tool list pinned in the protocol.
3. The runner checks that the head's delegation matched the frozen arguments and that the worker ran on the requested model without fallback. A mismatch is a harness fault (`invalid`), and the run may be retried.
4. The result is graded automatically (see Scoring). Head cost and time are recorded but excluded from the worker metrics.

The frozen delegation is produced once per pack by running the head on the pack's request (`bench freeze`): the head writes the assignment from the request alone (it does not read the repository), and the worker it spawns is stopped immediately. Every candidate gets the same text.

## Same-condition guards

- **Manifest.** `bench lock` hashes the protocol file, every pack file (request, delegation, source snapshot, oracle or checklist), the omp version and the dependency lockfile. `bench run` refuses to start if any hash drifted.
- **Config overlay.** Each run gets its own omp overlay: model fallback off, fallback chains empty, prewalk/advisor/memory/async off, MCP, GitHub, computer, archive and other non-worker tools disabled. Web search and web fetch stay enabled because they are part of real coding work; the web-search model is pinned.
- **Isolation.** The omp process runs under a macOS `sandbox-exec` profile that denies reading the contents of the original source checkout, all earlier experiment data and sessions (including database sidecar files), other runs, evaluator material, global agent instructions, MCP configuration and credential stores (file metadata stays readable so module resolution works; directory listings are denied). Writes to the bench home outside the run, to the shared dependency install and to source checkouts are denied. `GITHUB_TOKEN`/`GH_TOKEN` are removed from the environment and every run gets its own `TMPDIR`. The task source is a snapshot without git history or remotes. An isolation proof (read-deny, write-deny, allowed and traversal probes) runs before every batch; a failing proof aborts the batch.
- **Baseline.** Each role has a pinned baseline config. It is rerun in every batch. If the batch baseline score moves more than `baseline_drift_pp` from the latest accepted batch of the same role, protocol and judge (compared on their common packs), the batch is quarantined and excluded from publication until the owner accepts it (`bench accept`).
- **Rows.** One row is one model×effort config declared before the batch. Every attempt is kept.

## Run outcomes

| Outcome | Meaning | Scored |
|---|---|---|
| `completed` | worker finished | yes |
| `timeout` | worker hit the time limit; the repository state at that point is graded | yes |
| `quota-paused` | provider quota or rate limit stopped the run | no, retry later |
| `invalid` | delegation mismatch, model fallback, or wrong observed model | no, retry |
| `setup-error` | runner, sandbox, or dependency setup failed | no, retry |

## Scoring

- **task:** a per-case oracle (behavioural tests run after the worker stops, under a grading sandbox with the worker's read denies except that pack's oracle, and the worker's write denies). Every case is grounded in a sentence of the pack request. Pack score is the normalised gain over the untouched snapshot: `clamp((passed − baseline_passed) / (total − baseline_passed), 0, 1)`, so doing nothing scores 0 and breaking cases that already passed lowers the score. Raw counts are stored.
- **scout:** a checklist per pack. File items need the repository-relative path (a shorter suffix counts only when it is unique in the snapshot); symbol items need the identifier as a whole word. Fact items are graded by a judge model that sees the report with model and vendor names scrubbed; the judge runs without model fallback and its observed model must match the requested one. The judge model, effort, rubric version and prompt hash are stored with every grade. Re-grades are appended and never overwrite.
- **Config score** = mean of pack scores, each pack weighted equally (raw counts are kept in the detail view).
- **Ranking:** score (high first), then cost (low first). Time is shown but never ranked. A row is ranked only when it has a scored, audited attempt on every pack of its cohort; otherwise it is shown as incomplete, without a rank, below the ranked rows.
- **Small differences:** one attempt per config per pack by default. A row within `small_diff_pp` percentage points of the next-higher row is marked as a small difference. When a cohort is rerun, every config in it gets one more attempt.
- **Critical defect:** a failed oracle case or contradicted checklist fact marked critical (security or permission leak, audit or data loss, private value disclosure) flags the row. It does not change the score.

## Contamination

Every tool call of the head and the worker is audited after the run.

- An access that the sandbox denied is a **blocked attempt**. It is counted in the detail view and does not affect ranking.
- An access to forbidden material that succeeded (answer code, earlier sessions, other runs, evaluator material, credentials, the production site, the private repository on GitHub) marks the row as **answer access**. The score is still recorded and published, together with the access categories. The row gets no rank number and is listed below the ranked rows.
- Runs are never invalidated or rerun because of contamination. A run whose audit did not complete is not published.
- The head may only make the single frozen task call (plus `todo`). Any other head tool call, a second worker, or a model/effort override in the call makes the run `invalid`.

Known limits: network access is open (web search and fetch are allowed), so domain rules are enforced by the audit, not the sandbox. The omp credential store must stay readable for omp itself, so worker tools could read it; this is the same exposure as everyday use and is not a scoring path. Session files are written by the omp process the worker runs in, so a deliberately adversarial worker could alter its own evidence; the benchmark assumes cooperative models and the audit, oracle and identity checks would have to be fooled together.

## Metrics

- **Cost:** API-equivalent USD of the worker session (sum of per-request recorded costs, including nested sessions), plus web-search tokens priced at the pinned web-model rates. This is not the subscription charge. Token classes are stored for repricing.
- **Time:** worker session only, in minutes = model response time (sum of request durations) + tool execution time (union of tool-call intervals). Quota waits, retry backoff and idle time are excluded. Runs execute in parallel without a cap; parallel load can inflate tool time, so the detail view shows the model/tool split and the peak number of simultaneously running runs.
- **Identity:** requested model and effort, resolved model, and observed models are stored separately. Effort is the requested and harness-applied value; whether the backend honoured it cannot be verified.

## Cohorts

Rows are only compared inside a cohort: same protocol version, same role, and (for scout) the same judge model. Changing the judge creates a new cohort; earlier grades remain stored.

## Publication

Raw runs, sessions, task material and grades stay in a private local bench home. `bench export` builds `docs/data/v2.js` from an explicit field allowlist and fails if the output contains private terms or generic leak patterns. Public CI repeats the check with hashed terms as defense in depth.
