---
name: ai-tool-review
description: Review HomeSpace AI tool integrations, structured responses, external API contracts, geolocation logic and data boundaries. Use when AI tools or integration behavior require explicit review.
---

# ai-tool-review (Claude Code adapter for hs-ai-service)

This is an adapter. It does not contain the workflow itself. The source workflow is the Codex skill at:

```text
../../../.agents/skills/ai-tool-review/SKILL.md
```

(relative to this directory; it resolves to `.agents/skills/ai-tool-review/SKILL.md` in the `hs-ai-service` repository root).

Claude Code does not import that file automatically. When this skill is triggered:

1. Read the source file above with the Read tool.
2. Follow its workflow exactly, together with the `AGENTS.md` rules of this repository.
3. The source skill has no `references/` or `scripts/` directories. If they are added later, read them from the source skill directory when the workflow requires them.
