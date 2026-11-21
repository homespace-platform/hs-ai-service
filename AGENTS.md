# HomeSpace AI Service — Codex Instructions

## Engineering standards

- Preserve business invariants, public contracts and backward compatibility. Keep authentication, authorization, input validation and error handling intact.
- Protect secrets and sensitive data in code, commits, tests, logs and reports. Treat third-party hooks as untrusted until reviewed and explicitly authorized.
- Change production configuration, deploy or run destructive data operations only when explicitly authorized for the target environment.
- Keep review tasks read-only unless the developer requests fixes.
- Run targeted verification first. Broaden coverage when the risk or affected interfaces warrant it. Report checks that ran and any unverified behavior.

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
