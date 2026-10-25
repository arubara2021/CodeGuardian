# v2 — Auto-Propose Mode

**Status:** Design. Not built.
**Depends on:** v1.0 shipped (Phases 0-16), Phase 15 (MCP + reporters) complete.
**Purpose:** Let the pipeline run unattended, produce verified diffs, open pull requests, and notify the team. The human approves by clicking Merge in GitHub. No CLI, no terminal, no typing.

---

## 1. What changes from v1

v1 has one workflow: a human runs `codeguardian refactor`, the CLI shows a diff, the human types `y`, the change lands in the local working tree.

v2 adds a second workflow: a trigger runs the pipeline, the pipeline produces a verified diff, pushes to a new branch, opens a PR, and posts to Slack. The human reviews the PR and clicks Merge.

Both workflows use the same pipeline. Same characterization tests. Same sandbox. Same three verifiers. Same audit chain. Same blast radius. Only the entry point and the exit point change.

| | v1 | v2 |
|---|---|---|
| Trigger | Human types a command | Schedule, webhook, or CI job |
| Approval point | CLI prompt before apply | GitHub Merge button |
| Output | Diff in terminal | PR on GitHub with diff, blast report, verifier verdicts, audit hash |
| Notification | None | Slack, email |
| Main branch protection | Local rollback | GitHub branch protection, PR review |
| Who can run | The person at the terminal | Anyone with access to the trigger |

---

## 2. The auto-propose pipeline

One run, start to finish.

| Step | What happens | Where |
|---|---|---|
| 1 | Trigger fires (cron, webhook, manual workflow dispatch) | External |
| 2 | Config loaded from `.codeguardian/auto.toml` | `config/loader.py` |
| 3 | Budget check: tokens spent so far vs `max_tokens_per_run` | New: `core/orchestrator/budget.py` |
| 4 | Target selection: files matching `targets_glob`, filtered by blast threshold | New: `core/orchestrator/target_selector.py` |
| 5 | Dedup check: skip targets with an open PR from a previous run | New: `core/orchestrator/dedup.py` |
| 6 | For each target: full v1 pipeline (recon, blast, context, tests, refactor, sandbox, verifiers) | Existing |
| 7 | If verifiers pass: create branch, apply diff, push, open PR | New: `core/publishing/` |
| 8 | If verifiers fail: log, skip, continue to next target | Existing error path |
| 9 | Send notification for each opened PR | New: `plugins_bundled/notifiers/` |
| 10 | Write run summary, exit | Existing |

The pipeline stops cleanly on the budget cap, not on a rate limit or a crash.

---

## 3. Consent model change

v1 has one consent scope: `apply`. The human approved writing to the working tree.

v2 adds a second scope: `propose`. The pipeline approved creating a branch and PR. The human still owns the merge.

| Scope | Meaning | Who grants it | Recorded as |
|---|---|---|---|
| `apply` | Write to the working tree in the local clone | Human at CLI | v1 behavior |
| `propose` | Create a branch, push, open a PR | Config file explicitly enabling auto-propose | New: `consent.origin = "config_auto"` |
| `verify` | Run cross-model verification | Human at CLI or config | v1 behavior |

The `ConsentRecord` schema gains one field: `origin: Literal["human", "config_auto", "ci_token"]`. Default is `"human"`.

The audit log records `origin` on every entry. A proposer run shows `origin = "config_auto"` and `blast_score = N`. A CLI run shows `origin = "human"`.

The guarantee changes from:

> "No code is modified without explicit consent."

To:

> "No code reaches `main` without consent. Consent is either a human at the CLI, a config file that documents the automation policy, or a GitHub Merge click. Every consent is recorded with its origin."

That is the one semantic change. Everything else stays.

---

## 4. Branch strategy

Every propose run creates its own branch. `main` is never touched by CodeGuardian.

| Aspect | Rule |
|---|---|
| Branch name | `cg/<target-slug>-<short-run-id>` — e.g. `cg-src-agent-utils-parse-config-4f8a` |
| Base | Current `main` HEAD at the moment the run starts |
| Commit count | One commit per target. One branch per target. |
| Force push | Never. If the branch exists, dedup skipped this target. |
| Delete on close | GitHub auto-deletes branches after PR close or merge |
| Never push to | `main`, `master`, any branch named in `.codeguardian/auto.toml` under `protected_branches` |

A hard check in the publishing code:

```
if target_branch in protected_branches:
    raise CodeGuardianError(APPLY_AUTH_FAILED, "protected branch")
```

That check is the safety boundary for the propose path, the same way the working-tree clone is the safety boundary for the CLI path.

---

## 5. PR creation

Uses `gh pr create` or the GitHub API. Whichever ships first in Phase 15.

**PR title:**
```
CodeGuardian: <directive> in <target> [blast <score>]
```

