# Repository AI skills

Clone this repository and open its Git root in Orca. Start a fresh Codex or Claude Code session there. Both CLIs discover the committed project skills on startup. Sign in to the CLI if prompted on a new machine. Ponytail is an optional personal plugin.

In Codex, select `ai-tool-review` with `/skills` or type `$ai-tool-review`. In Claude Code, use `/ai-tool-review`. For example, ask the agent to review the current diff.

> Review timeout handling and structured tool responses in the changed integration.

## Maintaining skills

Codex reads `.agents/skills/<name>/SKILL.md`. Claude Code reads `.claude/skills/<name>/SKILL.md`. Edit these files directly in this repository. Each version contains the full workflow. When behavior should change in both agents, update both versions in the same PR. Keep supporting files in this repository.

The root `AGENTS.md` contains the engineering rules that `CLAUDE.md` imports for Claude Code.

To review a skill update, run `git diff --check`. Clone the review branch into a fresh directory, launch both CLIs at its Git root and check that each lists the expected skill. For changes to a workflow, invoke that skill with a read-only example.
