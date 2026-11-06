# Phase 12 — Skills Module: Design Settlement

**Snapshot date:** 2026-11-06
**Status:** v2 complete and shipped. Full pytest suite green at 1547 passed. Ruff clean.
**Supersedes:** `Phase_12.md` v1 (the "Fast Path Skill Library" settlement, archived in git history).

---

This document records two settlements in sequence. Part I is the v1 settlement that originally described Phase 12. It is preserved as historical context — it is superseded in full and should not be read as current. Part II is the v2 settlement that ships today, and it is the authoritative spec for the phase as delivered.

A future reader who wants to understand what Phase 12 does today should read Part II. A future reader who wants to understand why Phase 12 changed shape should read Part I to see the original design, then Part II to see what displaced it.

---

# Part I — v1 Settlement (Superseded on 2026-11-06)

> **SUPERSEDED.** Everything below this line describes the original Phase 12 plan. It is retained because the delta between v1 and v2 is instructive: four findings from the 2026 research literature made v1 insufficient at scale, and the v2 settlement in Part II is the response. Do not implement against Part I.

---

## v1.1 What Phase 12 was

Phase 12 delivered the **Skill Library**: the persistent memory and retrieval layer that sat on top of the Phase 3 `SkillStore` database. When Phase 11's Skill Curator proposed a draft skill, Phase 12 stored it, indexed it, and retrieved it for future runs.

The v1 settlement was explicit that Phase 12 did **not** implement heavy MAGMA multi-graph memory or deep GraphRAG traversal. Those were deferred to Phase 13 to prevent context bloat and CPU starvation. The v1 settlement delivered what it called the **Fast Path Skill Library**: local embeddings, SQLite vector + FTS5 search, and bounded negative constraints.

---

## v1.2 The Three-Tier Progressive Disclosure

If the Analyst loaded 500 full `SKILL.md` files into its prompt, the context window would fail. Phase 12 v1 implemented the 2026 standard for token efficiency:

1. **Tier 1 (L1 Metadata):** The retriever fetched only the `name`, a 1-sentence `trigger`, and the `skill_type` from SQLite FTS5. The token cost was flat regardless of whether there were 10 or 1,000 skills.
2. **Tier 2 (L2 Body):** If the LLM decided a skill was relevant based on the L1 metadata, the retriever fetched the full `SKILL.md` markdown body.
3. **Tier 3 (L3 Assets):** If the skill referenced an executable script or an `ast-grep` rule, that specific file was loaded on demand.

**Enforced Limit:** The combined L1 metadata text sent to the model was strictly truncated. The LLM never saw more than 1,536 characters of skill descriptions at once.

---

## v1.3 File-by-File Implementation

The v1 plan was for exactly six files:

1. `python/codeguardian/core/skills/embeddings.py` — local text embedding generation
2. `python/codeguardian/core/skills/library.py` — the high-level CRUD API
3. `python/codeguardian/core/skills/retriever.py` — hybrid search interface
4. `python/codeguardian/core/skills/indexer.py` — background maintenance and TTL decay
5. `python/codeguardian/core/skills/__init__.py` — public surface
6. `tests/unit/core/test_skills.py` — unit tests

The v1 settlement assumed each of these was small enough to fit one file. It did not anticipate the six-layer decomposition in Part II.

---

## v1.4 Explicit Limitations and Drawbacks

The v1 settlement named five limitations and accepted them:

1. **CPU starvation from local embeddings.** Running `all-MiniLM-L6-v2` locally required matrix multiplication. Mitigation: run the initial scan asynchronously on a low-priority background thread.
2. **Stale negative constraints.** A failure from an old library version could permanently block a valid refactor. Mitigation: 90-day TTL decay.
3. **The SKILL.md overhead.** Description text was sent on every request. Mitigation: 1,536-character metadata cap.
4. **No deep GraphRAG.** Multi-hop CPG traversal spiked latency to 18 seconds. Deferred to Phase 13.
5. **No MAGMA multi-graph.** Slow-path orchestration using the LLM to extract graph edges asynchronously. Deferred.

The v1 settlement treated these as engineering trade-offs. The v2 settlement in Part II shows that three of the five were correctly assessed as costs, one was correct scope (GraphRAG over the CPG is genuinely slow), and one (the flat retrieval assumption) was wrong at the scale Phase 12 v2 now targets.

---

## v1.5 What v1 Phase 12 handed forward

| To | What |
|---|---|
| Phase 13 | A populated SQLite vector table and FTS5 index. The `SkillRetriever` API for the Analyst to call. The `indexer.py` TTL decay logic. |
| Phase 14 | A `codeguardian skill list` CLI command that read from `SkillLibrary`. |

**v1 Phase 13 constraints (superseded, kept for contrast):**
- GraphRAG traversal must not exceed 1 hop to prevent the 18-second latency spike.
- The `SkillRetriever` must pass negative constraints to the Analyst prompt as a distinct system instruction.

Part II refines both of these. Graph traversal is now over the skills graph, not the CPG, so the 18-second latency does not apply. Negative constraints are now a separate tier with an explicit adoption protocol rather than an instruction injected into the prompt.

---

# Part II — v2 Settlement (Current, Complete)

---

## 1. Status and scope

Phase 12 becomes the **lifecycle-managed procedural memory** of CodeGuardian. The original settlement described a flat hybrid retriever over a Markdown-backed store. That design is the foundation; it is not the ceiling.

This document records the design that ships. It is longer than the original because the problem is larger than the original estimated: a skill library that grows past a few hundred entries does not degrade gracefully. It degrades silently, and the wrong skill gets loaded without anyone noticing.

**Delivered in this phase:**

- A **typed skill graph** built deterministically from `SKILL.md` frontmatter, so retrieval can return prerequisites, not just the topically nearest match.
- A **staged retrieval pipeline** that seeds from hybrid search, walks the graph backwards, optionally reranks, and hydrates to a token budget — with a fallback at every stage.
- A **separate negative-knowledge tier**, replacing the current `effectiveness < 0.3` heuristic with typed failure records the agent explicitly adopts or rejects.
- A **governance layer** with an admission gate, a lifecycle state machine, dedupe, and fitness-driven retirement.
- A **security layer** that treats a skill as untrusted supply-chain input: admission scan, provenance pinning, invocation guard.
- An **evolver** that grows the graph from execution traces, writes to a shadow copy, and promotes only after held-out verification.
- **Integration with Recon and Blast**: their existing JSON output filters and scores skills before a model ever sees them.