**PR body:**
```markdown
## Summary

**Target:** `src/agent/utils.py:parse_config`
**Directive:** add type hints and improve docstrings

**Blast radius:** 2 direct, 4 transitive, 0 violations, 0 gaps. Score 12 (proceed).

## Verifier verdicts

| Verifier | Verdict | Model | Family |
|---|---|---|---|
| Correctness | pass | gpt-oss-120b | openai |
| Security | pass | qwen3.8-27b | qwen |
| Contract | pass | qwen3.8-27b | qwen |

## Test results

- 8 characterization tests, all pass on old code
- 8 characterization tests, all pass on new code

## Audit

- Run ID: `4f8a2b1c`
- Consent origin: `config_auto`
- Chain head: `160d54a9a80362cf...`

---

*Opened by CodeGuardian. Review the diff and merge if approved.*
```

**PR labels** (for filtering):
- `codeguardian` — every CodeGuardian PR
- `cg/proceed` or `cg/review` — mirrors the blast recommendation
- `cg/directive/<slug>` — the refactor type
- `cg/auto` — from a scheduled run, not a human-triggered one

**Branch protection:** `main` requires at least one review and passes CI before merge. The PR cannot be merged by CodeGuardian. Only a human clicks Merge.

---

## 6. Notifications

Two channels for v2. More can be added as plugins.

### Slack

| Field | Value |
|---|---|
| Message | "New CodeGuardian PR: `parse_config` (blast 12, verifiers pass). <link>" |
| Channel | Configurable per run |
| Fallback | If Slack call fails, log to audit and continue. Do not block the pipeline. |
| Rate limit | One message per PR. Batch mode posts a summary after N PRs. |
| Secret | `SLACK_WEBHOOK_URL` from keychain, not config file |

### Email

| Field | Value |
|---|---|
| To | List from `auto.toml` |
| Subject | "CodeGuardian PR ready: <target>" |
| Body | Same content as PR body, plus link |
| Sender | Configured SMTP or a provider API (SendGrid, Resend) |
| Failure mode | Log, continue |

Both notifications are plugins. The pipeline does not depend on either. If both are down, the pipeline still runs, PRs still open, the audit log still records everything.

---

## 7. Config file

Path: `.codeguardian/auto.toml` in the target repo.

```toml
[auto_propose]
enabled = true
trigger = "nightly"
targets_glob = "src/agent/**/*.py"
directive = "add type hints and improve docstrings"
max_blast_score = 20
max_proposals_per_run = 3
max_tokens_per_run = 100000
protected_branches = ["main", "master", "release"]

[auto_propose.dedup]
skip_if_open_pr = true
label = "codeguardian"

[notifications.slack]
enabled = true
webhook_secret = "SLACK_WEBHOOK_URL"
channel = "#code-review"

[notifications.email]
enabled = false
recipients = ["team@example.com"]
```

**Every field has a default.** A missing config file means auto-propose is off. That is deliberate: the feature is opt-in, not opt-out.

`max_proposals_per_run` prevents one run from opening 50 PRs and burning the budget. `max_blast_score` prevents the pipeline from auto-proposing a change that touches 40 callers. `max_tokens_per_run` prevents the run from exhausting the model API key.

All three caps are required. A config without them fails validation at load time.

---

## 8. Operational concerns

These are not new features. They are the things that break when a pipeline runs unattended.

| Concern | Rule |
|---|---|
| **Deduplication** | Before proposing, check `gh pr list --label codeguardian --state open`. If a PR exists for the same target, skip. |
| **Rate limit on GitHub** | Cap at `max_proposals_per_run` per run. Sleep between PRs. |
| **Rate limit on model API** | The existing `FallbackModelProvider` handles this. Plus the token cap. |
| **Stale branches** | GitHub deletes branches after PR close. If not, a weekly cleanup job removes branches with `cg/` prefix older than 30 days. |
| **Silent failures** | Every skipped target is logged with a reason. Every failed verifier is logged. Every budget cap hit is logged. Nothing is silent. |
| **Partial run recovery** | Checkpoints still exist. A run that crashes at target 2 of 3 resumes at target 2, not target 1. |
| **Cost runaway** | The token cap is the hard stop. When it hits, the run ends cleanly. No exception, no crash. |

---

## 9. What does NOT change

Every v1 safety guarantee survives.

| Guarantee | Preserved in v2? | How |
|---|---|---|
| No write to `main` without approval | Yes | Branch + PR + GitHub branch protection |
| Characterization tests before refactor | Yes | Same pipeline, same order |
| Sandbox isolation | Yes | Same sandbox |
| Three independent verifiers | Yes | Same verifiers |
| Cross-model family independence | Yes | Same routing |
| Blast radius before refactor | Yes | Same blast engine |
| Hash-chained audit trail | Yes | Same chain, new `origin` field |
| Rollback on any accepted diff | Yes | Same `ApplyProvider.rollback` for the CLI path; branch deletion for the propose path |
| Human decides what reaches production | Yes | Merge button, not typed `y` |

