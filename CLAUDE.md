# CLAUDE.md — coding guardrails for this repo

Working principles for any agent (or human) modifying this project. Adapted from Andrej Karpathy's observations about LLM coding failure modes.

## 1. Think before coding

- Surface **assumptions** explicitly before acting. If two interpretations are reasonable, ask.
- When confused, **stop and ask** — do not guess and proceed silently.
- For non-trivial changes, write the plan in the relevant `docs/0X-…md` page **before** the code.

## 2. Simplicity first

- Ship the **minimum viable** code that satisfies the request. No speculative abstractions.
- Three similar lines beat one premature abstraction.
- No "future-proofing" without a concrete near-term need.
- No defensive error handling for cases that cannot happen. Validate at boundaries only.

## 3. Surgical changes

- Modify **only** what the task requires.
- Preserve existing style, naming, and comments.
- Remove **only** the code your changes orphaned.
- Don't refactor unrelated areas "while you're here".

## 4. Goal-driven execution

- Every phase has a written **definition of done** (in `docs/0X-…md` and the PR description).
- Multi-step work needs a verification loop: build → test → confirm → next.
- Don't mark a task complete until the success criterion is observably met.

---

## Repo conventions

- **Docs-first**: a phase doesn't start without its `docs/0X-…md` page being written first.
- **One PR per phase**: PR title = `Phase N — <short>`. Body links the brainstorm decision row(s) it implements.
- **Numbered docs and Terraform stacks**: `00-`, `01-`, … so order is unambiguous.
- **Python 3.11+ only**: matches `.python-version`. No 3.9 compat code paths.
- **dbt cross-warehouse via `adapter.dispatch`**: avoid duplicating models per warehouse.
- **No secrets in code or env files**: AWS Secrets Manager, namespaced `lending/<env>/<system>`.

## Cost rules

- **Default off.** Stop EC2, pause Redshift, suspend Snowflake between sessions.
- **No MWAA** until Phase 8b explicitly chooses to. ~$350/mo idle is not a learning expense.
- **One cost retro per major phase** in `docs/99-cost-retrospective.md`.
