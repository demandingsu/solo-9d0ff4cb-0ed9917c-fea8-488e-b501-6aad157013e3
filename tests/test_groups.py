"""分组代报：跨果园分组、重名冲突、撤组与并发交错。"""
import json
import threading
import time

import pytest

from app import ledger
from app.db import connect, read_transaction, write_transaction
from tests.conftest import (
    download_package,
    upload_batch,
    upload_good_batch,
    verify_with_reports,
)


def make_group(client, orchard, name, destinations):
    return client.post(
        "/groups",
        data={"orchard": orchard, "name": name, "destination": destinations},
        follow_redirects=True,
    )


def dissolve(client, orchard, group_id):
    return client.post(
        f"/groups/{group_id}/dissolve",
        data={"orchard": orchard},
        follow_redirects=True,
    )


def groups_of(app, orchard):
    with app.app_context():
        from app.db import get_db

        return ledger.list_groups(get_db(), orchard)


def entries_of(client, orchard):
    return {e["destination"]: e["crates"] for e in download_package(client, orchard)["entries"]}


def upload_more(client, inbound, deliveries):
    resp = upload_batch(
        client, inbound=inbound, transfers="起仓次,讫仓次,箱数\n", deliveries=deliveries
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)
    return resp


# ---------------------------------------------------------------- 跨果园分组

def test_cross_orchard_same_destination_can_join_different_groups(client, app):
    upload_good_batch(client)

    # 市场甲同时存在于果园A（55）和果园B（30）：两个果园可各自成组
    resp = make_group(client, "果园A", "经销商甲", ["市场甲", "市场乙"])
    assert "已在果园 果园A 建组" in resp.get_data(as_text=True)
    resp = make_group(client, "果园B", "经销商乙", ["市场甲", "市场丙"])
    assert "已在果园 果园B 建组" in resp.get_data(as_text=True)

    assert set(groups_of(app, "果园A")[0]["destinations"]) == {"市场甲", "市场乙"}
    assert set(groups_of(app, "果园B")[0]["destinations"]) == {"市场甲", "市场丙"}

    # 各果园按各自的组出数，互不影响
    assert entries_of(client, "果园A") == {"经销商甲": 95, "仓存": 5}
    assert entries_of(client, "果园B") == {"经销商乙": 75, "仓存": 5}


def test_group_merges_old_and_new_documents(client):
    """组内箱数包含建组前后的全部单据；未入组去向仍按原名出数。"""
    upload_good_batch(client)
    make_group(client, "果园A", "经销商甲", ["市场甲", "市场乙"])
    assert entries_of(client, "果园A") == {"经销商甲": 95, "仓存": 5}

    upload_more(
        client,
        "仓次,果园,箱数\nU1,果园A,20\n",
        "仓次,类型,去向,箱数\nU1,交货,市场甲,20\n",
    )
    # 新单据按原去向入账，汇总时自动并入已存在的组
    assert entries_of(client, "果园A") == {"经销商甲": 115, "仓存": 5}


def test_entries_keep_original_destination_in_ledger(client, app):
    """建组只影响汇总展示，交货/余箱仍按原去向入账。"""
    upload_good_batch(client)
    make_group(client, "果园A", "经销商甲", ["市场甲", "市场乙"])
    with app.app_context():
        from app.db import get_db

        conn = get_db()
        rows = conn.execute(
            "SELECT DISTINCT destination FROM deliveries WHERE destination IN ('市场甲','市场乙')"
        ).fetchall()
        assert {r["destination"] for r in rows} == {"市场甲", "市场乙"}
        assert ledger.destination_group_map(conn, "果园A") == {
            "市场甲": "经销商甲",
            "市场乙": "经销商甲",
        }


# ---------------------------------------------------------------- 重名 / 成员冲突

def test_group_name_conflicts_with_existing_group(client):
    upload_good_batch(client)
    make_group(client, "果园A", "经销商甲", ["市场甲", "市场乙"])
    resp = make_group(client, "果园A", "经销商甲", ["仓存", "市场乙"])
    body = resp.get_data(as_text=True)
    assert "组名 “经销商甲” 与本果园已有组名冲突" in body
    assert "已在组 “经销商甲” 中" in body
    assert "本次建组未生效" in body
    # 未产生第二个组
    assert len(groups_of(client.application, "果园A")) == 1