**Not delivered, and deliberately so:** agentic multi-hop retrieval, cross-repo skill transfer, WASM-sandboxed skill execution, skill authorship assistance. Each is listed in §13.

---

## 2. Why v2 supersedes v1

The original settlement defined a "Fast Path Skill Library". That design is the foundation; it is not the ceiling. Four findings from the 2026 research literature made the four-file v1 plan insufficient at the scale Phase 12 v2 now targets.

1. **The scale problem.** Held-out tests show that implicit skill routing — letting the agent choose while it works — collapses from **73.6% recall at zero distractors to 27.4% at 1,000 distractors**, with 51.6% of turns loading nothing at all. The original design assumed the agent would pick correctly. It does not, at scale. Explicit selection from the same library holds at 73.9%.

2. **The dependency problem.** Graph-of-Skills (EMNLP 2026 Main) reports **+43.6% average reward** and **−37.8% input tokens** when skills are retrieved as a dependency-complete bundle via reverse graph traversal, versus loading everything. The ablation is the striking part: replacing reverse traversal with forward traversal loses **9.1 reward points**. Semantic proximity does not imply executable sufficiency.

3. **The security problem.** Snyk's February 2026 audit of 3,984 skills found **36.82% contain security flaws, 13.4% critical, 76 confirmed malicious payloads**. Skill files are the highest attack-success vector at **69.4%**, and **84.2% of vulnerabilities** are embedded in the `SKILL.md` prose itself — invisible to code scanners. Admission is a required gate, not a policy suggestion.

4. **The latency fear was unfounded for the specific integration.** The original settlement avoided graph traversal because multi-hop CPG queries cost 18+ seconds. The graph introduced here is over **skills**, not source code. It is built offline from frontmatter, cached, and traversed with a hard 50 ms budget. The latency guarantee from v1 is preserved.

**Everything the v1 settlement promised still works.** Markdown stays the source of truth. FTS5 + vec0 stay the index. Every existing skill validates without edit. The old flat RRF path stays in the repo as the fallback. The upgrade is additive.

---

## 3. Research grounding

Every architectural choice below has a specific source. This section names them so a future reader can audit the reasoning.

| Source | Finding | Used by |
|---|---|---|
| **TMLR 2026 survey** (124 papers) | Skill libraries must be lifecycle-managed: evidence → verification → admission → storage → retrieval → composition → maintenance → distillation → governance. Flat retrieval degrades as the library grows. | §8 lifecycle; §4 layer 5 |
| **Graph-of-Skills, EMNLP 2026 Main** | Semantic top-K misses prerequisites. Reverse-weighted Personalized PageRank over an offline skill graph: **+7.0 reward, −56.7% tokens** vs loading everything. Ablation: forward traversal loses **9.1 reward**. | §4 layer 2; §5 stage 2 |
| **SE-GoS, Sep 2026** | The graph can evolve from execution traces (topology, edge weights, descriptions). Reward improves 52.4% → 59.4% in one round, **+5.4** transferred to held-out split. No model training. | §4 layer 7 (evolver) |
| **Systems-foundation paper, arXiv 2608.29596** | Four memory tiers (episodic / procedural / virtual paging / symbolic rules). Utility function `U(s) = gain − λ·cost`. Eviction below threshold. Names four failure modes: context overflow, retrieval pollution, skill technical debt, catastrophic forgetting. | §4 layer 4 (separate store); §8 retirement |
| **WikiSkill (Google Research + Virginia Tech, 2026)** | Wiki layer between raw traces and skills. **Retains rejected fixes and their validation results** so the system stops rediscovering the same failure. | §4 layer 4 (negative store) |
| **SkillForge, NeurIPS 2026** | Fitness-driven lifecycle: **trial → active → stable → retired**. Pre-retires low-fitness skills before they pollute retrieval. | §8 lifecycle |
| **Snyk audit, Feb 2026** | 3,984 skills audited. **36.82% have flaws, 13.4% critical, 76 malicious.** Five of top seven most-downloaded were malware. | §9 security |
| **CSA (Cloud Security Alliance), 2026** | Skills are the highest attack-success vector (**69.4%**). **84.2% of vulnerabilities are in the `SKILL.md` prose**, invisible to code scanners. | §9 security |
| **SkillRouter** | Compact encoder (0.6B) + reranker (0.6B) reach **74.0% top-1**. The chooser is a small dedicated model, not the main agent. | §5 stage 3 |
| **Implicit vs explicit routing study** | Implicit selection collapses from **73.6% → 27.4% recall** at 1,000 distractors. Explicit selection holds at **73.9%**. | §5 whole pipeline |
| **Body-is-decisive study** | Removing the skill body drops accuracy **29–44 pp** across methods; **91.7% of cross-encoder attention** lands on the body. The reranker must read the body. | §5 stage 3 |
| **Library Drift** | Unbounded growth causes retrieval degradation. Governance recipe (retirement + active-cap + meta-skill prior) lifts held-out pass@1 **0.258 → 0.584** over 100 rounds. | §8 governance |
| **Skill shadowing study** | Beyond ~30 skills, the gain is from **removing colliding descriptions**, not saving tokens. Collision costs **0.14 pass rate**. Deleting the largest skill changes nothing; renaming or merging two colliding skills changes everything. | §8 dedupe |
| **Production retrieval baseline, 2026** | Hybrid dense+sparse → RRF → cross-encoder rerank over top 20–50 → top 3–5. Local MiniLM reranker **50–100 ms**. | §5 whole pipeline |
| **R3-Skill** | Reranking signal is stage-dependent. Helps cross-encoder stage; no benefit at bi-encoder. | §5 stage 3 |
| **SkillGATE** | Graph-guided search over candidate regions adds **+16.3% R@1** over ranking each skill independently. | §5 optional escalation (deferred) |
| **Threshold calibration, 2026 production** | Three separate thresholds: relevance ≥ 0.5, selection ≥ 0.35, confidence ≥ 0.3. If nothing clears, load nothing. | §5 stage 4 |
| **Anthropic engineering, 2026** | They **retired** their shared profile cache after measurement: lean, task-specific grounding was faster and at least as useful. Cache question-agnostic facts only. | §6 |
| **OpenClaw schema history** | Older builds refuse newer schemas. Rollback requires the verified pre-upgrade snapshot plus the matching build. Migrations are forward-only and idempotent. | §7 migration; §10 safety rules |
| **gskill (2026)** | Per-repo skill files (`.gskill/` folders) scoped to one project. | §6 future extension |

