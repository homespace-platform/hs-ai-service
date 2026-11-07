---
name: targeted-verification
description: Select and run focused Python verification for HomeSpace AI Service. Use when testing or verification of AI service changes is explicitly requested.
---

# AI Service Targeted Verification

## Procedure

1. Identify changed Python modules.
2. Identify affected behavior and available tests.
3. Classify change risk.
4. Run the smallest relevant pytest selection.
5. Check affected interfaces and schemas.
6. Report actual results.

## Rules

- Prefer existing focused tests.
- Use project-defined tooling from pyproject.toml.
- Do not run all tests by default.
- Do not install dependencies unnecessarily.
- Avoid real external API calls in unit tests.
- Do not rerun unchanged failing commands.
- Expand verification for high-risk integrations.

## Completion

Report passed, failed, blocked and unverified checks.
