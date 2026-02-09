# Design: User-Scoped Change Groups

## Problem Statement

We want signed-in users to be able to start a **change group** with a required note. While the change group is active, every edit that user makes as they navigate the site should be associated with that group. Other users editing concurrently must not have their changes accidentally added to someone else's group.

## How sqlite-history-json Change Groups Work Today

The `_history_json` table (the "groups table") has this schema:

```sql
CREATE TABLE [_history_json] (
    id integer primary key,
    note text,
    current integer
);
CREATE UNIQUE INDEX [_history_json_current]
    ON [_history_json] (current) WHERE current = 1;
```

Every audit trigger contains this subquery to associate changes with a group:

```sql
(SELECT id FROM [_history_json] WHERE current = 1)
```

The `change_group()` context manager works by:

1. Setting `current = 1` on a new group row
2. Yielding (all trigger-logged changes pick up that group id)
3. Clearing `current = 1` in a `finally` block

This is designed for single-process batch scripts where one actor has exclusive access to the database. It is **not safe for multi-user concurrent access** because `current = 1` is a global signal visible to all triggers regardless of who initiated the write.

## The Concurrency Challenge

Datasette uses a **single writer thread per database**. All calls to `db.execute_write_fn(fn)` are serialized through one queue processed by one background thread. Each `fn(conn)` callback gets exclusive access to the write connection for its duration.

This means:

- Two write functions cannot interleave within a single database
- But between two *separate* `execute_write_fn` calls, other writes can be queued

So we **cannot** do this:

```python
# UNSAFE: another write could be queued between steps 1 and 2
await db.execute_write_fn(lambda conn: set_current(conn, group_id))  # step 1
await db.execute_write_fn(actual_write_fn)                           # step 2
await db.execute_write_fn(lambda conn: clear_current(conn))          # step 3
```

But we **can** safely do this:

```python
# SAFE: all three steps happen in a single write function
async def wrapped(conn):
    conn.execute("UPDATE _history_json SET current = 1 WHERE id = ?", [group_id])
    try:
        result = actual_write_fn(conn)
    finally:
        conn.execute("UPDATE _history_json SET current = null WHERE current = 1")
    return result

await db.execute_write_fn(wrapped)
```

Because `wrapped` runs as a single unit on the writer thread, no other write can see the `current = 1` state.

## Proposed Plugin Design

### User Flow

1. User signs in (standard Datasette authentication)
2. User clicks "Start change group" and provides a required note
3. A banner appears indicating the active change group
4. User navigates and edits data normally — all edits are tagged with the group
5. User clicks "End change group" to stop

### Data Model

**No schema changes to `_history_json`** — we use the existing table as-is.

The group row is created with `current = NULL` (not `1`). The group id is stored in a **signed cookie** scoped to the user:

```
Cookie: ds_change_group = <signed({"<database_name>": <group_id>})>
```

Using Datasette's `sign()`/`unsign()` with a dedicated `"change-group"` namespace prevents tampering.

### New Endpoints

```
POST /-/history-json/<db>/-/change-group/start
  Body: note=<required description>&csrftoken=<token>
  → Creates group row in _history_json (current=NULL)
  → Sets signed cookie with the group_id
  → Redirects back (or returns JSON)

POST /-/history-json/<db>/-/change-group/end
  Body: csrftoken=<token>
  → Clears the cookie for that database
  → Redirects back (or returns JSON)

GET /-/history-json/<db>/-/change-group
  → Returns current active group info for the signed-in user (from cookie)
```

### Core Technique: Write Wrapping

The key mechanism is intercepting every `execute_write_fn` call and, when the originating request has an active change group, wrapping the write function to activate/deactivate `current = 1` within that single callback.

```python
import contextvars

# Set per-request by ASGI middleware, read when wrapping writes
_active_change_groups = contextvars.ContextVar(
    "active_change_groups", default=None
)

# Captures the contextvar value in a closure before sending to writer thread
def _wrap_write_fn(fn, group_id):
    def wrapped(conn):
        conn.execute(
            "UPDATE _history_json SET current = 1 WHERE id = ?",
            [group_id],
        )
        try:
            return fn(conn)
        finally:
            conn.execute(
                "UPDATE _history_json SET current = null WHERE current = 1",
            )
    return wrapped
```

