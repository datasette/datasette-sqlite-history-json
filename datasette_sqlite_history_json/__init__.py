import json

from datasette import hookimpl
from datasette.permissions import Action, PermissionSQL
from datasette.resources import DatabaseResource, TableResource
from datasette.utils import escape_sqlite, tilde_decode, tilde_encode
from datasette.utils.asgi import Forbidden, Response
import sqlite_history_json


def _audit_table_name(table):
    return f"_history_json_{table}"


async def _is_tracked(db, table):
    """Check if table has active tracking (audit table + all 3 triggers)."""
    audit_name = _audit_table_name(table)
    result = await db.execute(
        "select count(*) from sqlite_master where type='table' and name=?",
        [audit_name],
    )
    if result.single_value() == 0:
        return False
    result = await db.execute(
        "select count(*) from sqlite_master where type='trigger' and name in (?, ?, ?)",
        [f"{audit_name}_insert", f"{audit_name}_update", f"{audit_name}_delete"],
    )
    return result.single_value() == 3


async def _has_history(db, table):
    """Check if audit table exists (may have history even if triggers removed)."""
    audit_name = _audit_table_name(table)
    result = await db.execute(
        "select count(*) from sqlite_master where type='table' and name=?",
        [audit_name],
    )
    return result.single_value() > 0


async def _has_groups_table(db):
    """Check if the _history_json groups table exists."""
    result = await db.execute(
        "select count(*) from sqlite_master where type='table' and name='_history_json'"
    )
    return result.single_value() > 0


async def _get_pk_columns(db, table):
    """Return list of PK column names for a table."""
    result = await db.execute(f"PRAGMA table_info([{table}])")
    columns = result.rows
    pks = sorted([c for c in columns if c[5] > 0], key=lambda c: c[5])
    return [c[1] for c in pks]


async def _row_exists(db, table, pk_columns, pk_values):
    """
    Returns True if the row currently exists in the source table.

    If the source table no longer exists (or the query otherwise fails), this
    returns False - history may still exist in the audit table.
    """
    if not pk_columns:
        return False
    where_bits = [f"{escape_sqlite(col)} = ?" for col in pk_columns]
    sql = (
        f"select 1 from {escape_sqlite(table)} where "
        + " and ".join(where_bits)
        + " limit 1"
    )
    try:
        result = await db.execute(sql, [pk_values[col] for col in pk_columns])
    except Exception:
        return False
    return bool(result.rows)


def _parse_pks(pks_string, pk_columns):
    """Parse comma-separated tilde-encoded PK values into a dict."""
    parts = pks_string.split(",")
    if len(parts) != len(pk_columns):
        return None
    result = {}
    for col, val in zip(pk_columns, parts):
        result[col] = tilde_decode(val)
    return result


def _encode_pks(pk_dict, pk_columns):
    """Encode PK values as comma-separated tilde-encoded string."""
    return ",".join(tilde_encode(str(pk_dict[col])) for col in pk_columns)


async def enable_tracking_view(request, datasette):
    database = tilde_decode(request.url_vars["database"])
    table = tilde_decode(request.url_vars["table"])

    try:
        db = datasette.get_database(database)
    except KeyError:
        return Response.text("Database not found", status=404)

    if not await datasette.allowed(
        action="sqlite-history-json",
        actor=request.actor,
        resource=DatabaseResource(database),
    ):
        raise Forbidden("Permission denied")

    if request.method == "GET":
        return Response.html(
            await datasette.render_template(
                "enable_tracking.html",
                {
                    "database": database,
                    "table": table,
                },
                request=request,
            )
        )

    # POST - enable tracking
    def _enable(conn):
        sqlite_history_json.enable_tracking(conn, table)

    await db.execute_write_fn(_enable)
    datasette.add_message(request, f"Tracking enabled for {table}")
    return Response.redirect(datasette.urls.table(database, table))


