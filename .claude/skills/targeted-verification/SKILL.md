---
name: targeted-verification
description: Select and run focused Python verification for HomeSpace AI Service. Use when testing or verification of AI service changes is explicitly requested.
disable-model-invocation: true
---

# AI Service Targeted Verification

## Procedure

1. Identify changed Python modules.
2. Identify affected behavior and available tests.
3. Classify change risk.
4. For edits without behavior impact, inspect the change once. For behavior changes, reuse applicable passing results for unchanged inputs, then run the smallest remaining pytest selection after editing.
5. Check affected interfaces and schemas.
6. Report actual results.

## Rules

- Prefer existing focused tests.
- Use project-defined tooling from pyproject.toml.
- Do not run all tests by default.
- Do not install dependencies unnecessarily.
- Avoid real external API calls in unit tests.
- On failure, diagnose whether it is a setup or code issue. Rerun only the affected command after correcting the cause.
- Expand verification for high-risk integrations.

## Completion

Report passed, failed, blocked and unverified checks.
