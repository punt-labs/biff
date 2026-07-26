# Troubleshooting

## First Steps

Run `biff doctor` to diagnose common issues:

```bash
biff doctor
```

This checks: Python version, uv installation, Claude Code CLI, MCP server registration, plugin files, status bar configuration, and `.biff` team file.

## Common Issues

### `/who` says "No sessions"

**Cause:** Biff is not enabled in the current repo.

**Fix:** Run `/biff enable` (or `biff enable` from the CLI). This writes the committed `.punt-labs/biff/enabled` marker — commit it so biff is on for everyone who clones the repo.

### Commands not available after install

**Cause:** Claude Code needs two restarts to fully activate a new plugin.

**Fix:** Restart Claude Code twice. The first restart loads the plugin, the second activates slash commands.

### Status bar not showing

**Cause:** The status bar setup was skipped or overwritten.

**Fix:**

```bash
biff install-statusline
```

If you had a custom status line before biff, verify it was stashed correctly:

```bash
cat ~/.punt-labs/biff/statusline-original.json
```

### Messages not arriving

**Possible causes:**

1. **Recipient is dormant.** Biff is not enabled in their repo (`/biff enable`).
2. **Recipient is in do-not-disturb.** Check `/who` --- `S` column shows `-` for DND. Messages still accumulate; they'll see them when they `/read` or `/mesg y`.
3. **NATS connection issue.** The relay may be unreachable. Check:

   ```bash
   biff doctor
   ```

4. **Different relay.** Both sides must use the same NATS relay URL in their `.punt-labs/biff/config.yaml`.

### "Talk requires a NATS relay connection"

**Cause:** `/talk` only works with the NATS relay, not in local-only mode.

**Fix:** Ensure your `.punt-labs/biff/config.yaml` has a `relay` section with a valid URL, and that biff is enabled (`/biff enable`).

### Status bar shows `biff` instead of username

**Cause:** The MCP server hasn't connected to the relay yet (dormant mode), or the unread file hasn't been written.

**Fix:** Enable biff (`/biff enable`) and run any command (`/who`) to trigger the first connection. The status bar updates within 2 seconds.

### Git hooks not firing

**Cause:** `biff install` deploys hooks to this clone's `.git/hooks/` (they are per-clone, never committed). If you cloned an already-enabled repo but never ran `biff install`, the hooks are absent. If the hooks directory doesn't exist or permissions are wrong, hooks won't fire either.

**Fix:**

```bash
biff install    # Redeploy this clone's hooks
ls -la .git/hooks/post-checkout .git/hooks/post-commit .git/hooks/pre-push
```

Hooks gate on the committed `.punt-labs/biff/enabled` marker (and `biff` being on `PATH`) --- they are silent when biff is not enabled in the repo.

### Identity shows wrong username

**Cause:** Biff resolves identity from `gh auth`.

**Fix:**

```bash
gh auth status    # Verify your GitHub username
gh auth login     # Re-authenticate if needed
```

Your biff username matches your GitHub username. There is no separate biff identity to configure.

### High idle time / nap mode

**Cause:** After about 2 minutes with no tool calls, biff enters nap mode — it reduces polling frequency from 2s to 30s but keeps the NATS connection alive. Wall changes and talk messages are still delivered in real-time via KV watches and NATS subscriptions.

**Not a bug.** Any tool call (`/who`, `/write`, `/read`) wakes biff immediately. The poller transitions back to active mode (2-second polling) on the next tool call.

## Getting Help

- **GitHub Issues:** [github.com/punt-labs/biff/issues](https://github.com/punt-labs/biff/issues)
- **Source code:** [github.com/punt-labs/biff](https://github.com/punt-labs/biff)