async def disable_tracking_view(request, datasette):
    database = tilde_decode(request.url_vars["database"])
    table = tilde_decode(request.url_vars["table"])

    try:
        db = datasette.get_database(database)
    except KeyError:
        return Response.text("Database not found", status=404)

    if not await datasette.allowed(
        action="sqlite-history-json",
        actor=request.actor,
        resource=DatabaseResource(database),
    ):
        raise Forbidden("Permission denied")

    if request.method == "GET":
        return Response.html(
            await datasette.render_template(
                "disable_tracking.html",
                {
                    "database": database,
                    "table": table,
                },
                request=request,
            )
        )

    # POST - disable tracking
    def _disable(conn):
        sqlite_history_json.disable_tracking(conn, table)

    await db.execute_write_fn(_disable)
    datasette.add_message(request, f"Tracking disabled for {table}")
    return Response.redirect(datasette.urls.table(database, table))


async def table_history_page(request, datasette):
    database = tilde_decode(request.url_vars["database"])
    table = tilde_decode(request.url_vars["table"])

    try:
        db = datasette.get_database(database)
    except KeyError:
        return Response.text("Database not found", status=404)

    if not await datasette.allowed(
        action="sqlite-history-json-view",
        actor=request.actor,
        resource=TableResource(database, table),
    ):
        raise Forbidden("Permission denied")

    if not await _has_history(db, table):
        return Response.text("No history for this table", status=404)

    is_tracked = await _is_tracked(db, table)

    return Response.html(
        await datasette.render_template(
            "table_history.html",
            {
                "database": database,
                "table": table,
                "is_tracked": is_tracked,
            },
            request=request,
        )
    )


async def table_history_api(request, datasette):
    database = tilde_decode(request.url_vars["database"])
    table = tilde_decode(request.url_vars["table"])

    try:
        db = datasette.get_database(database)
    except KeyError:
        return Response.json({"ok": False, "error": "Database not found"}, status=404)

    if not await datasette.allowed(
        action="sqlite-history-json-view",
        actor=request.actor,
        resource=TableResource(database, table),
    ):
        raise Forbidden("Permission denied")

    if not await _has_history(db, table):
        return Response.json(
            {"ok": False, "error": "No history for this table"}, status=404
        )

    page = int(request.args.get("page", "1"))
    page_size = 50
    operation = request.args.get("operation", None)
    group = request.args.get("group", None)

    audit_name = _audit_table_name(table)
    is_tracked = await _is_tracked(db, table)
    pk_columns = await _get_pk_columns(db, table)
    has_groups = await _has_groups_table(db)

    # Build WHERE conditions
    where_parts = []
    where_params = []
    if operation:
        where_parts.append("a.operation = ?")
        where_params.append(operation)
    if group is not None:
        where_parts.append("a.[group] = ?")
        where_params.append(int(group))
    where_clause = (" where " + " and ".join(where_parts)) if where_parts else ""

    # Count total entries
    count_sql = f"select count(*) from [{audit_name}] a{where_clause}"
    result = await db.execute(count_sql, where_params)
    total_count = result.single_value()

    # Fetch page of entries (join to _history_json for group note if available)
    offset = (page - 1) * page_size
    if has_groups:
        sql = (
            f"select a.*, g.note as group_note from [{audit_name}] a "
            f"left join [_history_json] g on a.[group] = g.id"
            f"{where_clause} order by a.id desc limit ? offset ?"
        )
    else:
        sql = (
            f"select a.* from [{audit_name}] a"
            f"{where_clause} order by a.id desc limit ? offset ?"
        )
    params = list(where_params) + [page_size, offset]

    result = await db.execute(sql, params)
    columns = [desc[0] for desc in result.description]

    entries = []
    for row in result.rows:
        row_dict = dict(zip(columns, row))
        pk = {}
        for col in pk_columns:
            pk[col] = row_dict.get(f"pk_{col}")
        updated_values = (
            json.loads(row_dict["updated_values"])
            if row_dict["updated_values"] is not None
            else None
        )
        entry = {
            "id": row_dict["id"],
            "timestamp": row_dict["timestamp"],
            "operation": row_dict["operation"],
            "pk": pk,
            "updated_values": updated_values,
        }
        if row_dict.get("group") is not None:
            entry["group"] = row_dict["group"]
            entry["group_note"] = row_dict.get("group_note")
        entries.append(entry)

    return Response.json(
        {
            "ok": True,
            "table": table,
            "is_tracked": is_tracked,
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
            "entries": entries,
        }
    )