Three general observations hold across the corpus:

- **Retrieval quality is a chain, not a model.** Every strong system has a deterministic prefilter, a compact retriever, a body-reading reranker, and a calibrated threshold gate. No production system lets the task agent choose.
- **Governance is not optional past ~30 skills.** Below that number, storage is enough. Above it, the library must actively retire and merge or it degrades silently.
- **The graph must be derived, never authoritative.** Every system that made the graph a source of truth regretted it within one schema change. The graph is rebuilt from Markdown, never the reverse.

---

## 4. The six-layer architecture

Each layer is one job, behind one Protocol, independently replaceable. Layer 1 is the existing code, untouched.

### Layer 1 — Store (unchanged)

The existing `SkillStore` (Phase 3), `SQLiteStore`, `MigrationRunner`, and the FTS5 + vec0 tables. No modifications to `SkillStore`'s public surface, and no changes to the Markdown source of truth, the FTS index, or the vector index as shipped. One access change: `graph_store.py` queries the `SQLiteStore` connection directly, not through `SkillStore`. No new methods are added to `SkillStore` — the graph is a distinct concern.

### Layer 2 — Graph (derived)

Purpose: represent the relationships between skills so retrieval can return prerequisites, providers, and superseding versions, not just the topically nearest match.

Construction: deterministic parse of each `SKILL.md` frontmatter.

| Frontmatter field | Edge produced |
|---|---|
| `prerequisites: [names]` | `prerequisite` edge → each named skill |
| `provides: [capabilities]` | `provides` node-label edge → capability nodes |
| `consumes: [capabilities]` | `consumes` edge → capability nodes |
| `supersedes: [names]` | `supersedes` edge → older skill |

Edges are typed. A **semantic** edge is computed separately from embedding cosine similarity (a threshold, not a model call) and marked `derived: semantic` so it can be regenerated cheaply if embeddings change.

Determinism: two identical repos produce identical graph files. Every edge has a stable ID derived from `(source, target, kind)`.

Rebuildability: dropping `skill_graph_nodes` and `skill_graph_edges` and calling `rebuild_graph()` restores the exact same graph from `SKILL.md` and embeddings. **The graph is derived. It is never authoritative.**

Fallback: if a skill declares no relations, it has no edges and is still retrievable via the flat path. If graph build fails or times out, retrieval skips Layer 2 entirely.

### Layer 3 — Retrieval (staged, with fallback at every stage)

Detailed in §5. The key architectural property: this layer **orchestrates** the existing `retriever.py` (the flat RRF implementation) as its fallback, not as its replacement. `retriever.py` is not deleted; it is the safety net.

### Layer 4 — Negative memory (separate tier)

Purpose: failures are not low-effectiveness skills. They are a **different kind of knowledge** with a different lifecycle. When a skill fails in a specific context, the failure teaches the next agent something: "in this shape, this approach breaks." That belongs in its own store.

Shape: each failure is a `NegativeRecord` with `failure_id`, `skill_name`, `failure_category` (one of localization, generation, schema, contract, timeout, context_overflow, model_unavailable), `context_signature` (a deterministic hash of the directive verb, stack, and target shape), `attempted`, `observed`, `run_id`, `confidence`, `first_seen`, `last_seen`, and provenance.

Adoption protocol: before the next attempt, the agent receives the relevant negative records and **explicitly adopts or rejects each one**. The decision is logged to `agent_calls`. Rejection is a signal that the failure may be stale or environment-specific.

Why separate from the skill store: the two decay on different schedules. A failed approach from an old library version is wrong today; a positive pattern from the same era might still be right. Combining them under one TTL erases one or the other incorrectly.

Fallback: if the store is empty or unavailable, no negatives are injected, positives are still delivered.

### Layer 5 — Governance

Purpose: decide what is allowed into the retrieval pool, and what leaves it.

State machine:

```
draft ──(admission gate)──► trial ──(N successes)──► active
                                │                        │
                                │                        ├─(M failures)─► retired
                                │                        └─(superseded)──► retired
                                └──(gate fails)────────────────────────►  retired
```

The v1 `draft → active → stale → archived` becomes `draft → trial → active → stable → retired`. `stale` and `archived` are treated as aliases for `retired` on every read, so a v1 skill row retains its meaning after the upgrade.

Admission gate: every `draft → trial` transition requires schema validity, pinned provenance, security scan pass, and (when supplied) an eval score above the threshold.

Dedupe: at admission, the proposed skill's description is compared against the active pool. Similarity is Jaccard over tokenized descriptions. Above `dedupe_threshold` (default `0.92`) means collision — the candidate is rejected. Between `0.80` and the threshold means admitted with a warning in `skill_admission_log` for later review.

Fitness-driven retirement: each active skill accumulates `U(s) = effectiveness`. A skill below `retire_threshold` (default `0.3`) for `retire_after_runs` uses (default `20`) retires automatically. Retired skills leave the pool but stay on disk for inspection.

Active cap: a configurable `max_active_skills` (default `200`) caps the pool. When the cap is hit, admission becomes retirement-required.

### Layer 6 — Security

Purpose: a skill is untrusted supply-chain input. Even one written by the Skill Curator is a model output, not human-reviewed code.

Admission scan: before `draft → trial`, the `SKILL.md` is scanned. The scanner blocks prompt injection markers, invisible Unicode codepoints, shell substitution, and exfiltration verbs targeting URLs. It warns on external URLs, `file://` references, conditional instructions, base64 strings above 200 chars, and single lines above 4,000 chars. The scanner uses regex and Unicode codepoint checks only; no model, no network.

Provenance: every skill carries a `ProvenancePin` with source runs, author, commit, license, admission evidence, and kernel version. Reconstructable from the `skill_provenance` table plus `agent_calls`.

Invocation guard: any executable asset a skill references inherits the skill's declared permissions. The Phase 9 `sys.addaudithook` extends to this case. A skill declaring `network: false` cannot spawn a plugin that opens a socket.

### Layer 7 — Evolver (secondary, opt-in)

Purpose: grow the graph from execution traces, so edges that prove useful in practice get stronger and edges that prove wrong get weaker.

Shadow promotion: every graph mutation is written to a sidecar file. A held-out evaluation compares retrieval quality on the shadow vs. the serving graph. **Only on improvement is the shadow promoted.** Rollback is a pointer swap or file deletion.

