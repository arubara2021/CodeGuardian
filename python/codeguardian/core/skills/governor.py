"""Admission gate, lifecycle transitions, and fitness retirement.

This module owns the decision of what is allowed into the retrieval
pool and what leaves it. The decisions it makes are:

1. Admission. A candidate skill is scanned for known attack patterns,
   checked against the active pool for near-duplicates, checked against
   the active cap, and (when an eval score is supplied) checked against
   the eval threshold. A candidate that fails any check is rejected;
   the rejection and its findings are logged to ``skill_admission_log``.

2. Lifecycle. A skill moves through the states
   ``draft -> trial -> active -> stable -> retired``. The two v1 state
   names ``stale`` and ``archived`` are treated as aliases for
   ``retired`` so that the upgrade preserves the meaning of any
   existing row.

3. Fitness retirement. A skill whose effectiveness has fallen below
   the configured threshold after a configured number of uses is
   retired automatically. Retirement removes the skill from the
   retrieval pool; the file remains on disk for inspection and
   possible revival.

Fitness is measured as the skill's ``effectiveness`` field, which the
underlying store recomputes as ``total_successes / total_uses``. The
published fitness function subtracts a lambda-weighted cost term.
That term is not implemented in this phase because the metadata schema
has no per-skill token-cost column and adding one would require a new
migration. The lifecycle thresholds are therefore on the gain side
only. When a cost column arrives, the formula in this module changes
in one place.

The governor never raises on a query. Every admission returns a
verdict; every retirement returns a bool or a list. Write failures on
the admission log are caught and logged because an audit-trail write
must not block the caller's decision.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from codeguardian.core.config.schema import SkillsConfig
from codeguardian.core.storage.sqlite_store import SQLiteStore

from .security import scan
from .types import AdmissionVerdict

if TYPE_CHECKING:
    from .library import SkillLibrary

__all__ = ["SkillGovernor"]

logger = logging.getLogger(__name__)


# The state transition map. A transition from ``draft`` to anything
# other than ``trial`` or ``retired`` is refused; the same rule applies
# at every state. Adding a state requires updating this map and the
# skills schema in the same change.
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"trial", "retired"}),
    "trial": frozenset({"active", "retired"}),
    "active": frozenset({"stable", "retired"}),
    "stable": frozenset({"retired"}),
    "retired": frozenset(),
}

# The v1 states that this phase folds into retired. The mapping is
# applied on every state read so a row written by a previous version
# of the code is treated as if it had been written by this version.
_STATE_ALIASES: dict[str, str] = {
    "stale": "retired",
    "archived": "retired",
}

# The eval threshold applied to a supplied score. Values below this
# reject the candidate. The number follows the design document and is
# overridable through the caller, who supplies the score.
_EVAL_THRESHOLD: float = 0.60

# The dedupe warning band. A candidate whose similarity to an existing
# skill lands between the warning floor and the duplicate threshold is
# admitted with a warning rather than rejected. Below the warning floor
# the candidate is admitted as unique.
_DEDUPE_WARNING_FLOOR: float = 0.80


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _tokenize(text: str) -> set[str]:
    """Return the set of lowercase alphanumeric tokens in the text."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _normalize_state(state: object) -> str:
    """Normalize a persisted state name to the current vocabulary."""
    raw = str(state) if state is not None else "active"
    return _STATE_ALIASES.get(raw, raw)


