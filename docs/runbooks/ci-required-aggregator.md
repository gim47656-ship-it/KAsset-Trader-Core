# `ci-required` aggregator + HANDOFF-only change classifier

**Status: active for HANDOFF-only changes.** The classifier skips resource
jobs only when `HANDOFF.md` is the sole added or modified path. Mixed lanes
and all fail-closed outcomes keep the full CI topology.

## 1. Why this exists

Branch protection currently requires:

```
ci-required
migration (PostgreSQL 15)
frontend
```

The stable aggregate decouples branch protection from the four test-shard
display names. The first active optimization is deliberately narrow:
documentation-only changes no longer start PostgreSQL/Redis service containers
or install Python/Node dependencies. Any broader lane cutover is a separate
change.

| piece | file | what it is |
|---|---|---|
| change classifier | `scripts/ci/classify_changes.py` | deterministic changed-path → lane mapping, fail-closed |
| aggregate evaluator | `scripts/ci/aggregate_required.py` | fixed-name gate over the required children's results |
| workflow wiring | `.github/workflows/test.yml` (`change-classifier`, resource-job conditions, `ci-required`) | runs classification first; only an exact HANDOFF-only verdict skips resource jobs |

## 2. Change classifier contract

Invoked as `python3 scripts/ci/classify_changes.py` (stdlib only, no
dependency install). It reads `$CLASSIFY_BASE_SHA` / `$CLASSIFY_HEAD_SHA` (or
`--base-sha` / `--head-sha`, or a `--name-status-file` for offline use) and
emits a JSON report plus `$GITHUB_OUTPUT` keys.

There are exactly three outcomes, and none of them is "run less because
something went wrong":

| outcome | exit | when |
|---|---|---|
| `classified` | 0 | every changed path mapped to a known lane, and every change was an add or a modify |
| `run_all` | 0 | any unknown path, any rename/copy/delete/type-change/unmerged path, any shared CI/config/test-infrastructure file, an empty change set, or an absent base SHA |
| `error` | **1** | a head SHA that was not supplied, a supplied SHA that stays unresolvable after `git fetch` (shallow/incomplete history, or an object that is not a commit), a failing `git` invocation, or a malformed `--name-status` record |

**Resolution order matters.** A supplied head is resolved *before* the
absent-base branch is taken. Otherwise an unresolvable head plus a missing or
all-zero base returned a conservative green `run_all`, making an invalid head
indistinguishable from a legitimate first-push one. The two cases must stay
distinct:

| base | head | outcome |
|---|---|---|
| absent / `0*40` | resolvable | `run_all`, exit 0 — a real first push |
| absent / `0*40` | unresolvable or not a commit | **`error`, exit 1** |
| present | unresolvable | **`error`, exit 1** |

(`--name-status-file` remains a pure offline path and resolves no SHA at all.)

Lanes and the jobs each one implies:

| lane | example paths | jobs |
|---|---|---|
| `docs` | top-level `HANDOFF.md` only | *(none)* |
| `app` | `app/**` | `lint`, `test`, `taskiq-smoke`, `security` |
| `tests` | `tests/**` | `lint`, `test` |
| `research` | `research/**`, `research_contracts/**` | `lint`, `research` |
| `scripts` | `scripts/**` (except `scripts/ci/**`) | `lint`, `test` |
| `frontend` | `frontend/**` | `frontend` |
| `migrations` | `alembic/**` | `lint`, `test` |
| `config` | `config/**` | `lint`, `test` |
| `ci_shared` | `.github/**`, `scripts/ci/**`, `docs/**`, `README.md`, `CLAUDE.md`, `AGENTS.md`, `pyproject.toml`, `uv.lock`, `Makefile`, `.test_durations`, `tests/conftest.py`, `tests/_socket_guard*.py`, `env.example`, `scripts/setup-test-env.sh`, `alembic.ini`, `codecov.yml` | **forces `run_all`** |
| `unknown` | anything unmatched | **forces `run_all`** |

Design points that look over-conservative and are meant to be:

- A rename inside a single lane still forces `run_all`. The pre-image path is
  part of the blast radius and `--name-status` gives no guarantee the two
  sides belong to the same lane's test surface.
- The error path still writes `run_all=true` into `$GITHUB_OUTPUT` before
  exiting 1, so a consumer reading a partially written output file cannot
  infer reduced coverage from a crashed classifier.