async def row_history_page(request, datasette):
    database = tilde_decode(request.url_vars["database"])
    table = tilde_decode(request.url_vars["table"])
    pks_string = request.url_vars["pks"]

    try:
        db = datasette.get_database(database)
    except KeyError:
        return Response.text("Database not found", status=404)

    if not await datasette.allowed(
        action="sqlite-history-json-view",
        actor=request.actor,
        resource=TableResource(database, table),
    ):
        raise Forbidden("Permission denied")

    if not await _has_history(db, table):
        return Response.text("No history for this table", status=404)

    pk_columns = await _get_pk_columns(db, table)
    pk_values = _parse_pks(pks_string, pk_columns)
    if pk_values is None:
        return Response.text("Invalid primary key", status=404)

    # Normalize PK string for use in URLs
    pks_string = _encode_pks(pk_values, pk_columns)
    row_exists = await _row_exists(db, table, pk_columns, pk_values)

    return Response.html(
        await datasette.render_template(
            "row_history.html",
            {
                "database": database,
                "table": table,
                "pk_values": pk_values,
                "pks_string": pks_string,
                "row_url": (
                    datasette.urls.row(database, table, pks_string)
                    if row_exists
                    else None
                ),
            },
            request=request,
        )
    )


async def row_history_api(request, datasette):
    database = tilde_decode(request.url_vars["database"])
    table = tilde_decode(request.url_vars["table"])
    pks_string = request.url_vars["pks"]

    try:
        db = datasette.get_database(database)
    except KeyError:
        return Response.json({"ok": False, "error": "Database not found"}, status=404)

    if not await datasette.allowed(
        action="sqlite-history-json-view",
        actor=request.actor,
        resource=TableResource(database, table),
    ):
        raise Forbidden("Permission denied")

    if not await _has_history(db, table):
        return Response.json(
            {"ok": False, "error": "No history for this table"}, status=404
        )

    pk_columns = await _get_pk_columns(db, table)
    pk_values = _parse_pks(pks_string, pk_columns)
    if pk_values is None:
        return Response.json({"ok": False, "error": "Invalid primary key"}, status=404)

    def _get_row_data(conn):
        entries = sqlite_history_json.get_row_history(conn, table, pk_values)
        state_sql = sqlite_history_json.row_state_sql(conn, table)

        results = []
        for entry in entries:
            if len(pk_columns) == 1:
                params = {"pk": pk_values[pk_columns[0]], "target_id": entry["id"]}
            else:
                params = {"target_id": entry["id"]}
                for i, col in enumerate(pk_columns, 1):
                    params[f"pk_{i}"] = pk_values[col]

            row = conn.execute(state_sql, params).fetchone()
            state = json.loads(row[0]) if row and row[0] else None

            result_entry = {
                "id": entry["id"],
                "timestamp": entry["timestamp"],
                "operation": entry["operation"],
                "pk": entry["pk"],
                "updated_values": entry["updated_values"],
                "state": state,
            }
            if entry.get("group") is not None:
                result_entry["group"] = entry["group"]
                result_entry["group_note"] = entry.get("group_note")
            results.append(result_entry)

        # Compute diffs (entries are newest-first)
        for i, entry in enumerate(results):
            if entry["operation"] == "insert":
                entry["diff"] = None
            elif entry["operation"] == "delete":
                if i + 1 < len(results) and results[i + 1]["state"]:
                    entry["diff"] = {
                        col: {"old": val, "new": None}
                        for col, val in results[i + 1]["state"].items()
                    }
                else:
                    entry["diff"] = None
            elif entry["updated_values"]:
                diff = {}
                prev_state = results[i + 1]["state"] if i + 1 < len(results) else None
                for col, new_val in entry["updated_values"].items():
                    if isinstance(new_val, dict):
                        if "null" in new_val:
                            new_val = None
                        elif "hex" in new_val:
                            new_val = f"(blob: {new_val['hex']})"
                    old_val = prev_state.get(col) if prev_state else None
                    diff[col] = {"old": old_val, "new": new_val}
                entry["diff"] = diff
            else:
                entry["diff"] = None

        return results

    entries = await db.execute_write_fn(_get_row_data)

    return Response.json(
        {
            "ok": True,
            "table": table,
            "pk_values": pk_values,
            "entries": entries,
        }
    )