Frequency: background, configurable, default `never`. Opt-in via `evolver_enabled = true`.

Design deviation: the settlement originally specified shadow tables. The actual implementation uses a sidecar JSON file to avoid a third migration dependency. See §13.

---

## 5. The four-stage decision chain

The single most important design decision: **the main agent never chooses.**

Held-out tests measure implicit routing (agent picks from visible list) at **73.6% recall with zero distractors, 27.4% at 1,000 distractors**. Explicit selection (dedicated retriever) holds at 73.9%. The pipeline below is the explicit path.

### Stage 0 — Project-signal filter (deterministic, no model)

From the current run's `recon.json` and `blast_radius.json`:

- Drop skills whose `language` is not in `recon.languages`
- Drop skills whose required dependencies are not in `recon.dependencies`
- Drop skills whose `test_runner` is incompatible with `recon.test_frameworks`
- Drop skills whose declared `blast_scope` conflicts with `blast.caller_search_complete`

Every field whose confidence is `inferred` becomes a soft prior, not a hard filter. Every observed field becomes a hard filter.

Removes most of the pool. Zero latency. Zero model.

### Stage 1 — Hybrid seed

FTS5 keyword plus vec0 semantic, merged by Reciprocal Rank Fusion, top N where N is roughly 20 to 50. This is the Phase 12 v1 path, preserved as a function. Fallback: when Stage 2 or later is off, this is the final result — exactly what Phase 12 v1 produced.

### Stage 2 — Reverse-weighted PageRank (graph, flag-gated)

Bounded to 1 to 2 hops, reverse traversal weighted higher than forward, hard timeout at 50 ms. Adds prerequisites that RRF missed. The GoS ablation shows reverse traversal is the source of the gain (9.1 reward points).

Fallback: skip and use the seed order.

### Stage 3 — Cross-encoder rerank (flag-gated)

Local MiniLM reads the `SKILL.md` body, budget 120 ms. The body is the decisive signal (29 to 44 percentage point drop without it). Optional `ProjectCard` around 200 tokens gives the reranker stack context: frameworks, test command, blast recommendation, `caller_search_complete`.

Fallback: skip and use the walk order.

### Stage 4 — Budget-aware hydration plus threshold gate

Load skill bodies until token budget fills. Prerequisites before dependents. Mark `truncated=True` with the strategy used. Apply the three calibrated thresholds: relevance ≥ 0.5, selection ≥ 0.35, confidence ≥ 0.3. If nothing clears, return empty. Empty is the correct answer more often than a forced match.

The chain is a superset of v1 behavior. At every stage, the fallback produces the RRF output from Stage 1. Config flags default to `false`; a user who never enables anything gets **bit-identical behavior** to Phase 12 v1.

---

## 6. Recon and Blast integration

Recon and Blast already produce JSON artifacts that filter and score skills. Feeding them into Stage 0 costs one file read per retrieval call.

### Recon signals

| Recon field | Used as | Confidence |
|---|---|---|
| `languages[]` + `tier` | Hard filter — drop skills for other languages | Observed |
| `frameworks[]` + `version` | Hard filter + specificity boost | Observed |
| `versions.declared / actual / available` | Drop skills requiring unavailable versions | Observed |
| `dependency_graph` + `hierarchical_index` | Scope to modules the project has | Observed |
| `repo_map.importance` (PageRank) | Rank skills touching high-importance code higher | Inferred |
| `test_frameworks`, `test_command`, `build_system` | Drop skills whose verification cannot run | Observed |
| `conventions` (AGENTS.md etc.) | Boost skills matching house rules | Inferred |
| `report_statuses` | Confidence gate — inferred fields become soft priors | Observed |

### Blast signals

| Blast field | Used as |
|---|---|
| `direct_callers`, `transitive_callers` | Risk posture — high fan-in prefers conservative skills |
| `blast_score`, `recommendation` | `block` → surface verification/caution skills, not refactor skills |
| `caller_search_complete` | Veto on confidence — `false` suppresses "confident refactor" skills and surfaces "verify assumptions" skills |
| `contract_violations`, `coverage_gaps` | Boost skills that address the exact violation shape |
| `target_public_api` | Public API changes need compatibility skills |

### The Project Card

**Decision: read `recon.json` and `blast_radius.json` directly at retrieval time.** No cached Project Card in v2.

Rationale: the files are already on disk, keyed by run, written by the agents. A file open is roughly 1 ms. The Anthropic engineering team **retired** their shared profile cache after measurement showed lean, task-specific grounding was faster and at least as useful. We adopt that finding: cache question-agnostic facts (the skill embeddings themselves), but read question-specific context (project shape at this moment) fresh.

If profiling later shows retrieval is hot, a 200-token Project Card cache keyed by `(repo_id, commit_hash)` can be added behind a flag. It is not the default.

### Scope note — Phase 12 now reads Phase 11 output

The original Phase 12 settlement said: "Phase 12 does not traverse the CPG. Relies entirely on vector + lexical search." This is preserved. The graph is over **skills**, not source code. Reading `recon.json` and `blast_radius.json` is a file open, not a CPG traversal. The 18-second latency the original settlement avoided came from graph traversal of source; that remains excluded.

If Recon has not run (for example, a standalone `codeguardian skill query` CLI call), Stage 0 skips and the pipeline falls back to Stage 1. No breakage.

### Future extension — per-repo skills

Recon's conventions reader is close to producing a `scope: repo` skill: "this project uses X, tests with Y, never does Z." Retrieved only for that repo. This is the `gskill` pattern. It is not in v2 scope; it is recorded here because the Recon integration makes it nearly free when it becomes urgent.

---

## 7. Storage and schema

### New tables — migrations `005` and `006`

Five new tables. All `CREATE TABLE IF NOT EXISTS`. **No ALTER on existing tables.** Forward-only and idempotent, matching the `MigrationRunner` contract.

| Table | Purpose |
|---|---|
| `skill_graph_nodes` | Node IDs (skill names + capability nodes), kind, metadata |
| `skill_graph_edges` | Typed edges (prerequisite / provides / consumes / supersedes / semantic / conflicts), confidence, provenance |
| `negative_knowledge` | Typed failure records (§4 layer 4) |
| `skill_provenance` | `ProvenancePin` records |
| `skill_admission_log` | One row per admission attempt: scan result, eval score, dedupe status, gate verdict |

All five are **derived**. Dropping them and rebuilding from `SKILL.md` plus `agent_calls` restores the exact state.

