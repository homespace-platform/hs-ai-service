# HomeSpace AI skills

Open the product Git root as its own project or terminal in Orca and start a fresh session. A folder containing several independent Git repos is not their common Git root. From a terminal opened at the product root, run `codex` or `claude`. Log in to each CLI on a new machine and review the workspace trust prompt. Project skills and instructions are already in Git. No personal plugin, sync run, Python installation or toolkit access is needed to use them.

In `hs-core-api`, use `/skills` in Codex and select `contract-risk-review`, or type `$contract-risk-review`. In Claude Code, select or type `/contract-risk-review`. Give either agent a task such as `Review the last change to deposit state transitions. Report findings only, using static evidence first.` Open a fresh session after changing skills. To inspect instructions in Claude Code, use `/memory` and look for the repository `CLAUDE.md` and its `AGENTS.md` import.

Skills with explicit-only invocation are selected by the developer. Skills such as `spring-api-change` can also be selected by the agent if a task matches their descriptions. Mandatory engineering rules live in `AGENTS.md`. `CLAUDE.md` imports the same rules. Nested rules in Core API remain in their modules.

## Edit a repository skill

Edit `.agents/skills/<name>/SKILL.md`. Use a matching lowercase kebab-case name, a one-line YAML description and a complete workflow body. For explicit-only selection, put `policy:` and `  allow_implicit_invocation: false` in `.agents/skills/<name>/agents/openai.yaml`. The script translates the policy into Claude front matter. The lock lists the local and shared skills. For a new local skill, add its name to `local` in `.agents/skills.lock.json`.

Use `py -3` on Windows, or substitute `python3` on Linux. Sync uses Python 3.10+ and Git without additional packages.

```powershell
py -3 scripts/ai-skills.py --sync --dry-run
py -3 scripts/ai-skills.py --sync
py -3 scripts/ai-skills.py --check
```

Commit the canonical changes, lock changes and generated copies together. `--check` runs offline in GitHub Actions. The script skips unchanged bytes and preserves LF or CRLF in existing files. It only updates registered, recognizable outputs. Review collisions or obsolete generated files in Git before removing or replacing them.

## Upgrade shared content

Use a reviewed commit or version tag from `homespace-platform/hs-ai-toolkit`. Only maintainers doing the upgrade need access to that repo. From the product Git root with a sibling toolkit checkout, run the following on a feature branch.

```powershell
git -C ../hs-ai-toolkit fetch --tags
git -C ../hs-ai-toolkit switch --detach <reviewed-tag-or-commit>
py -3 scripts/ai-skills.py --sync --toolkit ../hs-ai-toolkit --dry-run
py -3 scripts/ai-skills.py --sync --toolkit ../hs-ai-toolkit
py -3 scripts/ai-skills.py --check
git diff --check
```

Review the Git diff and open a PR with the pinned commit, changes and verification. `--toolkit` reads Git objects from the checkout's `HEAD`. Uncommitted toolkit changes have no effect. If an installed shared file was edited outside the sync process, review the diff and restore the installed version before upgrading.