@hookimpl
def register_routes(datasette):
    return [
        (
            r"^/-/history-json/(?P<database>[^/]+)/(?P<table>[^/]+)/-/enable$",
            enable_tracking_view,
        ),
        (
            r"^/-/history-json/(?P<database>[^/]+)/(?P<table>[^/]+)/-/disable$",
            disable_tracking_view,
        ),
        (
            r"^/-/history-json/(?P<database>[^/]+)/(?P<table>[^/]+)\.json$",
            table_history_api,
        ),
        (
            r"^/-/history-json/(?P<database>[^/]+)/(?P<table>[^/]+)/(?P<pks>.+)\.json$",
            row_history_api,
        ),
        (
            r"^/-/history-json/(?P<database>[^/]+)/(?P<table>[^/]+)$",
            table_history_page,
        ),
        (
            r"^/-/history-json/(?P<database>[^/]+)/(?P<table>[^/]+)/(?P<pks>.+)$",
            row_history_page,
        ),
    ]


@hookimpl
def register_actions(datasette):
    return [
        Action(
            name="sqlite-history-json",
            description="Enable and disable change tracking on tables",
            resource_class=DatabaseResource,
        ),
        Action(
            name="sqlite-history-json-view",
            description="View recorded change history for a table",
            resource_class=TableResource,
            also_requires="view-table",
        ),
    ]


@hookimpl(specname="permission_resources_sql")
async def permission_resources_sql(datasette, actor, action):
    # By default, allow viewing history for any table the actor can view.
    # This is disabled in --default-deny mode unless explicitly granted.
    if action != "sqlite-history-json-view":
        return None
    if datasette.default_deny:
        return None
    return PermissionSQL.allow(reason="default allow for sqlite-history-json-view")


@hookimpl
def table_actions(datasette, actor, database, table, request):
    async def inner():
        if table.startswith("_history_json_"):
            return []

        db = datasette.get_database(database)
        actions = []

        has_hist = await _has_history(db, table)
        tracked = await _is_tracked(db, table)

        if has_hist:
            actions.append(
                {
                    "href": datasette.urls.path(
                        f"/-/history-json/{tilde_encode(database)}/{tilde_encode(table)}"
                    ),
                    "label": "View history",
                    "description": "Browse a timeline of changes for this table.",
                }
            )

        can_manage = await datasette.allowed(
            action="sqlite-history-json",
            actor=actor,
            resource=DatabaseResource(database),
        )

        if can_manage and not db.is_mutable:
            can_manage = False

        if can_manage:
            if tracked:
                actions.append(
                    {
                        "href": datasette.urls.path(
                            f"/-/history-json/{tilde_encode(database)}/{tilde_encode(table)}/-/disable"
                        ),
                        "label": "Disable tracking",
                        "description": "Stop recording changes to this table.",
                    }
                )
            else:
                actions.append(
                    {
                        "href": datasette.urls.path(
                            f"/-/history-json/{tilde_encode(database)}/{tilde_encode(table)}/-/enable"
                        ),
                        "label": "Enable tracking",
                        "description": "Start recording changes to this table.",
                    }
                )

        return actions

    return inner()


@hookimpl
def row_actions(datasette, actor, request, database, table, row):
    async def inner():
        if table.startswith("_history_json_"):
            return []

        db = datasette.get_database(database)

        if not await datasette.allowed(
            action="sqlite-history-json-view",
            actor=actor,
            resource=TableResource(database, table),
        ):
            return []

        if not await _has_history(db, table):
            return []

        pk_columns = await _get_pk_columns(db, table)
        if not pk_columns:
            return []

        pk_values = {col: row[col] for col in pk_columns}
        pks_string = _encode_pks(pk_values, pk_columns)

        audit_name = _audit_table_name(table)
        where_bits = [f"{escape_sqlite('pk_' + col)} = ?" for col in pk_columns]
        sql = f"select count(*) from {escape_sqlite(audit_name)} where " + " and ".join(
            where_bits
        )
        result = await db.execute(sql, [pk_values[col] for col in pk_columns])
        count = result.single_value()

        return [
            {
                "href": datasette.urls.path(
                    f"/-/history-json/{tilde_encode(database)}/{tilde_encode(table)}/{pks_string}"
                ),
                "label": "View row history",
                "description": f"{count} change" + ("s" if count != 1 else ""),
            }
        ]

    return inner()