def test_group_name_conflicts_with_destination(client):
    upload_good_batch(client)
    resp = make_group(client, "果园A", "市场甲", ["市场乙", "仓存"])
    body = resp.get_data(as_text=True)
    assert "组名 “市场甲” 与本果园已有单据去向重名" in body
    assert "本次建组未生效" in body


def test_same_destination_cannot_join_two_groups_in_same_orchard(client):
    upload_good_batch(client)
    make_group(client, "果园A", "经销商甲", ["市场甲", "市场乙"])
    resp = make_group(client, "果园A", "经销商乙", ["市场甲", "仓存"])
    body = resp.get_data(as_text=True)
    assert "去向 “市场甲” 已在组 “经销商甲” 中" in body
    assert "本次建组未生效" in body


def test_group_needs_two_existing_destinations(client):
    upload_good_batch(client)
    resp = make_group(client, "果园A", "经销商甲", ["市场甲"])
    body = resp.get_data(as_text=True)
    assert "至少要选 2 个去向" in body

    resp = make_group(client, "果园A", "经销商乙", ["不存在去向", "市场甲"])
    body = resp.get_data(as_text=True)
    assert "不属于果园 果园A 的已入账去向" in body
    assert "不存在去向" in body


# ---------------------------------------------------------------- 上传与建组交错

def test_upload_destination_clashing_with_group_name_rejected(client, app):
    """先建组，再上传同名去向：后提交的上传整批退回并点名冲突。"""
    upload_good_batch(client)
    make_group(client, "果园A", "经销商甲", ["市场甲", "市场乙"])

    resp = upload_batch(
        client,
        inbound="仓次,果园,箱数\nV1,果园A,10\n",
        transfers="起仓次,讫仓次,箱数\n",
        deliveries="仓次,类型,去向,箱数\nV1,交货,经销商甲,10\n",
    )
    body = resp.get_data(as_text=True)
    assert "与已有组名重名" in body and "经销商甲" in body
    assert "本次上传已退回" in body
    # 整批未入账
    index = client.get("/").get_data(as_text=True)
    assert "V1" not in index
    with app.app_context():
        from app.db import get_db

        assert get_db().execute(
            "SELECT COUNT(*) FROM sessions WHERE name = 'V1'"
        ).fetchone()[0] == 0


def test_upload_then_group_name_clash_rejected(client, app):
    """先上传，再用该去向名当组名：后提交的建组整笔退回并点名冲突。"""
    upload_good_batch(client)
    upload_more(
        client,
        "仓次,果园,箱数\nU1,果园A,10\n",
        "仓次,类型,去向,箱数\nU1,交货,市场甲,10\n",
    )
    resp = make_group(client, "果园A", "市场乙", ["市场甲", "仓存"])
    body = resp.get_data(as_text=True)
    assert "组名 “市场乙” 与本果园已有单据去向重名" in body
    assert len(groups_of(client.application, "果园A")) == 0


def test_upload_name_matching_other_orchard_group_allowed(client, app):
    """组名只在本果园内占用：果园B 的组名不阻止果园A 新入账同名去向。"""
    resp = upload_batch(
        client,
        inbound="仓次,果园,箱数\nS1,果园A,95\nS3,果园B,75\n",
        transfers="起仓次,讫仓次,箱数\nS1,S2,40\n",
        deliveries=(
            "仓次,类型,去向,箱数\n"
            "S1,交货,市场甲,55\n"
            "S2,交货,市场乙,40\n"
            "S3,交货,市场甲,30\n"
            "S3,交货,市场丙,45\n"
        ),
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)
    make_group(client, "果园B", "经销商乙", ["市场甲", "市场丙"])

    resp = upload_batch(
        client,
        inbound="仓次,果园,箱数\nX1,果园A,8\n",
        transfers="起仓次,讫仓次,箱数\n",
        deliveries="仓次,类型,去向,箱数\nX1,交货,经销商乙,8\n",
    )
    assert resp.status_code == 302, resp.get_data(as_text=True)
    assert entries_of(client, "果园A") == {
        "市场甲": 55,
        "市场乙": 40,
        "经销商乙": 8,
    }
    # 果园B 的组不受影响
    assert entries_of(client, "果园B") == {"经销商乙": 75}


