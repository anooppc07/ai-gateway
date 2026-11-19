# gpt-5-mini

General-purpose model for agents: question answering, tool calling and structured output. Model A of the test bed:
the model projects use until a newer model passes evaluation.

| | |
|---|---|
| Gateway model name | `gpt-5-mini` |
| Use in an agent | `<connection>/gpt-5-mini` (the project's own connection name) |
| API | Chat Completions |
| Version | 2025-08-07, pinned (no automatic upgrade) |
| Content safety | Microsoft default guardrail on the deployment |

Request access by adding it to `models.allowed` in your project's `project.yaml`, with the tokens per minute and
per day you need. The platform team approves the pull request.