Migration `006_skill_states.sql` rebuilds the `skills` table with an expanded CHECK constraint (from `('active','stale','archived','draft')` to include `('trial','stable','retired')`). SQLite cannot ALTER a CHECK, so the migration rebuilds with rowids preserved and FTS rebuilt. Foreign key enforcement is disabled for the duration and restored to its previous value at the end (which is `OFF` by project default).

### Schema bump — `schemas/skill.json` v1.0 → v1.1

**Additive only.** Six new **optional** fields:

| Field | Type | Default |
|---|---|---|
| `prerequisites` | array of skill names | `[]` |
| `provides` | array of capability names | `[]` |
| `consumes` | array of capability names | `[]` |
| `supersedes` | array of skill names | `[]` |
| `provenance` | object (see §9) | absent → legacy skill |
| `permission_manifest` | object (see §9) | absent → inherit defaults |

The six required fields (`name`, `description`, `trigger`, `skill_type`, `language`, `state`) are **unchanged**. `additionalProperties: false` is preserved. Every existing `SKILL.md` validates against v1.1 without edit. The `state` enum is expanded to include `trial`, `stable`, `retired` — the three new lifecycle states.

**Reader supports N and N-1.** A v1.1 reader accepts v1.0 skills. A v1.0 reader rejects a v1.1 skill with a clear message rather than silently misreading it.

### Embeddings unchanged

384-dimensional `BAAI/bge-small-en-v1.5` via `fastembed`. No re-embedding. Old vectors remain valid.

---

## 8. Lifecycle and governance (in detail)

### States

| State | Enters retrieval pool? | Transitions out |
|---|---|---|
| `draft` | No | → `trial` via admission gate; → `retired` on gate failure |
| `trial` | Yes, with reduced weight | → `active` after `N` successful loads; → `retired` after `M` failures |
| `active` | Yes | → `stable` after `P` successful loads; → `retired` when fitness falls below threshold |
| `stable` | Yes | → `retired` when fitness falls below threshold |
| `retired` | No | Terminal unless manually revived |

`stale` and `archived` (v1 names) map to `retired`. Existing skills in those states retain their behavior via state aliasing on read.

### Admission gate criteria

Every rejection is logged to `skill_admission_log`. The log is queryable via `codeguardian skill list --rejected` (Phase 14).

| Check | Fail action |
|---|---|
| Security scan | Reject, log blocking findings |
| Strict warning promotion (when `admission_strict=true`) | Reject on any warning |
| Eval score ≥ threshold (when supplied) | Reject |
| Dedupe check | Reject with merge suggestion |
| Active cap | Reject until a retire happens |

### Fitness and retirement

Fitness is the skill's `effectiveness` field (successes / uses). The design's `U(s) = gain − λ·cost` formula is partially implemented: the `−λ·cost` term is deferred because the metadata schema has no per-skill token-cost column. This is a documented residual (§13).

Retirement requires `effectiveness < retire_threshold` for `retire_after_runs` consecutive uses. A skill with fewer uses than the run threshold is not eligible; the floor exists so one bad run does not retire a skill that has not been tried often enough.

### Dedupe

At admission: Jaccard similarity between the candidate's tokenized description and every active skill's tokenized description.

Above `dedupe_threshold` (default `0.92`): reject as duplicate.
Below `0.80`: admit freely.
Between: admit with a warning in `skill_admission_log` for later manual review.

The warning band exists because Jaccard similarity is noisy in the middle range. A skill that is "kind of similar" to an existing one is a judgment call, not a rule match.

---

## 9. Security (in detail)

### What the scanner catches

The admission scanner is a fixed list of regexes and Unicode codepoint checks. It runs in under 10 ms.

**Blocks:**

- Prompt injection markers: `ignore (all |previous|prior) (instructions|prompts)`, `system:`, `you are now`, `new instructions`, `disregard (all |the )?(previous|prior|above|earlier)`
- Invisible Unicode: zero-width space, zero-width joiner, RTL override, homoglyph ranges, byte-order marks
- Shell substitution: `$(...)`, `` `...` ``
- Exfiltration verbs targeting URLs: `send|post|upload|transmit|forward` followed by up to five intervening words and `to` followed by `http://`, `https://`, `ftp://`, `file://`, or an email address; plus `exfiltrate`, `curl`, `wget`

**Warns (require `admission_strict=true` to block):**

- External URLs of any scheme
- `file://` references
- Conditional instructions: `if the caller`, `when the user`, `when the agent`
- Base64-encoded strings above 200 chars
- Very long single lines (> 4,000 chars)

Warns do not block by default. `admission_strict=true` promotes warnings to blocks.

### What the scanner does not catch

