import json
import sqlite3

from datasette.app import Datasette
from datasette.resources import TableResource
import pytest
import sqlite_history_json


def _create_test_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("create table items (id integer primary key, name text, price real)")
    conn.execute("insert into items values (1, 'Widget', 9.99)")
    conn.execute("insert into items values (2, 'Gadget', 19.99)")
    conn.execute(
        "create table user_roles (user_id integer, role_id integer, note text, primary key (user_id, role_id))"
    )
    conn.execute("insert into user_roles values (1, 1, 'admin')")
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    _create_test_db(path)
    return path


@pytest.fixture
def ds(db_path):
    datasette = Datasette([str(db_path)])
    datasette.root_enabled = True
    return datasette


def _root_cookies(ds):
    return {"ds_actor": ds.sign({"a": {"id": "root"}}, "actor")}


async def _csrftoken(ds, path, cookies=None):
    cookies = cookies or {}
    response = await ds.client.get(path, cookies=cookies)
    csrf = response.cookies.get("ds_csrftoken")
    return csrf


@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    installed_plugins = {p["name"] for p in response.json()}
    assert "datasette-sqlite-history-json" in installed_plugins


@pytest.mark.asyncio
async def test_enable_tracking_requires_permission(ds):
    # Without auth, should get 403
    response = await ds.client.get(
        "/-/history-json/test/items/-/enable",
        follow_redirects=False,
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_enable_tracking_get(ds):
    cookies = _root_cookies(ds)
    response = await ds.client.get(
        "/-/history-json/test/items/-/enable",
        cookies=cookies,
    )
    assert response.status_code == 200
    assert "Enable tracking for items" in response.text


@pytest.mark.asyncio
async def test_enable_tracking_post(ds, db_path):
    cookies = _root_cookies(ds)
    # Get CSRF token
    csrf = await _csrftoken(ds, "/-/history-json/test/items/-/enable", cookies)
    cookies["ds_csrftoken"] = csrf
    response = await ds.client.post(
        "/-/history-json/test/items/-/enable",
        data={"csrftoken": csrf},
        cookies=cookies,
        follow_redirects=False,
    )
    assert response.status_code == 302

    # Verify tracking is enabled
    conn = sqlite3.connect(str(db_path))
    tables = [
        r[0]
        for r in conn.execute(
            "select name from sqlite_master where type='table'"
        ).fetchall()
    ]
    assert "_history_json_items" in tables
    triggers = [
        r[0]
        for r in conn.execute(
            "select name from sqlite_master where type='trigger'"
        ).fetchall()
    ]
    assert "_history_json_items_insert" in triggers
    assert "_history_json_items_update" in triggers
    assert "_history_json_items_delete" in triggers
    conn.close()


@pytest.mark.asyncio
async def test_disable_tracking_requires_permission(ds, db_path):
    # First enable tracking
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.commit()
    conn.close()

    # Without auth, should get 403
    response = await ds.client.get(
        "/-/history-json/test/items/-/disable",
        follow_redirects=False,
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_disable_tracking_post(ds, db_path):
    # First enable tracking
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.commit()
    conn.close()

    cookies = _root_cookies(ds)
    csrf = await _csrftoken(ds, "/-/history-json/test/items/-/disable", cookies)
    cookies["ds_csrftoken"] = csrf
    response = await ds.client.post(
        "/-/history-json/test/items/-/disable",
        data={"csrftoken": csrf},
        cookies=cookies,
        follow_redirects=False,
    )
    assert response.status_code == 302

    # Triggers removed but audit table remains
    conn = sqlite3.connect(str(db_path))
    triggers = [
        r[0]
        for r in conn.execute(
            "select name from sqlite_master where type='trigger'"
        ).fetchall()
    ]
    assert "_history_json_items_insert" not in triggers
    tables = [
        r[0]
        for r in conn.execute(
            "select name from sqlite_master where type='table'"
        ).fetchall()
    ]
    assert "_history_json_items" in tables
    conn.close()


@pytest.mark.asyncio
async def test_sqlite_history_view_requires_view_table(ds, db_path):
    await ds.invoke_startup()

    actor_without_view_table = {
        "id": "history-viewer",
        "_r": {"r": {"test": {"items": ["sqlite-history-json-view"]}}},
    }
    actor_with_view_table = {
        "id": "history-viewer",
        "_r": {"r": {"test": {"items": ["view-table", "sqlite-history-json-view"]}}},
    }
    table = TableResource("test", "items")

    assert not await ds.allowed(
        action="view-table",
        resource=table,
        actor=actor_without_view_table,
    )
    assert not await ds.allowed(
        action="sqlite-history-json-view",
        resource=table,
        actor=actor_without_view_table,
    )

    assert await ds.allowed(
        action="view-table", resource=table, actor=actor_with_view_table
    )
    assert await ds.allowed(
        action="sqlite-history-json-view",
        resource=table,
        actor=actor_with_view_table,
    )


@pytest.mark.asyncio
async def test_table_history_api(ds, db_path):
    # Enable tracking and make some changes
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.execute("update items set price = 12.99 where id = 1")
    conn.execute("insert into items values (3, 'Doohickey', 5.99)")
    conn.execute("delete from items where id = 2")
    conn.commit()
    conn.close()

    response = await ds.client.get("/-/history-json/test/items.json")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["table"] == "items"
    assert data["is_tracked"] is True
    assert data["total_count"] > 0
    assert len(data["entries"]) > 0

    # Each entry has expected fields
    entry = data["entries"][0]
    assert "id" in entry
    assert "timestamp" in entry
    assert "operation" in entry
    assert "pk" in entry


@pytest.mark.asyncio
async def test_table_history_api_operation_filter(ds, db_path):
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.execute("update items set price = 12.99 where id = 1")
    conn.commit()
    conn.close()

    # Filter by operation
    response = await ds.client.get("/-/history-json/test/items.json?operation=update")
    data = response.json()
    assert data["ok"] is True
    for entry in data["entries"]:
        assert entry["operation"] == "update"

    response = await ds.client.get("/-/history-json/test/items.json?operation=insert")
    data = response.json()
    assert data["ok"] is True
    for entry in data["entries"]:
        assert entry["operation"] == "insert"


@pytest.mark.asyncio
async def test_table_history_api_pagination(ds, db_path):
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.commit()
    conn.close()

    response = await ds.client.get("/-/history-json/test/items.json?page=1")
    data = response.json()
    assert data["page"] == 1
    assert data["page_size"] == 50


@pytest.mark.asyncio
async def test_row_history_api(ds, db_path):
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.execute("update items set price = 12.99 where id = 1")
    conn.execute("update items set name = 'Super Widget' where id = 1")
    conn.commit()
    conn.close()

    response = await ds.client.get("/-/history-json/test/items/1.json")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["table"] == "items"
    assert data["pk_values"] == {"id": "1"}
    assert len(data["entries"]) > 0

    # Entries should have state and diff
    for entry in data["entries"]:
        assert "state" in entry
        assert "diff" in entry

    # Most recent should be an update with diff
    newest = data["entries"][0]
    assert newest["operation"] == "update"
    assert newest["diff"] is not None
    assert "name" in newest["diff"]


@pytest.mark.asyncio
async def test_row_history_api_compound_pk(ds, db_path):
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "user_roles")
    conn.execute(
        "update user_roles set note = 'super admin' where user_id = 1 and role_id = 1"
    )
    conn.commit()
    conn.close()

    response = await ds.client.get("/-/history-json/test/user_roles/1,1.json")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["pk_values"] == {"user_id": "1", "role_id": "1"}
    assert len(data["entries"]) > 0


@pytest.mark.asyncio
async def test_table_history_page(ds, db_path):
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.commit()
    conn.close()

    response = await ds.client.get("/-/history-json/test/items")
    assert response.status_code == 200
    assert "History for items" in response.text


@pytest.mark.asyncio
async def test_row_history_page(ds, db_path):
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.commit()
    conn.close()

    response = await ds.client.get("/-/history-json/test/items/1")
    assert response.status_code == 200
    assert "Row history" in response.text


@pytest.mark.asyncio
async def test_table_history_404_for_untracked(ds):
    response = await ds.client.get("/-/history-json/test/items.json")
    assert response.status_code == 404
    data = response.json()
    assert data["ok"] is False


@pytest.mark.asyncio
async def test_row_history_404_for_untracked(ds):
    response = await ds.client.get("/-/history-json/test/items/1.json")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_table_actions_enable(ds):
    cookies = _root_cookies(ds)
    response = await ds.client.get("/test/items", cookies=cookies)
    assert response.status_code == 200
    assert "Enable tracking" in response.text
    assert "Start recording changes to this table." in response.text


@pytest.mark.asyncio
async def test_table_actions_view_history_and_disable(ds, db_path):
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.commit()
    conn.close()

    cookies = _root_cookies(ds)
    response = await ds.client.get("/test/items", cookies=cookies)
    assert response.status_code == 200
    assert "View history" in response.text
    assert "Disable tracking" in response.text
    assert "Browse a timeline of changes for this table." in response.text
    assert "Stop recording changes to this table." in response.text


@pytest.mark.asyncio
async def test_table_actions_no_manage_for_immutable():
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "imm.db")
        conn = sqlite3.connect(path)
        conn.execute("create table t (id integer primary key, v text)")
        conn.commit()
        conn.close()
        ds = Datasette(immutables=[path])
        ds.root_enabled = True
        cookies = _root_cookies(ds)
        response = await ds.client.get("/imm/t", cookies=cookies)
        assert response.status_code == 200
        assert "Enable tracking" not in response.text


