-- Evidence columns for agent_calls. Adds the four fields that let a
-- future coding agent reconstruct not only what an agent decided but
-- how it decided: which tools it called, which candidates it rejected,
-- what the model reasoned, and what evidence supported a verdict.
--
-- All four columns are nullable. Agents that have nothing to record
-- pass NULL and the row is still written. The evidence columns enrich
-- a row; they do not gate it.
--
-- tool_chain is a JSON array of tool invocations in the order they
-- occurred: [{"tool": str, "input": object, "output_bytes": int}, ...].
-- decision_log is a JSON array of decision events in the order they
-- occurred: [{"step": str, "candidate": int | null, "verdict": str,
-- "reason": str | null}, ...]. model_reasoning is the model's
-- reasoning trace as plain text, when the provider returns one.
-- evidence_receipt is a JSON object summarising what a verifier
-- checked: {"evidence_ids": [str], "artifacts_checked": [str],
-- "commands_run": [str]}.
--
-- None of these columns is CHECK-constrained. The valid shapes live
-- in codeguardian.core.agents.telemetry as TypedDicts. New keys can
-- be added without a migration.

ALTER TABLE agent_calls ADD COLUMN tool_chain TEXT;
ALTER TABLE agent_calls ADD COLUMN decision_log TEXT;
ALTER TABLE agent_calls ADD COLUMN model_reasoning TEXT;
ALTER TABLE agent_calls ADD COLUMN evidence_receipt TEXT;