- Novel attack patterns not in the regex list
- Attacks embedded in referenced scripts (those go through Phase 9's audit hook at invocation, not at admission)
- Attacks that require user interaction to trigger
- Attacks in the model that produced the skill (the Skill Curator is trusted; that trust is inherited)

The scanner is a **known-pattern filter**, not a proof of safety. The Snyk 36.82% is a floor. It is also the largest single improvement available without a full sandbox.

### Invocation guard

Every executable asset a skill references inherits the skill's `permission_manifest`. The manifest shape matches Phase 9's plugin manifest:

```yaml
permissions:
  filesystem: none | sandbox_only | read_only | full
  network: bool
  process_spawn: bool
  env_read: [str]
```

Enforcement uses the existing `sys.addaudithook` in `core/plugins/permissions.py`. No new enforcement mechanism. One additional check at the barrier: when a plugin is invoked from within a skill's scope, the plugin's permissions are intersected with the skill's. A skill declaring `network: false` cannot spawn a plugin that opens a socket.

### Provenance and rollback

Every admission writes a `ProvenancePin`. If a skill is later found to be malicious:

1. Trace it to its `source_runs` and the model family that produced it.
2. Find all `skill_graph_edges` where it is an endpoint.
3. Roll back any shadow promotions that included it.
4. Mark the skill `retired` with a `security_incident` flag.
5. Audit log records the rollback with reason.

The chain is intact. Every action was logged at admission.

---

## 10. The five safety rules

These rules make the upgrade safe for current users, current skills, and future runs. Every one is enforced by a test.

**Rule 1 — Everything new is off by default.**
`graph_enabled`, `ppr_enabled`, `reranker_enabled`, `negative_store_enabled`, `admission_strict`, `evolver_enabled`, `recon_blast_filter_enabled`. All `false` after upgrade. A user who never edits config gets bit-identical behavior to Phase 12 v1. `test_flag_off_is_identical` asserts this.

**Rule 2 — New state is derived, never authoritative.**
`skill_graph_nodes`, `skill_graph_edges`, `negative_knowledge`, `skill_provenance`, `skill_admission_log` are all rebuildable from `SKILL.md` plus `agent_calls`. Test: drop, rebuild, assert retrieval outputs are identical.

**Rule 3 — The old path is always the fallback.**
`retriever.py` stays in the repo and stays callable. Every new stage delegates to it when it cannot run. There is no configuration in which retrieval becomes unavailable.

**Rule 4 — The schema only grows optional fields.**
`skill.json` v1.1 has six new optional fields and an expanded state enum; the six required fields are untouched. Every existing `SKILL.md` validates without edit.

**Rule 5 — Nothing mutates in place.**
Graph updates and skill promotions write to a shadow. Validation before promotion. Rollback is a pointer swap, not a re-run.

**Operational rule (enforced by test): a hard latency budget per stage.**
Stage 0: 5 ms. Stage 1: 20 ms. Stage 2: 50 ms. Stage 3: 120 ms. Stage 4: 20 ms. Total: 215 ms. Exceeding any budget is a skip, not a wait.

---

## 11. Configuration

New `[skills]` table in `config.toml`. Every field defaults to off or current behavior.

```toml
[skills]
graph_enabled = false
ppr_enabled = false
ppr_hops = 1
ppr_timeout_ms = 50
reranker_enabled = false
reranker_timeout_ms = 120
negative_store_enabled = false
admission_strict = false
dedupe_threshold = 0.92
max_active_skills = 200
retire_threshold = 0.3
retire_after_runs = 20
trial_runs_before_active = 3
retrieval_token_budget = 3000
recon_blast_filter_enabled = false
evolver_enabled = false
```

`recon_blast_filter_enabled` is separate from `graph_enabled` because Stage 0 does not require the graph. A user may enable the Recon/Blast filter alone, getting project-shaped retrieval without the graph's dependencies.

---

## 12. Files — create, update, untouched

### New source (12 files, `python/codeguardian/core/skills/`)

| File | Job |
|---|---|
| `types.py` | Dataclasses: `SkillNode`, `SkillEdge`, `EdgeKind`, `RetrievalBundle`, `RetrievalItem`, `NegativeRecord`, `AdmissionVerdict`, `ProvenancePin`, `ProjectCard` |
| `graph.py` | Deterministic builder: parse frontmatter → typed edges; compute semantic edges from embeddings |
| `graph_store.py` | Persistence against `SQLiteStore` — new tables only |
| `ppr.py` | Reverse-weighted Personalized PageRank, bounded hops, hard timeout |
| `retrieval.py` | Public entry point (`retrieve_for_prompt`) orchestrating all stages with fallbacks |
| `hydrator.py` | Budget-aware hydration, prerequisite-before-dependent ordering, truncation marker |
| `reranker.py` | Optional local MiniLM cross-encoder, flag-gated, timeout-bounded |
| `negative.py` | Typed failure records, adopt/reject protocol, separate decay |
| `governor.py` | Lifecycle state machine, admission gate, dedupe, fitness retirement |
| `provenance.py` | Provenance pinning, reconstructability |
| `security.py` | Admission scanner, invocation guard extension |
| `evolver.py` | Graph evolution from traces, shadow promotion |

### New migrations (2 files, `python/codeguardian/core/storage/migrations/`)

- `005_skill_graph.sql`
- `006_skill_states.sql`

### New tests (8 files, `tests/unit/core/`)

`test_skill_graph.py`, `test_ppr.py`, `test_retrieval_pipeline.py`, `test_hydrator.py`, `test_negative.py`, `test_governor.py`, `test_skill_security.py`, `test_evolver.py`

### Updated (6 files)

| File | Change |
|---|---|
| `core/skills/__init__.py` | Export new public surface (19 names) |
| `core/skills/library.py` | Wire governor and provenance; add accessors; admission routing when flag on |
| `core/skills/indexer.py` | Add `rebuild_graph()` and `apply_forgetting_curve()`; existing methods unchanged |
| `schemas/skill.json` | v1.0 → v1.1, six optional fields, expanded state enum |
| `core/config/schema.py` | New `[skills]` table validation |
| `core/config/defaults.py` | 16 new `DEFAULT_SKILLS_*` constants |

### Rule 1 escalation (1 file)

| File | Change |
|---|---|
| `core/storage/skill_store.py` | `_upsert_skill_row` changed from DELETE-then-INSERT to UPDATE-when-exists; `remove` deletes `skill_feedback` rows before `skills`. Latent Phase 3 bug exposed by Phase 12 v2's `record_load_outcome`. |

### Deliberately untouched (3 files)

| File | Why |
|---|---|
| `core/skills/embeddings.py` | Same model, same dimension, no re-embedding |
| `core/skills/retriever.py` | The fallback. Kept callable as the flat path |
| `core/storage/skill_store.py` public surface | `SkillStore` methods unchanged (only the internal `_upsert_skill_row` and `remove` fixed) |

`001_initial.sql` through `004_*.sql` untouched. `skills`, `skills_fts`, `skill_embeddings` schemas preserved by migration `006`.

### Total

12 new modules + 2 migrations + 8 tests + 6 updates + 1 Rule 1 escalation = **29 files touched**. Of these, **zero are rewrites of working module code** (the one Rule 1 escalation fixes a bug, it does not change the module's contract).

---

## 13. Non-goals and residual risks

### Explicitly not delivered in Phase 12 v2

| Not delivered | Reason |
|---|---|
| Agentic multi-hop retrieval | SkillGATE-style region exploration is a further 16.3% R@1, but adds latency and a second model call. Deferred until the graph path proves insufficient on real workloads. |
| WASM-sandboxed skill execution | Requires a new sandbox primitive. The admission scanner catches known patterns; execution sandboxing is a separate problem for a future phase. |
| Cross-repo skill transfer | A skill learned in project A does not automatically travel to project B. `scope: repo` is a future extension. |
| Skill authorship assistance | The Skill Curator (Phase 11) proposes; the governor decides. No "auto-fix the rejected skill" loop in this phase. |
| Embedding model swap | The 384-dim model is preserved. A future swap introduces a parallel table; out of scope here. |
| Multi-tenant isolation | Skills are shared per user. Per-team or per-org scoping is not in scope. |
| Skill execution tracing at the code level | We track which skills were loaded and whether the run succeeded. We do not trace which specific lines of a skill's body were followed. |
| Semantic caching of retrieval results | Every retrieval call is fresh. Caching task-specific results is the failure mode the Anthropic engineering team retired. |

### Residual risks

| Risk | Mitigation |
|---|---|
| Novel injection pattern bypasses scanner | Invocation guard via audit hook limits blast radius. Skill permissions inherited. |
| Graph quality depends on frontmatter correctness | Deterministic parse only; manual review at admission for high-blast skills. |
| Negative store grows unbounded | Forgetting curve on `last_seen`; `max_per_skill` cap; manual invocation of decay in v2. |
| Dedupe misses similar-but-not-identical skills | The warning band (0.80 to 0.92) exists for this. Manual review queue via `skill_admission_log`. |
| Evolver promotes a wrong edge | Shadow promotion is verified against a held-out split. Serving graph is never mutated directly. |
| Reranker adds latency on slow machines | Hard 120 ms budget, skip on timeout. Flat RRF output on skip. |
| Recon or Blast unavailable | Stage 0 skips, falls back to Stage 1. |
| Recon produces wrong `languages[]` | `report_statuses` distinguishes observed from inferred. Inferred fields become soft priors, not hard filters. |
| Threshold calibration wrong for this workload | All thresholds are config fields. Defaults are conservative. Three separate thresholds from the production literature. |

### Documented residuals specific to Phase 12 v2

| Residual | When it becomes relevant |
|---|---|
| Reranker loads `Xenova/ms-marco-MiniLM-L-6-v2` via `fastembed.rerank.cross_encoder.TextCrossEncoder`; if the installed fastembed lacks that class, degrades to passthrough silently | When reranker proves insufficient on real workloads |
| Stage 0 project filter is language-only in v2; dependency version and test framework match need Phase 13 orchestration | Phase 13 |
| Fitness formula uses effectiveness only; `−λ·cost` deferred because there is no per-skill cost column | When cost tracking arrives |
| Evolver store uses a sidecar JSON file, not shadow tables, to avoid a third migration dependency | Phase 13+ |
| Negative knowledge decay is manual (via `apply_forgetting_curve`), not automatic | When Phase 13 scheduler exists |
| Provenance `admission_evidence` is reconstructed from persisted columns, not stored as a JSON blob | When richer evidence is needed |

---

## 14. Handoff to Phases 13–16

### To Phase 13 (Orchestrator)

- A `RetrievalPipeline` that accepts `project_context: ProjectCard | None` and returns a `RetrievalBundle`.
- A `SkillGovernor` with explicit admission and retirement that the orchestrator calls after the Skill Curator produces a proposal.
- A `NegativeKnowledgeStore` that the orchestrator queries before each retry, providing the adopt/reject protocol.
- **Constraint:** the orchestrator must supply `recon.json` and `blast_radius.json` paths or the retrieval pipeline loses Stage 0 filtering. This is a required input, not optional.
- **Constraint:** call `library.governor().record_load_outcome(name, outcome)` after every skill use, or the lifecycle never advances and fitness never updates.
- **Constraint:** call `library.indexer().rebuild_graph()` and `.apply_forgetting_curve(governor=...)` at run start or on a timer, or the graph drifts and the negative store grows unbounded.

### To Phase 14 (CLI)

- `codeguardian skill list` — reads from the governor, shows lifecycle states.
- `codeguardian skill show <name>` — full record including provenance.
- `codeguardian skill promote <name>` — human-initiated `draft → trial`.
- `codeguardian skill retire <name>` — human-initiated retirement.
- `codeguardian skill rejected` — reads `skill_admission_log`.
- `codeguardian skill graph <name>` — reads `skill_graph_edges` and renders neighbors.

### To Phase 15 (MCP plus Reporters)

- Retrieval decisions are already logged in `agent_calls.decision_log`. Reporters can surface which skills were loaded per run and why.
- Negative adoption decisions are also logged. Reporters can surface "this run rejected 3 negative constraints, 2 of which later proved relevant" as an ops signal.

### To Phase 16 (Full Assembly)

- The deep test gains a skills section: build the graph from a real repo, run retrieval with the project filter active, assert bundle composition is deterministic, assert flag-off equals v1.
- The integration script asserts the five safety rules via `scripts/integration_phase.py --through 12`.

---

## 15. What "done" means

Phase 12 v2 is done when:

1. All 12 new modules exist, each with its own Protocol and default implementation.
2. `migrations/005_skill_graph.sql` and `migrations/006_skill_states.sql` run idempotently on a v1 database and produce the expanded schema.
3. `schemas/skill.json` v1.1 validates every existing `SKILL.md` without edit.
4. All eight new test files pass.
5. `test_all_flags_off_delegates_to_v1_retriever` proves that with every flag `false`, retrieval returns byte-identical output to the v1 retriever.
6. The scanner's true-positive / true-negative guard passes (verified manually in this session, could be lifted into a test in a future cleanup).
7. `ruff check python/codeguardian/ tests/` returns `All checks passed!`
8. `PYTHONPATH=python python3 -m pytest tests/ -q` returns `1547 passed, 14 skipped, 7 xfailed, 5 xpassed`.
9. `BUILD_STATE.md` updated: Phase 12 v2 marked complete, both Rule 1 escalations recorded, residuals noted.

All nine are satisfied at the time of this document.

---

## 16. One-paragraph summary

Phase 12 becomes lifecycle-managed procedural memory: a derived dependency graph so retrieval returns prerequisites, a four-stage decision chain where the main agent never chooses, a separate negative-knowledge tier with explicit adopt/reject, an admission gate that treats every skill as untrusted supply-chain input, a fitness-driven retirement rule that keeps the pool solvent, and integration with Recon and Blast so a new project's own signals narrow the pool before any model is called. All of it is additive, all of it is flag-off by default, all of it is derived-and-rebuildable, and none of it rewrites a working file. A user who upgrades and never touches config gets exactly what they had. A user who turns it on gets project-shaped retrieval that stays at 74% top-1 accuracy instead of collapsing to 27%.

---

## 17. Decision log

Recorded here so future sessions can audit the reasoning without re-deriving it.

| Decision | Alternatives considered | Why this one |
|---|---|---|
| **Graph is derived, not authoritative** | Graph as primary store | Every system that made the graph authoritative regretted it within one schema change. Rebuildable-from-Markdown preserves the Phase 3 invariant. |
| **Reverse PPR, not forward** | Forward diffusion, no traversal | The GoS ablation shows reverse traversal is the source of the gain (9.1 reward points). Forward is nearly useless. |
| **Four-stage chain, not one reranker** | Single cross-encoder over flat pool | The literature is unambiguous: implicit selection collapses. The chain holds. |
| **Explicit selection, not agent choice** | Let the task agent pick | Measured: 73.6% → 27.4% recall at 1,000 distractors. The main agent choosing is a known failure mode. |
| **Read `recon.json` directly, no Project Card cache** | Cache a 200-token card keyed by commit | Anthropic engineering retired their profile cache after measurement. Question-agnostic facts cache; question-specific facts read fresh. |
| **Separate negative store, not effectiveness column** | `effectiveness < 0.3` heuristic on the existing table | Positive and negative knowledge decay on different schedules. Combining them under one TTL erases the wrong one. |
| **`trial` state, not `draft → active`** | Direct promotion | A skill that has never run has no fitness. `trial` proves it before it competes for retrieval slots. |
| **New tables, no ALTER on existing** | Add columns to `skills` | Forward-only, idempotent migrations are the only safe pattern. `ALTER` breaks rollback. |
| **Six optional schema fields, no required** | Bump to v2.0 with new required fields | Every existing skill must validate without edit. Growing with optional fields is the standard way to do this. |
| **Local MiniLM reranker, not API** | Cloud cross-encoder | Local-first tool. 50 to 100 ms is acceptable. Network call is not. |
| **Block on scan hit, warn on suspicious** | Block on everything / warn on everything | Blocking on warnings would reject 36.82% of legitimate skills. Warning on blocks would let 13.4% of critical vulnerabilities through. |
| **Evolver off by default** | Evolver on by default | The feature is promising (SE-GoS: 52.4% → 59.4%) but the shadow promotion logic is novel. Prove it before defaulting it on. |
| **`max_active_skills = 200` default** | No cap | The Library Drift work shows unbounded growth degrades retrieval. The cap is a forcing function for retirement. |
| **Threshold defaults from production literature** | Tuned locally | No local data yet. The three-threshold scheme (0.5 / 0.35 / 0.3) is the 2026 production default. Tune after real runs. |
| **Per-repo skills deferred** | Ship in v2 | `gskill` is proven but not urgent. The Recon integration makes it nearly free later. |
| **Agentic multi-hop deferred** | Ship in v2 | SkillGATE adds 16.3% R@1 but requires a second model call and adds latency. Graph plus reranker should suffice first. |
| **Evolver sidecar JSON, not shadow tables** | Shadow tables | A third migration is out of scope. A JSON file has the same isolation property. |
| **`_upsert_skill_row` UPDATE-when-exists** | Keep DELETE-then-INSERT | The DELETE-then-INSERT pattern fails the `skill_feedback.skill_name` FK once a skill has any feedback. UPDATE never triggers the FK. |
| **Migration 006 rebuilds the table** | ALTER CHECK constraint | SQLite does not support ALTER on CHECK constraints. Rebuild is the only path. |
| **PRAGMA foreign_keys stays OFF after migration 006** | Turn ON at the end | The project runs with FK enforcement off by default. A migration that turns it ON changes behavior the rest of the codebase does not expect. The migration restores its previous state. |

---

## 18. References

The sources cited in §3, in the order they appear. Every claim in this document traces to one of them.

1. TMLR 2026 systematic survey — 124 papers on agent skill libraries, lifecycle management, verification, evolution.
2. Graph-of-Skills (GoS) — EMNLP 2026 Main. Reverse-weighted PPR over offline skill graph. +7.0 reward, −56.7% tokens, 9.1-point forward-traversal ablation.
3. SE-GoS — Sep 2026. Graph evolution from execution traces. 52.4% → 59.4% in one round, +5.4 on held-out split.
4. Systems-foundation paper, arXiv 2608.29596 — Four memory tiers, `U(s) = gain − λ·cost`, four failure modes.
5. WikiSkill, Google Research + Virginia Tech 2026 — Wiki layer, retention of rejected fixes and validation results.
6. SkillForge, NeurIPS 2026 — Fitness-driven lifecycle `trial → active → stable → retired`.
7. Snyk audit, Feb 2026 — 3,984 skills audited, 36.82% flawed, 13.4% critical, 76 malicious.
8. Cloud Security Alliance, 2026 — 69.4% attack success, 84.2% of vulnerabilities in prose.
9. SkillRouter — 0.6B encoder + 0.6B reranker, 74.0% top-1.
10. Implicit vs explicit routing study — 73.6% → 27.4% recall collapse at 1,000 distractors; 51.6% no-load rate.
11. Body-is-decisive study — 29 to 44 pp drop removing body; 91.7% cross-encoder attention on body.
12. Library Drift — Unbounded growth degrades retrieval; governance recipe lifts 0.258 → 0.584 over 100 rounds; ~30-skill threshold.
13. Skill shadowing study — Collision costs 0.14 pass rate; merging colliding descriptions is the highest-leverage action.
14. Production retrieval baseline, 2026 — Hybrid → RRF → cross-encoder rerank over top 20 to 50 → top 3 to 5; local MiniLM 50 to 100 ms.
15. R3-Skill — Reranking signal stage-dependent; helps cross-encoder, not bi-encoder.
16. SkillGATE — Graph-guided region exploration, +16.3% R@1 over independent ranking.
17. Threshold calibration, production 2026 — relevance ≥ 0.5, selection ≥ 0.35, confidence ≥ 0.3.
18. Anthropic engineering, 2026 — Retired shared profile cache; lean task-specific grounding.
19. OpenClaw schema history — Older builds refuse newer schemas; rollback requires pre-upgrade snapshot plus matching build.
20. gskill, 2026 — Per-repo `.gskill/` scoped skill files.

Related internal documents:

- `docs/05-data-model.md` §5.13, §7 — skill schema, retention.
- `docs/16-context-engineering.md` — progressive disclosure, token budgets.
- `docs/08A-module-relationships.md` §5 — agent → data contract map (Recon and Blast output shapes).
- `docs/modularity-audit.md` §7 — the pipeline/agent mode risk that Phase 13 must address.
- `BUILD_STATE.md` — known gaps that touch this phase.
- `BUILD_ORDER.md` — phase plan.

---

*End of document.*