**Why this is safe:** The closure captures `group_id` by value at creation time (on the async request-handling coroutine). By the time `wrapped` runs on the writer thread, the value is already bound. Each request gets its own closure with its own captured `group_id`. The writer thread processes these closures one at a time, so no two wrapped functions overlap.

### ASGI Middleware (via `asgi_wrapper` hook)

```python
@hookimpl
def asgi_wrapper(datasette):
    def wrapper(app):
        async def middleware(scope, receive, send):
            if scope["type"] == "http":
                # Parse cookies from raw headers
                cookie_header = dict(scope.get("headers", [])).get(b"cookie", b"")
                # Extract and unsign ds_change_group cookie
                # Set _active_change_groups contextvar
                ...
            await app(scope, receive, send)
        return middleware
    return wrapper
```

### Patching `execute_write_fn` (via `startup` hook)

```python
@hookimpl
def startup(datasette):
    original = Database.execute_write_fn

    async def patched(self, fn, block=True, transaction=True):
        groups = _active_change_groups.get()
        if groups and self.name in groups:
            group_id = groups[self.name]
            fn = _wrap_write_fn(fn, group_id)
        return await original(self, fn, block=block, transaction=transaction)

    Database.execute_write_fn = patched
```

This is the part of the design that works but is fragile — monkey-patching a core Datasette method. See "Proposed Changes to Datasette" below for how Datasette could provide a clean hook for this instead.

### UI Integration

- **Banner**: When a change group is active, display a banner on every page showing the group note and a button to end it. This can be done via the `extra_body_script` hook or a template override.
- **Start/End UI**: Add actions to the database actions menu (via `database_actions` hook) for starting a change group. Show the "end" action when one is active.

### Security Considerations

