"""Runs the (up to) four-stage Claude Agent SDK pipeline against a cloned repo.

Single pass per stage, no retries (see DESIGN.md / TODO.md). Any stage can
end the whole job by responding with a "SKIP: <reason>" final message --
checked uniformly across all four stages rather than being special-cased
per stage.

Tool scoping is the actual safety mechanism here: the planner and plan
reviewer only ever get read-only tools, so they cannot touch repo files no
matter what a prompt says -- the SKIP-on-request behavior in the system
text is a courtesy, not the enforcement boundary.

`PipelineContext.difficulty` gates which of the four stages actually run
(see `_DIFFICULTY_STAGE_FLAGS`); implementer always runs. An unrecognized
difficulty falls back to "difficult" (today's full pipeline) rather than
failing the job -- validation should have already rejected anything else
before a worker ever sees it (see `github_client.validate_issue_body`),
this is just belt-and-suspenders on the conservative side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

from guppy.common.config import TurnBudgets
from guppy.common.store import Store
from guppy.worker.prompts import (
    PipelineContext,
    code_reviewer_prompt,
    code_reviewer_prompt_no_plan,
    plan_reviewer_prompt,
    planner_prompt,
    implementer_prompt,
    implementer_prompt_no_plan,
)

READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
FULL_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]

# (run_planner, run_plan_reviewer, run_code_reviewer) -- implementer always runs.
_DIFFICULTY_STAGE_FLAGS: dict[str, tuple[bool, bool, bool]] = {
    "trivial": (False, False, False),
    "easy": (False, False, True),
    "medium": (True, False, True),
    "difficult": (True, True, True),
}

LogFn = Callable[[str], None]


@dataclass
class StageResult:
    completed: bool  # False if the stage errored or hit its turn limit
    final_text: str
    hit_turn_limit: bool = False
    cost_usd: float | None = None
    tool_calls: list[str] = field(default_factory=list)


async def run_stage(*, repo_dir: Path, prompt: str, allowed_tools: list[str], max_turns: int, log: LogFn) -> StageResult:
    options = ClaudeAgentOptions(
        cwd=str(repo_dir),
        allowed_tools=allowed_tools,
        permission_mode="acceptEdits",
        max_turns=max_turns,
    )

    final_text = ""
    tool_calls: list[str] = []
    hit_turn_limit = False
    completed = False
    cost_usd = None

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    log(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append(block.name)
                    log(f"[tool] {block.name} {block.input}")
        elif isinstance(message, ResultMessage):
            cost_usd = getattr(message, "total_cost_usd", None)
            final_text = message.result or ""
            # `terminal_reason` is the documented, forward-compatible signal
            # for why the query loop ended; `subtype`/`is_error` are kept as
            # a fallback for older CLI versions that don't set it.
            terminal_reason = getattr(message, "terminal_reason", None)
            if terminal_reason == "max_turns" or message.subtype == "error_max_turns":
                hit_turn_limit = True
                log(f"[stage hit its turn limit ({max_turns} turns)]")
            elif terminal_reason in (None, "completed") and not message.is_error:
                completed = True
            else:
                log(f"[stage ended abnormally: subtype={message.subtype} terminal_reason={terminal_reason}]")

    return StageResult(
        completed=completed,
        final_text=final_text,
        hit_turn_limit=hit_turn_limit,
        cost_usd=cost_usd,
        tool_calls=tool_calls,
    )


def _check_skip(result: StageResult) -> str | None:
    text = result.final_text.strip()
    if text.upper().startswith("SKIP"):
        _, _, reason = text.partition(":")
        return reason.strip() or "the agent determined this issue could not be safely handled"
    return None


@dataclass
class PipelineOutcome:
    skipped: bool
    skip_reason: str | None
    error: str | None
    plan_path: Path | None
    plan_reviewed_path: Path | None
    log_path: Path
    implementer_summary: str | None = None


async def run_pipeline(
    ctx: PipelineContext,
    *,
    repo_dir: Path,
    artifacts_dir: Path,
    turn_budgets: TurnBudgets,
    store: Store,
    job_id: str,
) -> PipelineOutcome:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    log_path = artifacts_dir / "pipeline.log"
    log_lines: list[str] = []

    def log(text: str) -> None:
        log_lines.append(text)
        log_path.write_text("\n".join(log_lines))

    store.set_artifact(job_id, "log_path", str(log_path))

    def outcome(**overrides) -> PipelineOutcome:
        base = dict(
            skipped=False,
            skip_reason=None,
            error=None,
            plan_path=None,
            plan_reviewed_path=None,
            log_path=log_path,
            implementer_summary=None,
        )
        base.update(overrides)
        return PipelineOutcome(**base)

    run_planner, run_plan_reviewer, run_code_reviewer = _DIFFICULTY_STAGE_FLAGS.get(
        ctx.difficulty, _DIFFICULTY_STAGE_FLAGS["difficult"]
    )

    plan: str | None = None
    plan_path: Path | None = None
    plan_reviewed_path: Path | None = None

    # --- planner ---------------------------------------------------------
    if run_planner:
        store.set_stage(job_id, "planner")
        log("=== planner ===")
        planner_result = await run_stage(
            repo_dir=repo_dir,
            prompt=planner_prompt(ctx),
            allowed_tools=READ_ONLY_TOOLS,
            max_turns=turn_budgets.planner,
            log=log,
        )
        if (reason := _check_skip(planner_result)) is not None:
            return outcome(skipped=True, skip_reason=reason)
        if not planner_result.completed:
            return outcome(error="planner stage did not complete (hit its turn limit or errored)")

        plan = planner_result.final_text
        plan_path = artifacts_dir / "plan.md"
        plan_path.write_text(plan)
        store.set_artifact(job_id, "plan_artifact_path", str(plan_path))

    # --- plan reviewer -----------------------------------------------------
    if run_plan_reviewer:
        store.set_stage(job_id, "plan_reviewer")
        log("=== plan reviewer ===")
        review_result = await run_stage(
            repo_dir=repo_dir,
            prompt=plan_reviewer_prompt(ctx, plan),
            allowed_tools=READ_ONLY_TOOLS,
            max_turns=turn_budgets.plan_reviewer,
            log=log,
        )
        if (reason := _check_skip(review_result)) is not None:
            return outcome(skipped=True, skip_reason=reason, plan_path=plan_path)
        if not review_result.completed:
            return outcome(
                error="plan-review stage did not complete (hit its turn limit or errored)",
                plan_path=plan_path,
            )

        plan = review_result.final_text
        plan_reviewed_path = artifacts_dir / "plan_reviewed.md"
        plan_reviewed_path.write_text(plan)
        store.set_artifact(job_id, "plan_review_artifact_path", str(plan_reviewed_path))

    # --- implementer -----------------------------------------------------
    # implementer always runs, but low-difficulty jobs that skipped the
    # planner get the no-plan prompt variant (see prompts.py docstring)
    # instead of a placeholder plan.
    store.set_stage(job_id, "implementer")
    log("=== implementer ===")
    impl_prompt = implementer_prompt(ctx, plan) if plan is not None else implementer_prompt_no_plan(ctx)
    impl_result = await run_stage(
        repo_dir=repo_dir,
        prompt=impl_prompt,
        allowed_tools=FULL_TOOLS,
        max_turns=turn_budgets.implementer,
        log=log,
    )
    if (reason := _check_skip(impl_result)) is not None:
        return outcome(skipped=True, skip_reason=reason, plan_path=plan_path, plan_reviewed_path=plan_reviewed_path)
    if not impl_result.completed:
        return outcome(
            error="implementer stage did not complete (hit its turn limit or errored)",
            plan_path=plan_path,
            plan_reviewed_path=plan_reviewed_path,
        )

    # --- code reviewer -----------------------------------------------------
    if run_code_reviewer:
        store.set_stage(job_id, "code_reviewer")
        log("=== code reviewer ===")
        cr_prompt = (
            code_reviewer_prompt(ctx, plan, impl_result.final_text)
            if plan is not None
            else code_reviewer_prompt_no_plan(ctx, impl_result.final_text)
        )
        code_review_result = await run_stage(
            repo_dir=repo_dir,
            prompt=cr_prompt,
            allowed_tools=FULL_TOOLS,
            max_turns=turn_budgets.code_reviewer,
            log=log,
        )
        if (reason := _check_skip(code_review_result)) is not None:
            return outcome(
                skipped=True, skip_reason=reason, plan_path=plan_path, plan_reviewed_path=plan_reviewed_path
            )
        if not code_review_result.completed:
            return outcome(
                error="code-review stage did not complete (hit its turn limit or errored)",
                plan_path=plan_path,
                plan_reviewed_path=plan_reviewed_path,
            )

    return outcome(
        plan_path=plan_path,
        plan_reviewed_path=plan_reviewed_path,
        implementer_summary=impl_result.final_text,
    )
