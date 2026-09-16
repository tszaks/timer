---
name: timer-agent
description: Schedule durable deferred work with the Timer CLI when an agent must continue in a later turn or survive a session timeout.
---

# Timer Agent Recipe

Use Timer as a durable continuation queue, not as `sleep`.

Use one delivery recipe: `start --key`, `claim`, then `ack`.

1. Choose a stable `--key`, `--consumer`, namespace, owner, and route reference. Use `session:<id>` or `thread:<id>` when the supervisor must inject a Codex turn. Use `task:<id>` only when the payload also contains a routable `route_ref`.
2. Start with the explicit agent form:

   ```sh
   timer start 10m --key deploy-check --namespace agent:codex-123 --owner codex \
     --message "Check the deploy and intervene only on failure." \
     --ref thread:THREAD_ID \
     --payload '{"goal":"Check deploy","done_when":"success or failed","on_tick":"if pending, stop","artifacts":["logs/deploy.txt"],"budget":{"ticks_left":12}}' \
     --json
   ```

3. Do not call `timer wait` unless the host guarantees that the tool process can remain alive. Do not use the terse human form (`timer 10m rice`) for agent work.
4. When a continuation turn arrives, or when polling is required, claim one event:

   ```sh
   timer claim --consumer codex-123 --lease 60s --namespace agent:codex-123 --event expired --json
   ```

5. Act from `delivery.message`, `delivery.ref`, and `delivery.payload`. After the work reaches its stated stopping condition, acknowledge that exact event:

   ```sh
   timer ack EVENT_ID --consumer codex-123 --lease-id LEASE_ID --json
   ```

6. If work cannot begin safely, use `timer nack EVENT_ID --consumer codex-123 --lease-id LEASE_ID --json`. The `lease_id` comes from the claim response and prevents a stale worker from acknowledging a newer claim. If the process crashes, let the lease expire so the same event becomes claimable again.

Never use `drain` for agent work. It acknowledges each event before caller work begins. Each delivery path must keep a stable consumer name; sessions that intentionally compete for the same work share that name.

Before relying on automatic wake delivery, verify `timer service status --json` reports `event: "delivering"`; `running=true` alone is insufficient. Install it with `timer setup` when needed. Setup checks that `codex queue --thread` accepts session identifiers, validates a synthetic expiry, and starts the user service.