- **Signed cookie**: The group_id is tamper-proof (Datasette's itsdangerous signing).
- **Actor validation**: On every request, verify the cookie's group_id actually belongs to a group that exists and was created by the current actor. This requires adding an `actor_id` column to `_history_json` (see below).
- **CSRF protection**: Start/end endpoints require CSRF tokens.
- **Stale cookies**: If a group_id in the cookie no longer exists, clear the cookie silently.

### Recommended Schema Addition to `_history_json`

To validate that a change group belongs to the requesting actor, the groups table should gain an `actor_id` column:

```sql
ALTER TABLE [_history_json] ADD COLUMN actor_id TEXT;
```

This requires a change to `sqlite-history-json` itself. The `change_group()` function would accept an optional `actor_id` parameter. The plugin would set this when creating groups. This also enables future queries like "show me all change groups by actor X."

---

## Proposed Changes to Datasette

The plugin design above works today but relies on monkey-patching `Database.execute_write_fn`. This is fragile and could break across Datasette versions. The following changes to Datasette would make this (and similar plugins) cleaner to implement.

### 1. `wrap_write` Plugin Hook

**The most impactful change.** A new hook that lets plugins wrap write functions before they execute:

```python
# In datasette/hookspecs.py
@hookspec
def wrap_write(datasette, database, fn, request):
    """Wrap a write function before it executes on the writer thread.

    Return a new callable(conn) that wraps fn(conn), or return None
    to leave it unchanged.

    ``request`` may be None for writes not originating from an HTTP request.
    """
```

Datasette would call this in `execute_write_fn`:

```python
async def execute_write_fn(self, fn, block=True, transaction=True, request=None):
    for wrapper in pm.hook.wrap_write(
        datasette=self.ds, database=self.name, fn=fn, request=request
    ):
        if wrapper is not None:
            fn = wrapper
    # ... existing queue/thread logic
```

**Why this matters:** It gives plugins a first-class way to inject behavior around writes — setting pragmas, activating change groups, logging, enforcing constraints — without monkey-patching.

**The `request` parameter is critical.** Today `execute_write_fn` has no knowledge of which HTTP request triggered the write. Passing this through enables plugins to make per-request decisions (like "does this user have an active change group?").

### 2. Pass `request` Through Write Paths

Currently, Datasette's internal write paths (row insert/update/delete, canned queries) call `execute_write_fn` without any reference to the originating request. To support `wrap_write` with request context, the `request` parameter needs to flow through:

```python
# Current (no request context):
await db.execute_write_fn(_write_fn)

# Proposed:
await db.execute_write_fn(_write_fn, request=request)
```

This change would need to be made in:
- `TableInsertView` / `TableUpsertView` — row insert/upsert endpoints
- `RowUpdateView` / `RowDeleteView` — row update/delete endpoints
- `TableDropView` — table drop endpoint
- Canned query execution
- Any other internal write path

This is a larger change but has value beyond change groups — any plugin that wants to audit, authorize, or modify writes based on the requesting actor would benefit.

### 3. `execute_write_fn` Accepts `request` as Parameter

The method signature change:

```python
# Current:
async def execute_write_fn(self, fn, block=True, transaction=True)

# Proposed:
async def execute_write_fn(self, fn, block=True, transaction=True, request=None)
```

The `request` parameter is optional (defaulting to `None`) for backward compatibility. Plugin-initiated writes that don't have a request context continue to work unchanged.

### 4. Alternative: `contextvars`-Based Request Context

If threading `request` through all write paths is too invasive, a lighter-weight alternative is for Datasette to set a `ContextVar` with the current request before calling route handlers:

```python
# In datasette/app.py
_current_request = contextvars.ContextVar("current_request", default=None)

# In route dispatch:
_current_request.set(request)
response = await view(request, datasette)
```

Plugins could then read `_current_request.get()` anywhere, including in `wrap_write` hooks or ASGI middleware. This avoids modifying `execute_write_fn`'s signature but makes the request implicitly available.

**Tradeoff:** Explicit is better than implicit. The `request` parameter on `execute_write_fn` is more explicit and discoverable. The `ContextVar` approach is easier to ship incrementally.

### 5. `database_actions` Hook

The existing `table_actions` and `row_actions` hooks let plugins add action menu items to table and row pages. A `database_actions` hook (if it doesn't already exist) would let this plugin add "Start change group" / "End change group" to the database-level action menu.

### Summary of Datasette Changes (in priority order)

| Change | Complexity | Impact |
|--------|-----------|--------|
| `wrap_write` plugin hook | Medium | Enables write-wrapping plugins without monkey-patching |
| Pass `request` through write paths | Medium-High | Makes actor context available to write hooks |
| `ContextVar` for current request | Low | Lighter alternative to explicit `request` threading |
| `database_actions` hook | Low | UI integration for database-level plugin actions |

The combination of `wrap_write` + `request` on write paths would eliminate the need for monkey-patching entirely and make the change groups feature a clean, maintainable plugin.

---

## Alternatives Considered

### A. Post-Hoc Audit Row Update

Instead of setting `current = 1` during the write, let the write happen normally (`group = NULL`), then update audit rows after the fact:

```python
def wrapped(conn):
    # Record max audit IDs before write
    max_id = conn.execute("SELECT max(id) FROM [_history_json_items]").fetchone()[0] or 0
    result = fn(conn)
    # Update new audit rows with group
    conn.execute("UPDATE [_history_json_items] SET [group] = ? WHERE id > ?", [group_id, max_id])
    return result
```

**Pros:** Doesn't touch the `current` column; no conflict with direct `change_group()` usage.
**Cons:** Requires knowing all tracked tables; extra queries per write even when no audit entries were created; fragile if tables are added/removed during a session.

### B. User-Defined Function in Triggers

Modify `sqlite-history-json` to use a Python UDF instead of a subquery:

```sql
-- Instead of: (SELECT id FROM _history_json WHERE current = 1)
-- Use:        _history_json_active_group()
```

Register the UDF via `prepare_connection`, have it read from a thread-local variable set by the write wrapper.

**Pros:** Clean separation; no `current` column needed at all.
**Cons:** Requires changes to sqlite-history-json trigger SQL; Python UDFs in triggers have performance implications; the UDF only works with the Python sqlite3 module (not other SQLite clients).

### C. Per-Connection State via Temp Tables

Create a temporary table per-connection to hold the active group:

```sql
CREATE TEMP TABLE _history_json_session (group_id INTEGER);
```

Triggers would query this temp table instead.

**Pros:** Connection-scoped; no global state.
**Cons:** Datasette shares one write connection across all requests, so temp tables are shared too — same problem as `current = 1`. Would only work if Datasette used isolated connections per request.