@pytest.mark.asyncio
async def test_table_actions_not_shown_for_audit_tables(ds, db_path):
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.commit()
    conn.close()

    cookies = _root_cookies(ds)
    response = await ds.client.get("/test/_history_json_items", cookies=cookies)
    assert response.status_code == 200
    # Audit table should not show history actions
    assert "Enable tracking" not in response.text
    assert "View history" not in response.text


@pytest.mark.asyncio
async def test_database_not_found(ds):
    response = await ds.client.get("/-/history-json/nonexistent/items.json")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_row_history_state_reconstruction(ds, db_path):
    """Test that state is correctly reconstructed at each point in history."""
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.execute("update items set price = 12.99 where id = 1")
    conn.execute("update items set name = 'Super Widget', price = 15.99 where id = 1")
    conn.commit()
    conn.close()

    response = await ds.client.get("/-/history-json/test/items/1.json")
    data = response.json()
    entries = data["entries"]

    # Newest first - should be the most recent update
    assert entries[0]["state"]["name"] == "Super Widget"
    assert entries[0]["state"]["price"] == 15.99

    # Previous update
    assert entries[1]["state"]["name"] == "Widget"
    assert entries[1]["state"]["price"] == 12.99

    # Original insert
    assert entries[2]["state"]["name"] == "Widget"
    assert entries[2]["state"]["price"] == 9.99