def test_interleaved_writes_serialize_and_reject_second(tmp_path):
    """两个连接交错：后拿到写锁的一方看到先提交结果，自行退回，不产生重名。"""
    from app import db as dbmod

    db_path = str(tmp_path / "ledger.db")
    setup = connect(db_path)
    setup.executescript(dbmod.SCHEMA)
    setup.execute("PRAGMA journal_mode = WAL")
    setup.execute(
        "INSERT INTO uploads (batch_hash, uploaded_at, inbound_count, transfer_count, delivery_count)"
        " VALUES ('h', 'now', 1, 0, 2)"
    )
    setup.execute("INSERT INTO sessions (name, orchard) VALUES ('S1','果园A')")
    for dest, crates in (("市场甲", 10), ("市场乙", 10)):
        setup.execute(
            "INSERT INTO deliveries (upload_id, session, kind, destination, crates)"
            " VALUES (1,'S1','交货',?,?)",
            (dest, crates),
        )
    setup.close()

    first = connect(db_path)
    second = connect(db_path)
    with write_transaction(first):
        ledger.create_group(first, "果园A", "经销商甲", ["市场甲", "市场乙"])
    with pytest.raises(ledger.GroupRejected) as exc:
        with write_transaction(second):
            ledger.create_group(second, "果园A", "经销商甲", ["市场甲"])
    assert any("已有组名冲突" in p for p in exc.value.problems)
    # 没有部分写入
    assert len(ledger.list_groups(second, "果园A")) == 1
    first.close()
    second.close()


def test_group_rollback_leaves_no_partial_member_rows(tmp_path):
    from app import db as dbmod

    db_path = str(tmp_path / "ledger.db")
    conn = connect(db_path)
    conn.executescript(dbmod.SCHEMA)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        "INSERT INTO uploads (batch_hash, uploaded_at, inbound_count, transfer_count, delivery_count)"
        " VALUES ('h', 'now', 1, 0, 2)"
    )
    conn.execute("INSERT INTO sessions (name, orchard) VALUES ('S1','果园A')")
    for dest, crates in (("市场甲", 10), ("市场乙", 10)):
        conn.execute(
            "INSERT INTO deliveries (upload_id, session, kind, destination, crates)"
            " VALUES (1,'S1','交货',?,?)",
            (dest, crates),
        )
    with pytest.raises(ledger.GroupRejected):
        with write_transaction(conn):
            ledger.create_group(conn, "果园A", "经销商甲", ["市场甲", "市场乙"])
            # 事务内再建重名组 -> 抛错，整个事务回滚（含第一组）
            ledger.create_group(conn, "果园A", "经销商乙", ["市场甲"])
    assert ledger.list_groups(conn, "果园A") == []
    assert conn.execute("SELECT COUNT(*) FROM dest_group_members").fetchone()[0] == 0
    conn.close()


def test_read_snapshot_consistent_while_writer_commits(tmp_path):
    """下载包的读事务持有一致快照：期间入账 / 建组提交，读到的箱数与分组仍同属一个状态。"""
    from app import db as dbmod

    db_path = str(tmp_path / "ledger.db")
    setup = connect(db_path)
    setup.executescript(dbmod.SCHEMA)
    setup.execute("PRAGMA journal_mode = WAL")
    setup.execute(
        "INSERT INTO uploads (batch_hash, uploaded_at, inbound_count, transfer_count, delivery_count)"
        " VALUES ('h', 'now', 1, 0, 2)"
    )
    setup.execute("INSERT INTO sessions (name, orchard) VALUES ('S1','果园A')")
    for dest, crates in (("市场甲", 10), ("市场乙", 10)):
        setup.execute(
            "INSERT INTO deliveries (upload_id, session, kind, destination, crates)"
            " VALUES (1,'S1','交货',?,?)",
            (dest, crates),
        )
    setup.close()

    reader = connect(db_path)
    started = threading.Event()
    done = threading.Event()
    errors = []

    def commit_while_reader_holds_snapshot():
        writer = connect(db_path)  # sqlite 连接须在所属线程创建
        try:
            started.wait(5)
            with write_transaction(writer):
                # 新增同一去向的单据并同时建组
                writer.execute(
                    "INSERT INTO deliveries (upload_id, session, kind, destination, crates)"
                    " VALUES (1,'S1','交货','市场甲',5)"
                )
                ledger.create_group(writer, "果园A", "经销商甲", ["市场甲", "市场乙"])
        except BaseException as exc:  # 让主线程看到线程内异常
            errors.append(exc)
        finally:
            writer.close()
            done.set()

    t = threading.Thread(target=commit_while_reader_holds_snapshot)
    t.start()
    with read_transaction(reader):
        before = ledger.orchard_entries(reader, "果园A")
        started.set()
        # 读事务期间写事务可以提交（WAL），但我们的快照不变
        assert done.wait(10), "writer was blocked by reader snapshot"
        after = ledger.orchard_entries(reader, "果园A")
    t.join()
    assert not errors, errors
    assert before == after == [
        {"destination": "市场乙", "crates": 10},
        {"destination": "市场甲", "crates": 10},
    ]
    # 新事务看到的是新状态：新单并入组、箱数 +5
    with read_transaction(reader):
        fresh = ledger.orchard_entries(reader, "果园A")
    assert fresh == [{"destination": "经销商甲", "crates": 25}]
    reader.close()


