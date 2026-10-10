# Demo walkthrough

About ten minutes. Shows the success path, automatic retry, dead-lettering, diagnosis, replay, and
that a read-only user cannot perform a restricted action. Everything runs on your machine: no cloud
account, no billing, no model API key.

## Before you start

Needs `uv`, Node 22+ and PostgreSQL 15+ binaries on your `PATH` (`brew install postgresql@16` on
macOS). Docker is not required.

```bash
make setup     # installs dependencies, creates .env with generated secrets, starts a
               # project-local PostgreSQL on port 54329, migrates, seeds the demo workspace
make dev       # API :8000 · worker :8001 · UI http://localhost:5173
```

`make setup` prints the three demo accounts. Their passwords are the `RELAY_SEED_*_PASSWORD` values
in `.env` (generated, never printed):

```bash
grep SEED_.*PASSWORD .env
```

Sign in at http://localhost:5173 as **operator@relay.local**.

## 1. A successful delivery

1. **Settings → Demo tools.** Under *2*, leave the destination on **Healthy**.
2. Under *1*, choose **Valid order** and press **Send**. The table shows `202 · Accepted`.
3. Press **Open**. The event is **Delivered**.

What to look at:

- **Timeline:** Received → Pending publish → Queued → Processing → Attempt 1 succeeded (HTTP 201)
  → Delivered.
- **Payloads:** the vendor's shape on the left (`order_no`, `items[].qty` as a string, a padded
  name, an upper-case email) and the canonical order on the right (`order_id`, `lines[].quantity`
  as an integer, trimmed name, lower-case email, UTC timestamp).
- The card number in the source payload reads `[REDACTED]`.
- **Settings → Demo tools → 3** shows the destination stored that order once, keyed by the event id.

## 2. Duplicates and bad input

- Send **Duplicate**: two rows, `Accepted` then `Duplicate`. One event exists.
- Send **Malformed**: `422`. Open it — *Validation failed*, with the two problems located
  (`order_no` missing; `items[0].qty` has the wrong type). **No delivery attempts.**
- Send **Invalid signature**: `401`, and nothing is stored.

## 3. Automatic retry

1. Set the destination to **Flaky** (fails the first two attempts per order).
2. Send a **Valid order** and open it.
3. Watch the timeline: attempt 1 fails (HTTP 503) → *Retry scheduled* → attempt 2 fails → attempt 3
   succeeds → **Delivered**. Every attempt carried the same idempotency key.

## 4. Dead-letter, investigate, replay

1. Set the destination to **Error 500**.
2. Send a **Valid order** and open it. After four attempts with growing delays (about ten seconds)
   it is **Dead-lettered** — *attempts exhausted*.
3. It appears under **Failures → Dead-lettered** with its reason.
4. On the event, press **Investigate**. With no model configured you get a **rule-based diagnosis**:
   likely cause *Destination unavailable*, the attempt outcomes as observed facts with evidence
   chips, and recommendations that each need a human decision.
5. **Approve** the replay recommendation. Note the message: approval is a record only. Go back to
   the event — it is still dead-lettered. Nothing was executed.
6. Set the destination back to **Healthy**.
7. On the event press **Replay…**, give a reason, and confirm.
8. It becomes **Delivered**. The four failed attempts are still listed; the new one is
   *Attempt 1 (replay cycle 2)*. **Audit log** shows who replayed it and why.

## 5. A lost response does not create a duplicate

1. Set the destination to **Slow success**: it stores the order, then stalls past the 2-second
   delivery timeout.
2. Send a **Valid order**. Attempt 1 **times out** — Relay cannot know whether it worked.
3. Attempt 2 returns HTTP 200 `duplicate_ignored`: the destination recognised the idempotency key.
4. **Demo tools → 3** shows *requests seen: 2, stored: once*.

This is the honest version of the delivery guarantee: at-least-once, made safe by idempotency.

## 6. Roles are enforced by the API

1. Sign out; sign in as **viewer@relay.local**.
2. Open the dead-lettered or validation-failed event: there is no Replay or Investigate button.
3. Now bypass the UI. In the browser console:

   ```js
   const me = await (await fetch("/api/v1/auth/me")).json();
   const r = await fetch(`/api/v1/events/${location.pathname.split("/").pop()}/replay`, {
     method: "POST",
     headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf_token, "Idempotency-Key": "viewer-try-0001" },
     body: JSON.stringify({ reason: "trying anyway" }),
   });
   console.log(r.status, await r.json());   // 403 permission_denied
   ```

## 7. Change configuration safely

Sign in as **admin@relay.local** → **Integrations** → the demo integration → **Configuration**.

- **Test with a sample → Run test** shows the output the destination would receive. Nothing is sent.
- Break a mapping's source path (`order_no..oops`) and press **Validate**: the error names
  `mappings[0].source`.
- Fix it, add a change note, **Save as new version**. **Version history** now lists v2 and the
  untouched v1. Events already accepted keep the version they arrived under.

## 8. As an external system would send it

No session, only the HMAC signature:

```bash
uv run --env-file .env python scripts/simulate.py --connection <source-connection-id> valid_order
```

(`make seed` prints the connection id in its *Webhook path* line.)

## Optional: with a local model

```bash
ollama pull llama3.1        # or any tool-capable model
# in .env:
RELAY_AI_PROVIDERS=ollama
RELAY_AI_OLLAMA_MODEL=llama3.1
```

Restart `make dev`. *Investigate* now reports *AI-assisted*, lists the tools it called, and falls
back to the rule-based diagnosis if the model is unavailable or produces an invalid report. This
path was built and tested against scripted model responses; it has not been run against a live
Ollama server on the machine this project was developed on.

## Reset

```bash
make db-reset && make migrate seed     # wipes local demo data only
```
