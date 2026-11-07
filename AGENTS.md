# HomeSpace AI Service — Codex Instructions

## Scope

Python AI service.

Inspect pyproject.toml and existing documentation for actual framework, dependencies and tooling.

## Architecture

- Preserve existing application boundaries.
- Follow current service, API and data-access patterns.
- Preserve structured response contracts.
- Preserve tool-calling input and output schemas.
- Do not fabricate external API fields.
- Do not change database models without checking migration requirements.

## AI Safety

- Respect user and tenant data boundaries.
- Validate tool inputs.
- Do not expose secrets in logs.
- Do not make real external AI calls in ordinary unit tests.
- Do not silently alter prompt behavior.
- Preserve search filters and constraints across interactions.

## Verification

- Prefer specific existing pytest files or test functions.
- Inspect actual project tooling before running commands.
- Do not run the entire test suite for every small change.
- Do not download models or install dependencies unnecessarily.
- Do not start databases unless integration verification requires them.
- Expand testing for tool schemas, security and persistence changes.

## Skills

- ai-tool-review: Explicit review of AI tool integrations.
- targeted-verification: Explicit focused validation.

## Completion

Report changed behavior, tests executed and unverified external dependencies.
