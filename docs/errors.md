# AgentKit error codes

Every failed command prints `error.code` (stable, never renamed or reused) and `error.run` (the run id).
Tasks show the code in `task status` (`code`, `at` = component#step, `log`) and in `task report` (`error`).

Find where it failed: `agentkit logs <run-or-task-id> --errors` · attach a redacted archive: `agentkit debug-bundle <id>` ·
draft an issue (never posted automatically): `agentkit feedback --bug <id>`.

| Code | Cause | Fix |
|---|---|---|
| `AK-USAGE` | bad command line arguments | agentkit <command> --help |
| `AK-CONFIG` | unreadable .agentkit/config.toml or bad config value | fix the TOML; agentkit doctor |
| `AK-PATH` | path missing, outside the repo, a directory, or protected | check the path with agentkit ls / glob |
| `AK-READ-BINARY` | file is binary | binary files are not read; pick a text file |
| `AK-READ-SECRET` | refused to read a secret file (.env, ~/.ssh ...) | do not read secrets; use --allow-secret-file only if the human asks |
| `AK-READ-SPEC` | bad read spec or range | use file, file:START-END or file#Symbol |
| `AK-SEARCH-REGEX` | invalid regular expression | fix the regex or pass --fixed |
| `AK-TOOL-EDIT-NOMATCH` | old text not found (0 matches) | read the file again; candidates are in data.candidates |
| `AK-TOOL-EDIT-MULTI` | old text matches more than once | add surrounding lines to make it unique, or --all |
| `AK-TOOL-EDIT-SYNTAX` | the edit would introduce a syntax error | fix the new text at the reported line |
| `AK-TOOL-EDIT-STALE` | file changed since it was last read | read the file again, then edit |
| `AK-TOOL-EDIT-OP` | bad edit/patch operation (exists, not found, bad JSON, unknown op) | check the op list and paths |
| `AK-TOOL-WRITE` | writing files failed; everything was restored | check disk space / permissions |
| `AK-RUN-DENIED` | command refused by a permission rule (M-22) | ask the human, or allow it in .agentkit/config.toml [run] |
| `AK-CHECKPOINT` | unknown checkpoint or checkpoint failure | agentkit checkpoint list |
| `AK-MEMORY` | unknown memory id, category or anchor | agentkit memory list |
| `AK-VERIFY-NOCHECK` | verify/task has no check command | pass --check "<test command>" |
| `AK-VERIFY-FAIL` | checks ran and failed | read the failing check's why/log; fix the code |
| `AK-VERIFY-INVALID` | a check command is missing or broken | fix the check command itself, not the code |
| `AK-VERIFY-SUSPICIOUS` | checks passed but suspicious changes were found | inspect the diff on the task branch before merging |
| `AK-MODEL-NONE` | no worker model configured (direct mode) | use the tools directly, or agentkit model add |
| `AK-MODEL-KEYENV` | the model's key environment variable is not set | export the variable named by key_env |
| `AK-MODEL-AUTH` | model rejected the key (401/403) | agentkit model test NAME; check the key |
| `AK-MODEL-429` | model rate limit / quota exhausted (429) | wait for Retry-After or the quota reset, then re-submit |
| `AK-MODEL-TIMEOUT` | model did not answer after 2 retries | check the endpoint / network; re-submit later |
| `AK-MODEL-NETWORK` | cannot reach the model endpoint | check base_url and network; agentkit model test |
| `AK-MODEL-HTTP` | model endpoint returned an HTTP error (e.g. 400/404 bad model id) | check the model id and base_url |
| `AK-MODEL-BAD-RESPONSE` | model endpoint did not return an OpenAI chat completion | check that base_url is OpenAI-compatible |
| `AK-WORKER-MALFORMED` | worker produced 3 malformed tool calls in a row | try a stronger model tier |
| `AK-WORKER-STUCK` | worker made no progress (no new diff / same error repeated) | narrow the goal or use a stronger model |
| `AK-WORKER-ROUNDS` | worker hit the max rounds limit | raise [worker] max_rounds or split the task |
| `AK-WORKER-CRASH` | the worker loop raised an unexpected exception | agentkit logs ID --errors; agentkit debug-bundle ID |
| `AK-BUDGET` | task stopped: token / USD / time budget exceeded | raise --budget-* or simplify the task |
| `AK-TASK-CANCELLED` | task was cancelled | re-submit with agentkit task create if needed |
| `AK-TASK-INTERRUPTED` | worker process died (crash / kill / reboot) | agentkit task report ID; re-submit to retry |
| `AK-TASK-NOGIT` | dispatch needs a git repo with at least one commit | git init && git add -A && git commit -m init |
| `AK-TASK-UNKNOWN` | unknown or invalid task id | agentkit task status |
| `AK-TASK-STATE` | operation not allowed in the task's current state | agentkit task status ID |
| `AK-GIT` | a git command failed | agentkit doctor |
| `AK-DISK-FULL` | not enough free disk space | agentkit task clean --all |
| `AK-LOGS-UNKNOWN` | no events for that run / task id | ids are in error.run, task ids in task status |
| `AK-MODEL-UNAVAILABLE` | model endpoint overloaded / server error (500/502/503) after 2 retries | re-submit later or use another model |
| `AK-INTERNAL` | bug in agentkit (uncaught exception) | agentkit debug-bundle RUN and attach it to an issue |

Event log line fields (JSONL, redacted): `ts, run_id, step, component, event, duration, error_code` + event-specific extras.
Commands log to `.agentkit/logs/events.jsonl` (rotated at 5 MB); each task logs to `.agentkit/logs/events-<task id>.jsonl`.
Uncaught exceptions: the traceback goes to `.agentkit/logs/crash-<run id>.log`; the JSON output only carries `AK-INTERNAL` + run id.
