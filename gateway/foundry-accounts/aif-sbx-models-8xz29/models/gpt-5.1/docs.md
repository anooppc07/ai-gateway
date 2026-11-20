# gpt-5.1

Judge model for evaluations on the platform. Not for agents: it scores other models' and agents' answers.

| | |
|---|---|
| Gateway model name | `gpt-5.1` |
| Used by | Platform evaluation (B against A) and project evaluations, through the setting `EVAL_JUDGE_MODEL` |
| API | Chat Completions |
| Version | 2025-11-13, pinned (no automatic upgrade) |
| Content safety | Microsoft default guardrail on the deployment |
