---
name: ai-tool-review
description: Review HomeSpace AI tool integrations, structured responses, external API contracts, geolocation logic and data boundaries. Use when AI tools or integration behavior require explicit review.
---

# AI Tool Review

## Mode

Review-only unless fixes are requested.

## Workflow

1. Identify the affected tool or integration.
2. Inspect input validation and output contracts.
3. Check authorization and data boundaries.
4. Check failure, timeout and retry behavior.
5. Review response parsing and field assumptions.
6. Check location, distance and filtering correctness where relevant.
7. Inspect existing tests.
8. Report actionable findings.

## Efficiency

- Use mocks or fixtures when appropriate.
- Do not call paid external APIs merely for review.
- Do not automatically start infrastructure.
- Do not run unrelated tests.
- Do not fabricate API behavior.

## Output

Provide findings ordered by severity, with supporting evidence and recommended fixes.