class SkillGovernor:
    """Admission gate and lifecycle manager.

    Args:
        store: An open ``SQLiteStore``.
        library: The skill library, used for reading and updating
            skill state.
        config: The skills configuration. All thresholds and caps come
            from here.
    """

    def __init__(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        config: SkillsConfig,
    ) -> None:
        self._store = store
        self._library = library
        self._config = config

    def admit(
        self,
        skill_name: str,
        frontmatter: dict,
        body: str,
        *,
        eval_score: float | None = None,
    ) -> AdmissionVerdict:
        """Run the admission gate for a candidate skill.

        The checks run in order and short-circuit at the first rejection:
        security scan, strict warning promotion, eval score, dedupe,
        active cap.

        Args:
            skill_name: The name of the skill being admitted.
            frontmatter: The parsed frontmatter dictionary.
            body: The Markdown body.
            eval_score: An optional eval score in the range 0 to 1.
                When supplied and below the threshold, the candidate is
                rejected. When ``None``, the eval check is skipped.

        Returns:
            An ``AdmissionVerdict`` describing the outcome. Every call
            writes one row to ``skill_admission_log``.
        """
        scan_text = f"{json.dumps(frontmatter, sort_keys=True)}\n---\n{body}"
        scan_verdict = scan(scan_text)

        if not scan_verdict.accepted:
            return self._record_verdict(
                skill_name,
                AdmissionVerdict(
                    accepted=False,
                    reason=f"security scan blocked: {scan_verdict.reason}",
                    blocking_findings=scan_verdict.blocking_findings,
                    warning_findings=scan_verdict.warning_findings,
                    scan_result=scan_verdict.scan_result,
                ),
            )

        if self._config.admission_strict and scan_verdict.warning_findings:
            return self._record_verdict(
                skill_name,
                AdmissionVerdict(
                    accepted=False,
                    reason=(f"strict admission: {len(scan_verdict.warning_findings)} warning(s)"),
                    warning_findings=scan_verdict.warning_findings,
                    scan_result=scan_verdict.scan_result,
                ),
            )

        if eval_score is not None and eval_score < _EVAL_THRESHOLD:
            return self._record_verdict(
                skill_name,
                AdmissionVerdict(
                    accepted=False,
                    reason=f"eval score {eval_score:.2f} below {_EVAL_THRESHOLD:.2f}",
                    warning_findings=scan_verdict.warning_findings,
                    scan_result=scan_verdict.scan_result,
                    eval_score=eval_score,
                ),
            )

        dedupe_status, near_duplicate = self._dedupe(
            skill_name, str(frontmatter.get("description", ""))
        )
        if dedupe_status == "duplicate":
            return self._record_verdict(
                skill_name,
                AdmissionVerdict(
                    accepted=False,
                    reason=f"duplicate of {near_duplicate!r}",
                    warning_findings=scan_verdict.warning_findings,
                    scan_result=scan_verdict.scan_result,
                    dedupe_status="duplicate",
                    near_duplicate_of=near_duplicate,
                    eval_score=eval_score,
                ),
            )

        active_count = self._count_active()
        if active_count >= self._config.max_active_skills:
            return self._record_verdict(
                skill_name,
                AdmissionVerdict(
                    accepted=False,
                    reason=f"active cap {self._config.max_active_skills} reached",
                    warning_findings=scan_verdict.warning_findings,
                    scan_result=scan_verdict.scan_result,
                    dedupe_status=dedupe_status,
                    near_duplicate_of=near_duplicate,
                    eval_score=eval_score,
                ),
            )

        return self._record_verdict(
            skill_name,
            AdmissionVerdict(
                accepted=True,
                reason="passed",
                warning_findings=scan_verdict.warning_findings,
                scan_result=scan_verdict.scan_result,
                dedupe_status=dedupe_status,
                near_duplicate_of=near_duplicate,
                eval_score=eval_score,
            ),
        )

    def transition(self, skill_name: str, target_state: str) -> bool:
        """Move a skill to a new lifecycle state if the transition is legal.

        Args:
            skill_name: The skill to move.
            target_state: The desired state.

        Returns:
            True when the skill exists and the transition was applied,
            False when the skill does not exist or the transition is
            not permitted from its current state.
        """
        record = self._library.get_skill(skill_name)
        if record is None:
            return False
        current_state = _normalize_state(record["frontmatter"].get("state"))
        if target_state not in _VALID_TRANSITIONS.get(current_state, frozenset()):
            return False
        return self._library.set_skill_state(skill_name, target_state)

    def retire(self, skill_name: str, reason: str = "") -> bool:
        """Retire a skill regardless of fitness.

        Args:
            skill_name: The skill to retire.
            reason: Free-text reason, currently unused. Reserved for a
                future audit-trail entry.

        Returns:
            True when the skill was retired, False when it was already
            retired or does not exist.
        """
        del reason
        return self.transition(skill_name, "retired")

    def record_load_outcome(
        self,
        skill_name: str,
        outcome: str,
        *,
        task_description: str | None = None,
    ) -> None:
        """Record one use of a skill and apply automatic transitions.

        The outcome is recorded through the library's existing feedback
        path, which keeps ``total_uses``, ``total_successes``, and
        ``effectiveness`` in sync inside the ``skills`` table. The
        transition check reads those counters from the database row,
        not from the Markdown frontmatter, because the feedback path
        does not rewrite the file. Reading the file here would observe
        the pre-feedback counters and no transition would ever fire.

        Args:
            skill_name: The skill that was used.
            outcome: One of ``"success"``, ``"partial"``, ``"failure"``.
            task_description: Optional free-text description.
        """
        try:
            self._library.record_feedback(
                name=skill_name,
                run_id=None,
                outcome=outcome,
                task_description=task_description,
            )
        except Exception as exc:
            logger.warning("Failed to record feedback for %s: %s", skill_name, exc)
            return

        row = self._get_db_row(skill_name)
        if row is None:
            return
        state = _normalize_state(row.get("state"))
        uses = int(row.get("total_uses", 0))
        effectiveness = float(row.get("effectiveness", 0.5))

        if state == "trial":
            if uses >= self._config.trial_runs_before_active and effectiveness >= 0.5:
                self._library.set_skill_state(skill_name, "active")
            elif (
                uses >= self._config.trial_runs_before_active
                and effectiveness < self._config.retire_threshold
            ):
                self._library.set_skill_state(skill_name, "retired")
        elif state == "active":
            if uses >= self._config.trial_runs_before_active * 3 and effectiveness >= 0.7:
                self._library.set_skill_state(skill_name, "stable")
            elif (
                uses >= self._config.retire_after_runs
                and effectiveness < self._config.retire_threshold
            ):
                self._library.set_skill_state(skill_name, "retired")
        elif state == "stable" and (
            uses >= self._config.retire_after_runs and effectiveness < self._config.retire_threshold
        ):
            self._library.set_skill_state(skill_name, "retired")

    def apply_retirement(self) -> list[str]:
        """Retire every active and stable skill below the fitness floor.

        The rule is: a skill with at least ``retire_after_runs`` uses
        whose effectiveness has fallen below ``retire_threshold`` is
        retired. A skill with fewer uses than the run threshold is not
        eligible; the floor exists so that one bad run does not retire
        a skill that has not been tried often enough.

        Returns:
            The names of the skills retired on this call.
        """
        retired: list[str] = []
        for record in self._library.list_skills():
            state = _normalize_state(record.get("state"))
            if state not in ("active", "stable"):
                continue
            uses = int(record.get("total_uses", 0))
            effectiveness = float(record.get("effectiveness", 0.5))
            if (
                uses >= self._config.retire_after_runs
                and effectiveness < self._config.retire_threshold
            ) and self._library.set_skill_state(record["name"], "retired"):
                retired.append(record["name"])
        return retired

    def get_state(self, skill_name: str) -> str | None:
        """Return the normalized state of a skill, or ``None``."""
        record = self._library.get_skill(skill_name)
        if record is None:
            return None
        return _normalize_state(record["frontmatter"].get("state"))

    def _dedupe(
        self,
        skill_name: str,
        description: str,
    ) -> tuple[str, str | None]:
        """Return the dedupe status and the closest existing skill.

        Similarity is Jaccard over tokenized descriptions. The
        threshold comes from the config; the warning floor is a fixed
        constant. The check considers only skills in the active, stable,
        or trial states, because a draft is not yet a competing entry
        and a retired skill has left the pool.
        """
        if not description:
            return ("unique", None)
        new_tokens = _tokenize(description)
        if not new_tokens:
            return ("unique", None)

        best_score = 0.0
        best_name: str | None = None
        for record in self._library.list_skills():
            other_name = record["name"]
            if other_name == skill_name:
                continue
            if _normalize_state(record.get("state")) not in (
                "active",
                "stable",
                "trial",
            ):
                continue
            other_tokens = _tokenize(str(record.get("description", "")))
            if not other_tokens:
                continue
            union = new_tokens | other_tokens
            if not union:
                continue
            score = len(new_tokens & other_tokens) / len(union)
            if score > best_score:
                best_score = score
                best_name = other_name

        if best_score >= self._config.dedupe_threshold:
            return ("duplicate", best_name)
        if best_score >= _DEDUPE_WARNING_FLOOR:
            return ("near_duplicate", best_name)
        return ("unique", None)

    def _count_active(self) -> int:
        """Return the number of skills in the active or stable state."""
        return sum(
            1
            for record in self._library.list_skills()
            if _normalize_state(record.get("state")) in ("active", "stable")
        )

    def _get_db_row(self, skill_name: str) -> dict[str, Any] | None:
        """Return the ``skills`` table row for a skill, or ``None``.

        The governor reads lifecycle counters (``total_uses``,
        ``effectiveness``, ``state``) from this row rather than from the
        Markdown frontmatter, because ``record_feedback`` updates the
        row and not the file. Reading the file here would see stale
        counters and the transition logic would never fire.
        """
        rows = self._store.query(
            "SELECT state, total_uses, total_successes, effectiveness FROM skills WHERE name = ?",
            (skill_name,),
        )
        return dict(rows[0]) if rows else None

    def _record_verdict(
        self,
        skill_name: str,
        verdict: AdmissionVerdict,
    ) -> AdmissionVerdict:
        """Append one row to the admission log and return the verdict."""
        try:
            self._store.execute(
                """
                INSERT INTO skill_admission_log (
                    admission_id, skill_name, attempted_at, accepted,
                    reason, scan_result, eval_score, dedupe_status,
                    blocking_findings_json, warning_findings_json,
                    reviewer, reviewer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    skill_name,
                    _now_iso(),
                    1 if verdict.accepted else 0,
                    verdict.reason,
                    verdict.scan_result,
                    verdict.eval_score,
                    verdict.dedupe_status,
                    json.dumps(list(verdict.blocking_findings)),
                    json.dumps(list(verdict.warning_findings)),
                    None,
                    None,
                ),
            )
        except Exception as exc:
            logger.warning("Failed to write admission log for %s: %s", skill_name, exc)
        return verdict
