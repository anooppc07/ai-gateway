# gpt-5.4-mini

Fast, low-cost general-purpose model for agents: question answering, tool calling and structured output. Model B of
the test bed: the candidate that replaces gpt-5-mini once it passes platform and project evaluation.

| | |
|---|---|
| Gateway model name | `gpt-5.4-mini` |
| Use in an agent | `<connection>/gpt-5.4-mini` (the project's own connection name) |
| API | Chat Completions |
| Version | 2026-03-17, pinned (no automatic upgrade) |
| Content safety | Microsoft default guardrail on the deployment |

Request access by adding it to `models.allowed` in your project's `project.yaml`, after the platform has approved it
and your project's own evaluation has passed.
