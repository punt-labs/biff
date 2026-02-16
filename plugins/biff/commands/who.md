---
description: List active team members and what they're working on
---

Call `mcp__biff__who` with no arguments. The result is a pipe-separated list of entries in the format `@user +/- HH:MM plan` where `+` means accepting messages and `-` means messages off.

Display the results as a table with columns: NAME, S, TIME, PLAN. Example:

```
NAME        S  TIME   PLAN
@kai        +  14:19  refactoring auth
@eric       -  11:18  reviewing PRs
```

Do not send any other text besides the tool call and the formatted table.