@pytest.mark.asyncio
async def test_history_after_disable(ds, db_path):
    """After disabling tracking, history is still viewable."""
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.execute("update items set price = 12.99 where id = 1")
    conn.commit()
    sqlite_history_json.disable_tracking(conn, "items")
    conn.commit()
    conn.close()

    response = await ds.client.get("/-/history-json/test/items.json")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["is_tracked"] is False
    assert data["total_count"] > 0


@pytest.mark.asyncio
async def test_table_history_api_change_groups(ds, db_path):
    """Changes made inside a change_group include group and group_note."""
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.commit()

    with sqlite_history_json.change_group(conn, note="bulk update") as group_id:
        conn.execute("update items set price = 12.99 where id = 1")
        conn.execute("update items set price = 29.99 where id = 2")
    conn.commit()
    conn.close()

    response = await ds.client.get("/-/history-json/test/items.json")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True

    # The two updates should have group info
    grouped = [e for e in data["entries"] if e.get("group") is not None]
    assert len(grouped) == 2
    for entry in grouped:
        assert entry["group"] == group_id
        assert entry["group_note"] == "bulk update"

    # The initial populate inserts should not have group info
    ungrouped = [e for e in data["entries"] if "group" not in e]
    assert len(ungrouped) > 0


@pytest.mark.asyncio
async def test_table_history_api_filter_by_group(ds, db_path):
    """?group= filters to entries from that change group."""
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.commit()

    with sqlite_history_json.change_group(conn, note="first batch") as gid1:
        conn.execute("update items set price = 12.99 where id = 1")

    with sqlite_history_json.change_group(conn, note="second batch") as gid2:
        conn.execute("update items set price = 29.99 where id = 2")

    conn.commit()
    conn.close()

    response = await ds.client.get(f"/-/history-json/test/items.json?group={gid1}")
    data = response.json()
    assert data["ok"] is True
    assert data["total_count"] == 1
    assert all(e["group"] == gid1 for e in data["entries"])

    response = await ds.client.get(f"/-/history-json/test/items.json?group={gid2}")
    data = response.json()
    assert data["ok"] is True
    assert data["total_count"] == 1
    assert all(e["group"] == gid2 for e in data["entries"])


@pytest.mark.asyncio
async def test_row_history_api_change_groups(ds, db_path):
    """Row history entries include group info when present."""
    conn = sqlite3.connect(str(db_path))
    sqlite_history_json.enable_tracking(conn, "items")
    conn.commit()

    with sqlite_history_json.change_group(conn, note="price change") as group_id:
        conn.execute("update items set price = 12.99 where id = 1")
    conn.commit()
    conn.close()

    response = await ds.client.get("/-/history-json/test/items/1.json")
    assert response.status_code == 200
    data = response.json()
    entries = data["entries"]

    # Most recent entry (the update) should have group info
    assert entries[0]["group"] == group_id
    assert entries[0]["group_note"] == "price change"

    # The initial insert should not have group info
    assert "group" not in entries[-1]