- **Paths are matched literally.** Nothing is stripped or normalized first.
  Git tracks `" docs/only.md"` (leading space) as a file in a directory named
  `" docs"`, and it is emitted verbatim under `--name-status -z`; trimming it
  would answer "docs-only, no jobs needed" for a path that is not under
  `docs/` at all. Unmatched literals are `unknown` → `run_all`.
- **Only `HANDOFF.md` is metadata-only.** Tests read exact text from
  `docs/**`, `README.md`, `CLAUDE.md`, and `AGENTS.md`; those paths therefore
  force `run_all`. Unlisted top-level Markdown also remains `unknown` and
  forces `run_all`.

### NUL stream and status grammar

`--name-status -z` output is parsed strictly, because a parser that
resynchronises quietly is a parser that reports the wrong lane confidently:

- The stream must be NUL-terminated; a truncated one is red.
- Exactly one trailing empty field (the terminator) is permitted. Any *other*
  empty field means an embedded NUL, so record boundaries cannot be trusted →
  red. (Previously such fields were filtered out, silently re-pairing statuses
  with the wrong paths.)
- The status token is validated as a whole *before* its first character is
  used: a single letter from `A C D M R T U X B`, optionally followed by a
  1–3 digit score for `R`, `C` and `M`. Anything else (`AA`, `Z`, `A1`,
  `R1000`, `m`) is red rather than truncated into a status git never emitted.
- A **scored `M`** (`M100`) is git's dissimilarity score for a *rewrite*,
  emitted under `-B`. It is valid git output, so it is not red — but it is not
  an ordinary modify either, so the whole token is kept, which places it
  outside `CLASSIFIABLE_STATUSES` and forces `run_all`.

## 3. `ci-required` aggregate contract

`ci-required` declares `if: always()` and
`needs: [lint, test, taskiq-smoke, change-classifier]`. `always()` is
load-bearing: classifier or child failure must still produce the fixed required
check.

`scripts/ci/aggregate_required.py` evaluates `toJSON(needs)`:

| child result | verdict |
|---|---|
| `success` | pass |
| `skipped` | pass **only** with `--authorize-skip <name>` |
| `failure` / `cancelled` | red |
| absent or unknown result | red |

The workflow adds `--authorize-skip lint`, `test`, and `taskiq-smoke` only when
the classifier reports `run_all=false` and `lanes=docs`. A classifier failure,
mixed lane, unknown path, unsafe Git status, or unexpected skip stays red.

The *declaration itself* is validated before the payload is even read
(`validate_configuration`), so a malformed gate can never reach a verdict:

- an empty `--required` list is red (an aggregate with no children passes
  vacuously);
- a blank or whitespace-only `--required` / `--authorize-skip` name is red —
  argparse turns `--required ''` into a one-element list that satisfies the
  non-empty check while naming no real job;
- a duplicated `--required` / `--authorize-skip` name is red;
- an `--authorize-skip` name that is not in `--required` is red.

For the matrix `test` job, GitHub collapses all four shards into a single
`needs.test.result`, which is `failure` if any shard failed. The aggregate
therefore covers all four shard results through one child entry.

## 4. Verification

`tests/ci/test_ci_required_workflow_contract.py` machine-checks that:

- the live protected names remain `ci-required`, `migration (PostgreSQL 15)`,
  and `frontend`;
- every resource job has the same fail-closed HANDOFF gate and depends on the
  classifier;
- the test matrix remains Python 3.13 × four shards;
- `ci-required` runs with `always()` and authorizes exactly the three
  HANDOFF-only child skips;
- only gated jobs and the aggregate read classifier outputs.

Run:

```bash
uv run pytest tests/ci/ -q -ra
actionlint .github/workflows/test.yml
```

## 5. Expanding beyond HANDOFF-only

Do not reuse the docs condition for another lane. A broader cutover requires:

1. Evidence that the lane-to-job map covers every consumer and generated
   artifact affected by that path.
2. Contract tests for mixed-lane changes and every newly authorized skip.
3. `ci-required` `needs:` and `--required` parity for every merge-gating job.
4. A real PR demonstrating that required skipped checks report and that a
   classifier failure remains red.

Unknown paths, deletes, renames, copies, rewrites, and shared CI/config inputs
must continue to force full coverage.
