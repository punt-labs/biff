---
description: Set the background polling interval
argument-hint: "{N}s | {N}m | n"
allowed-tools: ["ToolSearch", "mcp__plugin_biff-dev_tty__set_poll_interval", "mcp__plugin_biff-dev_tty__get_poll_status", "CronCreate", "CronList", "CronDelete"]
---
<!-- markdownlint-disable MD041 -->

## Input

Arguments: $ARGUMENTS

If no argument provided, call `mcp__plugin_biff-dev_tty__get_poll_status` and report the result. Stop.

Otherwise parse the argument as an interval (`{N}s` or `{N}m`, e.g. `2s`, `30s`, `5m`) or `n` (disable). Sub-minute intervals set the server-side tick rate; the durable cron fires at 1-minute minimum (cron granularity limit).

## Task

1. Call `mcp__plugin_biff-dev_tty__set_poll_interval` with `interval` set to the parsed value.
2. If the interval is `n` (disable):
   - Call `CronList` and find any job whose `prompt` field exactly matches `/biff-dev:read-dev`.
   - If found, call `CronDelete` with that job's `id` to remove the polling cron.
3. If the interval is NOT `n`:
   - Call `CronList` and check if any job has a `prompt` field that exactly matches `/biff-dev:read-dev`.
   - If an existing `/biff-dev:read-dev` cron exists, call `CronDelete` on it first.
   - Call `CronCreate` with:
     - `cron`: convert the interval to a cron expression (e.g. `2s` -> `*/1 * * * *`, `5s` -> `*/1 * * * *`, `1m` -> `*/1 * * * *`, `2m` -> `*/2 * * * *`, `5m` -> `*/5 * * * *`). For intervals under 1 minute, use `*/1 * * * *` (cron minimum is 1 minute).
     - `prompt`: `/biff-dev:read-dev`
     - `recurring`: true
     - `durable`: true
4. Do not mention the cron setup or send any other text to the user beyond the tool result.
