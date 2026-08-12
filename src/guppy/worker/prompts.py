"""Prompt construction for the four pipeline stages.

Every stage shares the same SKIP escape hatch and no-comments/no-changelog
constraints (carried over from v1). Test generation is unconditional: the
implementer is always instructed to write tests, whether or not the issue's
optional "## Tests" section is filled in -- if it is, those scenarios are
surfaced as required coverage in addition to whatever else the agent
decides to test.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PipelineContext:
    issue_number: int
    issue_title: str
    issue_body: str
    issue_type: str
    tests_section: str | None
    context_file_content: str | None


_COMMON_CONSTRAINTS = """Constraints:
- Only touch files necessary to address this issue.
- Do not add comments explaining what you changed.
- Do not create migration or changelog files.
- If you determine this cannot be safely handled (the issue is ambiguous, \
contradictory, or unsafe to act on), respond with exactly one line as your \
entire final message: "SKIP: <short reason>", and make no file changes."""


def _issue_context_block(ctx: PipelineContext) -> str:
    parts = [
        f"Issue #{ctx.issue_number}: {ctx.issue_title}",
        f"Type: {ctx.issue_type}",
        "",
        ctx.issue_body,
    ]
    if ctx.context_file_content:
        parts += ["", "--- Repository context (.guppy/context.md) ---", ctx.context_file_content]
    return "\n".join(parts)


def _tests_instruction(ctx: PipelineContext) -> str:
    if ctx.tests_section:
        return (
            "The issue's \"## Tests\" section lists specific scenarios you must "
            f"also cover:\n{ctx.tests_section}"
        )
    return "The issue has no \"## Tests\" section -- decide which scenarios best cover this change."


def planner_prompt(ctx: PipelineContext) -> str:
    return f"""You are the planning stage of an automated coding agent pipeline. \
Read the codebase as needed and produce an implementation plan for the \
following issue. Do not write or edit any files -- only plan.

{_issue_context_block(ctx)}

Your final message must be the complete plan in Markdown: the approach, \
which files will change and how, and how the acceptance criteria will be \
covered by automated tests. {_tests_instruction(ctx)}

{_COMMON_CONSTRAINTS}"""


def plan_reviewer_prompt(ctx: PipelineContext, plan: str) -> str:
    return f"""You are the plan-review stage of an automated coding agent \
pipeline. Critique the plan below against the issue, and directly correct \
it -- don't just list problems. You may read the codebase to verify the \
plan is grounded in the real code, but do not write or edit any files.

{_issue_context_block(ctx)}

--- Proposed plan ---
{plan}
--- end plan ---

Your final message must be the complete, corrected plan in Markdown, ready \
to hand to an implementer. If the plan is already good, return it \
unchanged rather than making busywork edits.

{_COMMON_CONSTRAINTS}"""


def implementer_prompt(ctx: PipelineContext, plan: str) -> str:
    return f"""You are the implementation stage of an automated coding \
agent pipeline. Implement the plan below against the issue.

{_issue_context_block(ctx)}

--- Final plan ---
{plan}
--- end plan ---

Your task:
1. Implement the change per the plan.
2. Write automated tests for your change, using the project's existing \
test framework and conventions, covering the acceptance criteria. \
{_tests_instruction(ctx)}
3. Run the test suite and fix any failures, including in the tests you \
just wrote.

Your final message should be a short summary of what you changed (this is \
for the pipeline's internal logs, not the PR description).

{_COMMON_CONSTRAINTS}"""


def code_reviewer_prompt(ctx: PipelineContext, plan: str, implementer_summary: str) -> str:
    return f"""You are the final code-review stage of an automated coding \
agent pipeline. Review the changes already made in this working directory \
against the plan and the issue, run the test suite, and directly fix \
anything wrong -- don't just report problems.

{_issue_context_block(ctx)}

--- Final plan ---
{plan}
--- end plan ---

--- Implementer's summary of changes made ---
{implementer_summary}
--- end summary ---

Check: does the diff satisfy the acceptance criteria? Do the tests \
actually cover the acceptance criteria (and the "## Tests" section's \
scenarios, if the issue has one)? Do all tests pass? Fix anything that \
doesn't hold up.

Your final message should be a short summary confirming the change is \
ready for human review (or, if you used SKIP, why it isn't).

{_COMMON_CONSTRAINTS}"""