The one thing that changes is the location of the human gate. From CLI prompt to GitHub Merge. The gate still exists.

---

## 10. Files added

New files, no changes to existing pipeline files.

| File | Purpose |
|---|---|
| `python/codeguardian/core/orchestrator/target_selector.py` | Picks targets from `targets_glob`, filters by blast score, respects `max_proposals_per_run` |
| `python/codeguardian/core/orchestrator/budget.py` | Tracks tokens per run, stops the run when the cap is hit |
| `python/codeguardian/core/orchestrator/dedup.py` | Checks for open PRs on the same target |
| `python/codeguardian/core/publishing/__init__.py` | Public surface |
| `python/codeguardian/core/publishing/branch.py` | Create branch, apply diff, commit, push |
| `python/codeguardian/core/publishing/pr.py` | Open PR with title, body, labels via `gh` or API |
| `python/codeguardian/core/publishing/notify.py` | Dispatch to notifier plugins |
| `python/codeguardian/plugins_bundled/notifiers/__init__.py` | Notifier plugin package |
| `python/codeguardian/plugins_bundled/notifiers/slack_notifier/plugin.py` | Slack webhook |
| `python/codeguardian/plugins_bundled/notifiers/email_notifier/plugin.py` | SMTP email |
| `schemas/consent.json` | Updated: add `origin` field |
| `schemas/auto-propose-config.json` | Config schema for `.codeguardian/auto.toml` |
| `tests/unit/core/test_target_selector.py` | Test target selection |
| `tests/unit/core/test_budget.py` | Test budget caps |
| `tests/unit/core/test_dedup.py` | Test dedup |
| `tests/unit/core/test_publishing.py` | Test branch/PR/notify |
| `tests/integration/test_auto_propose.py` | End-to-end with fake GitHub |

That is 16 new files and one schema update. No pipeline files touched. The `ApplyProvider` gets one new method: `propose_branch()`, which wraps branch creation and push. Everything else is additive.

---

## 11. Phasing

The work splits cleanly. Three sub-phases.

| Sub-phase | What ships | Effort |
|---|---|---|
| **v2.0a — Branch and PR** | Target selector, budget, dedup, branch creation, PR creation. No notifications. | 2-3 weeks |
| **v2.0b — Notifiers** | Slack and email as plugins. Config. | 1 week |
| **v2.0c — Config and CI integration** | `.codeguardian/auto.toml` schema, GitHub Actions example, docs | 1 week |

Total: 4-5 weeks of work on top of v1.0. Not a rewrite. Not a pipeline change.

---

## 12. Risks

| Risk | Effect | Mitigation |
|---|---|---|
| **Too many PRs** | Team ignores them | `max_proposals_per_run`, blast cap, dedup |
| **Cost runaway** | Budget exhausted | `max_tokens_per_run` hard stop |
| **Bad PR merged** | Production regression | Branch protection, required review, CI must pass |
| **Silent failures** | Pipeline broken, nobody notices | Slack/email on every run, even when zero PRs open |
| **Config drift** | Two repos with different policies, no visibility | Config schema validated at load, logged to audit |
| **Slack outage** | Notifications stop, pipeline continues | Notifications are best-effort, audited on failure |

None of these are new. They are the standard operational risks of any unattended pipeline. The v2 design addresses each one directly.

---

## 13. What this does not include

Explicitly not in v2.0.

| Item | Reason |
|---|---|
| Auto-merge without human review | Violates the consent principle |
| Direct push to `main` | Violates branch protection |
| Continuous watch on every commit | NG-S9 non-goal |
| Dependency upgrade proposals | Separate feature (v2.5 candidate) |
| Framework migration proposals | Separate feature (v3.0 candidate) |
| Jira, Linear, Notion integrations | Not in v2.0. Notifications are Slack and email only. |
| Bitbucket, Azure DevOps PRs | GitHub and GitLab only in v2.0 |
| Chat-driven refactor ("@codeguardian fix this") | Different interaction model. Separate feature. |

---

## 14. Acceptance criteria

The feature is done when:

1. A nightly GitHub Actions run opens 3 PRs, each with a verified diff, blast report, verifier verdicts, and audit hash.
2. The Slack message fires for each PR with the correct link.
3. `main` cannot be modified without a Merge click. Verified by attempting to push directly to `main` and watching it fail.
4. A run that hits `max_tokens_per_run` stops cleanly with a summary.
5. A run that finds an existing PR for a target skips that target and logs why.
6. Every run produces an audit log with `origin = "config_auto"` on every entry.
7. A PR that fails the required CI check cannot be merged by anyone.
8. A deleted branch after PR close does not leave a stale reference in the local clone.

---

## 15. The one-line summary

v2 adds a second way to use the pipeline: unattended runs that produce verified diffs, open PRs on isolated branches, and notify the team. The human approves by clicking Merge. `main` stays protected. The pipeline still verifies before anything is proposed. Nothing about the safety model changes. The only thing that changes is where the human says yes.