# ---------------------------------------------------------------- 撤组

def test_dissolve_group_restores_per_destination(client, app):
    upload_good_batch(client)
    make_group(client, "果园A", "经销商甲", ["市场甲", "市场乙"])
    assert entries_of(client, "果园A") == {"经销商甲": 95, "仓存": 5}

    group_id = groups_of(app, "果园A")[0]["id"]
    resp = dissolve(client, "果园A", group_id)
    assert "恢复按原去向分别出数" in resp.get_data(as_text=True)
    assert entries_of(client, "果园A") == {"市场甲": 55, "市场乙": 40, "仓存": 5}
    assert groups_of(app, "果园A") == []

    # 撤组后可把同一去向再入新组
    make_group(client, "果园A", "经销商乙", ["市场甲", "仓存"])
    assert entries_of(client, "果园A") == {"经销商乙": 60, "市场乙": 40}


def test_dissolve_old_and_new_documents_both_restored(client, app):
    upload_good_batch(client)
    make_group(client, "果园A", "经销商甲", ["市场甲", "市场乙"])
    upload_more(  # 建组后同一去向又入账 20 箱
        client,
        "仓次,果园,箱数\nW1,果园A,20\n",
        "仓次,类型,去向,箱数\nW1,交货,市场甲,20\n",
    )
    assert entries_of(client, "果园A")["经销商甲"] == 115
    group_id = groups_of(app, "果园A")[0]["id"]
    dissolve(client, "果园A", group_id)
    assert entries_of(client, "果园A") == {"市场甲": 75, "市场乙": 40, "仓存": 5}


def test_dissolve_unknown_group_rejected(client, app):
    upload_good_batch(client)
    resp = dissolve(client, "果园A", 999)
    body = resp.get_data(as_text=True)
    assert "不存在该组" in body


def test_dissolve_wrong_orchard_rejected(client, app):
    upload_good_batch(client)
    make_group(client, "果园A", "经销商甲", ["市场甲", "市场乙"])
    group_id = groups_of(app, "果园A")[0]["id"]
    resp = dissolve(client, "果园B", group_id)
    assert "不存在该组" in resp.get_data(as_text=True)
    assert len(groups_of(app, "果园A")) == 1


# ---------------------------------------------------------------- 包的核对

def test_downloaded_package_verifies_against_group_entries(client):
    """已下载的包按包内（组名）去向核对，核对过程不查库。"""
    upload_good_batch(client)
    make_group(client, "果园A", "经销商甲", ["市场甲", "市场乙"])
    pkg = download_package(client, "果园A")
    entries = {e["destination"]: e["crates"] for e in pkg["entries"]}
    assert entries == {"经销商甲": 95, "仓存": 5}

    resp = verify_with_reports(client, pkg, {"经销商甲": 95, "仓存": 5})
    assert "相符" in resp.get_data(as_text=True)

    # 下载后再撤组，旧包仍按包内去向核对，不受库内分组变化影响
    body = verify_with_reports(client, pkg, {"经销商甲": 90, "仓存": 5}).get_data(as_text=True)
    assert "总数差额" in body


def test_package_entries_do_not_leak_sessions(client):
    upload_good_batch(client)
    make_group(client, "果园A", "经销商甲", ["市场甲", "市场乙"])
    pkg = download_package(client, "果园A")
    body = json.dumps(pkg, ensure_ascii=False)
    assert "S1" not in body and "S2" not in body


def test_packages_page_shows_groups(client):
    upload_good_batch(client)
    make_group(client, "果园A", "经销商甲", ["市场甲", "市场乙"])
    body = client.get("/packages?orchard=果园A").get_data(as_text=True)
    assert "经销商甲" in body
    assert "市场甲（已在组 “经销商甲” 中）" in body
    assert "撤组" in body
    # 各果园汇总标题显示组数（曾把组数覆盖成组明细列表）
    assert "<h3>果园A · 1 个组</h3>" in body
    assert "<h3>果园B</h3>" in body
