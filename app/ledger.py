"""台账核心逻辑：CSV 解析、整批校验、入账、核对包汇总。

业务规则
--------
1. 按整箱记数：所有箱数必须是正整数。
2. 入库注明果园；同一仓次的入库只能涉及一个果园；经转仓相连的仓次必须同属一个果园。
3. 转仓注明起讫仓次，起讫不得相同。
4. 交货与余箱合计最多 MAX_DESTINATIONS 个去向（全库口径）。
5. 每个仓次收发必须平衡：入库 + 转入 = 转出 + 交货 + 余箱。
   有差额则指出仓次，整批退回（不入账）。
6. 批次整体去重（内容哈希），仓次名全局唯一。
"""
import csv
import hashlib
import io
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

MAX_DESTINATIONS = 4
MAX_NAME_LEN = 64
MIN_GROUP_DESTINATIONS = 2

KIND_DELIVERY = "交货"
KIND_REMAINING = "余箱"
DELIVERY_KINDS = (KIND_DELIVERY, KIND_REMAINING)

INBOUND_HEADERS = ["仓次", "果园", "箱数"]
TRANSFER_HEADERS = ["起仓次", "讫仓次", "箱数"]
DELIVERY_HEADERS = ["仓次", "类型", "去向", "箱数"]


class BatchRejected(Exception):
    """整批校验未通过：problems 为面向用户的问题列表，本次上传全部退回。"""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


class GroupRejected(Exception):
    """建组 / 撤组被退回：problems 为面向用户的问题列表，本次改动不生效。"""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass
class Batch:
    inbound: list = field(default_factory=list)    # {session, orchard, crates}
    transfers: list = field(default_factory=list)  # {from_session, to_session, crates}
    deliveries: list = field(default_factory=list) # {session, kind, destination, crates}


# ---------------------------------------------------------------- CSV 解析

def _parse_crates(raw, label, line_no, problems):
    text = (raw or "").strip()
    if not text.isdigit():
        problems.append(f"{label} 第 {line_no} 行：箱数 “{raw}” 不是正整数（须按整箱记数）")
        return None
    value = int(text)
    if value < 1:
        problems.append(f"{label} 第 {line_no} 行：箱数必须为正整数")
        return None
    return value


def _parse_name(raw, label, line_no, col, problems):
    text = (raw or "").strip()
    if not text:
        problems.append(f"{label} 第 {line_no} 行：{col} 不能为空")
        return None
    if len(text) > MAX_NAME_LEN:
        problems.append(f"{label} 第 {line_no} 行：{col} 超长（>{MAX_NAME_LEN} 字符）")
        return None
    return text


def _read_csv(raw_bytes, required_headers, label, problems):
    """解析单个 CSV，返回 dict 行列表；表头不符或文件不可读时记入 problems。"""
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        problems.append(f"{label}：文件不是有效的 UTF-8 编码")
        return []
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        problems.append(f"{label}：文件为空")
        return []
    headers = [h.strip() for h in rows[0]]
    if headers != required_headers:
        problems.append(
            f"{label}：表头应为 {','.join(required_headers)}，实际为 {','.join(headers) or '（空）'}"
        )
        return []
    out = []
    for i, row in enumerate(rows[1:], start=2):  # 第 1 行是表头
        if len(row) != len(required_headers):
            problems.append(f"{label} 第 {i} 行：列数应为 {len(required_headers)}，实际 {len(row)}")
            continue
        out.append(dict(zip(required_headers, row)))
    return out


def parse_batch(inbound_bytes, transfer_bytes, delivery_bytes):
    """把三个 CSV 的字节流解析成 Batch；有任何问题抛 BatchRejected。"""
    problems = []
    inbound_raw = _read_csv(inbound_bytes, INBOUND_HEADERS, "入库 CSV", problems)
    transfer_raw = _read_csv(transfer_bytes, TRANSFER_HEADERS, "转仓 CSV", problems)
    delivery_raw = _read_csv(delivery_bytes, DELIVERY_HEADERS, "交货 CSV", problems)

    batch = Batch()
    # 逐行解析（行号 = CSV 中的实际行号，含表头偏移）
    for idx, r in enumerate(inbound_raw, start=2):
        session = _parse_name(r["仓次"], "入库 CSV", idx, "仓次", problems)
        orchard = _parse_name(r["果园"], "入库 CSV", idx, "果园", problems)
        crates = _parse_crates(r["箱数"], "入库 CSV", idx, problems)
        if session and orchard and crates:
            batch.inbound.append({"session": session, "orchard": orchard, "crates": crates})

    for idx, r in enumerate(transfer_raw, start=2):
        frm = _parse_name(r["起仓次"], "转仓 CSV", idx, "起仓次", problems)
        to = _parse_name(r["讫仓次"], "转仓 CSV", idx, "讫仓次", problems)
        crates = _parse_crates(r["箱数"], "转仓 CSV", idx, problems)
        if frm and to and frm == to:
            problems.append(f"转仓 CSV 第 {idx} 行：起讫仓次相同（{frm}）")
            frm = to = None
        if frm and to and crates:
            batch.transfers.append({"from_session": frm, "to_session": to, "crates": crates})

    for idx, r in enumerate(delivery_raw, start=2):
        session = _parse_name(r["仓次"], "交货 CSV", idx, "仓次", problems)
        kind = (r["类型"] or "").strip()
        if kind not in DELIVERY_KINDS:
            problems.append(f"交货 CSV 第 {idx} 行：类型只能是 交货 或 余箱，实际为 “{r['类型']}”")
            kind = None
        dest = _parse_name(r["去向"], "交货 CSV", idx, "去向", problems)
        crates = _parse_crates(r["箱数"], "交货 CSV", idx, problems)
        if session and kind and dest and crates:
            batch.deliveries.append(
                {"session": session, "kind": kind, "destination": dest, "crates": crates}
            )

    if not batch.inbound and not batch.transfers and not batch.deliveries and not problems:
        problems.append("三个 CSV 都没有有效数据行")
    if problems:
        raise BatchRejected(problems)
    return batch


# ---------------------------------------------------------------- 批次校验

class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def resolve_session_orchards(batch, problems):
    """确定每个仓次归属的果园；冲突记入 problems。返回 {session: orchard}。"""
    uf = _UnionFind()
    pinned = {}  # session -> orchard（来自入库行）
    for row in batch.inbound:
        s, o = row["session"], row["orchard"]
        if s in pinned and pinned[s] != o:
            problems.append(f"仓次 {s} 的入库涉及多个果园（{pinned[s]}、{o}），同一仓次只能归属一个果园")
        pinned.setdefault(s, o)
        uf.find(s)
    for t in batch.transfers:
        uf.union(t["from_session"], t["to_session"])
    for d in batch.deliveries:
        uf.find(d["session"])

    # 每个连通分量至多一个果园
    comp_orchard = {}
    for s in list(uf.parent):
        root = uf.find(s)
        o = pinned.get(s)
        if o is None:
            continue
        if root in comp_orchard and comp_orchard[root] != o:
            problems.append(
                f"仓次经转仓关联到不同果园（{comp_orchard[root]}、{o}），转仓只能在同一果园的仓次间进行"
            )
        comp_orchard.setdefault(root, o)

    return {s: comp_orchard.get(uf.find(s)) for s in uf.parent}


def check_balance(batch, problems):
    """仓次收发平衡：入库 + 转入 = 转出 + 交货 + 余箱。有差额指出仓次。"""
    inflow = defaultdict(int)
    outflow = defaultdict(int)
    for r in batch.inbound:
        inflow[r["session"]] += r["crates"]
    for t in batch.transfers:
        outflow[t["from_session"]] += t["crates"]
        inflow[t["to_session"]] += t["crates"]
    for d in batch.deliveries:
        outflow[d["session"]] += d["crates"]
    for s in sorted(set(inflow) | set(outflow)):
        if inflow[s] != outflow[s]:
            diff = inflow[s] - outflow[s]
            side = "收大于支" if diff > 0 else "支大于收"
            problems.append(
                f"仓次 {s} 收发有差额：收入 {inflow[s]} 箱，支出 {outflow[s]} 箱，{side} {abs(diff)} 箱"
            )


def validate_batch(batch, existing_sessions, existing_destinations, group_names_by_orchard=None):
    """整批校验，返回 {session: orchard}；未通过抛 BatchRejected。

    group_names_by_orchard 为 {orchard: {组名,...}}，用于拦截与组名重名的去向。
    """
    problems = []

    batch_sessions = (
        {r["session"] for r in batch.inbound}
        | {t["from_session"] for t in batch.transfers}
        | {t["to_session"] for t in batch.transfers}
        | {d["session"] for d in batch.deliveries}
    )
    reused = sorted(batch_sessions & set(existing_sessions))
    if reused:
        problems.append(f"以下仓次已入账，不得重复上传：{', '.join(reused)}")

    session_orchard = resolve_session_orchards(batch, problems)
    check_balance(batch, problems)

    destinations = {d["destination"] for d in batch.deliveries} | set(existing_destinations)
    if len(destinations) > MAX_DESTINATIONS:
        problems.append(
            f"交货和余箱合计最多 {MAX_DESTINATIONS} 个去向，当前共 {len(destinations)} 个："
            f"{', '.join(sorted(destinations))}"
        )

    if group_names_by_orchard:
        clashes = []
        seen = set()
        for d in batch.deliveries:
            orchard = session_orchard.get(d["session"])
            if orchard is None:
                continue
            key = (orchard, d["destination"])
            if key in seen:
                continue
            seen.add(key)
            if d["destination"] in group_names_by_orchard.get(orchard, set()):
                clashes.append(f"去向 “{d['destination']}”（果园 {orchard}）与已有组名重名")
        if clashes:
            problems.append(
                "以下名称与同果园的组名冲突，整批退回：" + "；".join(sorted(clashes))
            )

    if problems:
        raise BatchRejected(problems)
    return session_orchard


# ---------------------------------------------------------------- 入账

def batch_hash(inbound_bytes, transfer_bytes, delivery_bytes):
    h = hashlib.sha256()
    for chunk in (inbound_bytes, transfer_bytes, delivery_bytes):
        h.update(len(chunk).to_bytes(8, "big"))
        h.update(chunk)
    return h.hexdigest()


def persist_batch(conn, digest, batch, session_orchard):
    """在调用方已开启的写事务内入账；批次哈希冲突（重复上传）抛 BatchRejected。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        cur = conn.execute(
            "INSERT INTO uploads (batch_hash, uploaded_at, inbound_count, transfer_count, delivery_count)"
            " VALUES (?, ?, ?, ?, ?)",
            (digest, now, len(batch.inbound), len(batch.transfers), len(batch.deliveries)),
        )
        upload_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO inbound (upload_id, session, orchard, crates) VALUES (?, ?, ?, ?)",
            [(upload_id, r["session"], r["orchard"], r["crates"]) for r in batch.inbound],
        )
        conn.executemany(
            "INSERT INTO transfers (upload_id, from_session, to_session, crates) VALUES (?, ?, ?, ?)",
            [(upload_id, t["from_session"], t["to_session"], t["crates"]) for t in batch.transfers],
        )
        conn.executemany(
            "INSERT INTO deliveries (upload_id, session, kind, destination, crates) VALUES (?, ?, ?, ?, ?)",
            [(upload_id, d["session"], d["kind"], d["destination"], d["crates"]) for d in batch.deliveries],
        )
        conn.executemany(
            "INSERT INTO sessions (name, orchard) VALUES (?, ?)",
            [(s, o) for s, o in sorted(session_orchard.items()) if o is not None],
        )
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" in str(exc) and "batch_hash" in str(exc):
            raise BatchRejected(["该批次已上传并入账，请勿重复提交"]) from exc
        if "UNIQUE" in str(exc) and "sessions" in str(exc):
            raise BatchRejected(["仓次名已被其他上传占用，请刷新页面后重新提交"]) from exc
        raise


# ---------------------------------------------------------------- 查询 / 汇总

def existing_sessions(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sessions")}


def existing_destinations(conn):
    return {r["destination"] for r in conn.execute("SELECT DISTINCT destination FROM deliveries")}


def orchard_summaries(conn):
    """各果园汇总：总箱数、去向数、仓次数、组数。"""
    rows = conn.execute(
        """
        SELECT s.orchard AS orchard,
               COALESCE(SUM(d.crates), 0) AS total_crates,
               COUNT(DISTINCT d.destination) AS destinations,
               COUNT(DISTINCT d.session) AS sessions
        FROM sessions s
        LEFT JOIN deliveries d ON d.session = s.name
        GROUP BY s.orchard
        ORDER BY s.orchard
        """
    ).fetchall()
    group_counts = {
        r["orchard"]: r["n"]
        for r in conn.execute(
            "SELECT orchard, COUNT(*) AS n FROM dest_groups GROUP BY orchard"
        )
    }
    out = []
    for r in rows:
        item = dict(r)
        item["groups"] = group_counts.get(item["orchard"], 0)
        out.append(item)
    return out


def orchard_destinations(conn, orchard):
    """某果园已入账的单据去向（去重）。"""
    rows = conn.execute(
        """
        SELECT DISTINCT d.destination AS destination
        FROM deliveries d JOIN sessions s ON d.session = s.name
        WHERE s.orchard = ?
        """,
        (orchard,),
    )
    return {r["destination"] for r in rows}


def group_names_by_orchard(conn):
    """{orchard: {组名, ...}}，用于核对上传去向是否与组名冲突。"""
    out = defaultdict(set)
    for r in conn.execute("SELECT orchard, name FROM dest_groups"):
        out[r["orchard"]].add(r["name"])
    return dict(out)


def list_groups(conn, orchard=None):
    """建组结果，按果园 + 组名排序：[{id, orchard, name, destinations}]。"""
    if orchard is None:
        group_rows = conn.execute(
            "SELECT id, orchard, name FROM dest_groups ORDER BY orchard, name"
        ).fetchall()
    else:
        group_rows = conn.execute(
            "SELECT id, orchard, name FROM dest_groups WHERE orchard = ? ORDER BY name",
            (orchard,),
        ).fetchall()
    by_id = {r["id"]: {"id": r["id"], "orchard": r["orchard"], "name": r["name"], "destinations": []}
             for r in group_rows}
    if by_id:
        placeholders = ",".join("?" for _ in by_id)
        for m in conn.execute(
            f"SELECT group_id, destination FROM dest_group_members"
            f" WHERE group_id IN ({placeholders}) ORDER BY destination",
            tuple(by_id),
        ):
            by_id[m["group_id"]]["destinations"].append(m["destination"])
    return list(by_id.values())


def destination_group_map(conn, orchard):
    """{去向: 组名}，仅该果园。"""
    rows = conn.execute(
        """
        SELECT m.destination AS destination, g.name AS name
        FROM dest_group_members m JOIN dest_groups g ON g.id = m.group_id
        WHERE m.orchard = ?
        """,
        (orchard,),
    )
    return {r["destination"]: r["name"] for r in rows}


def create_group(conn, orchard, name, destinations):
    """在调用方已开启的写事务内建组；不合法抛 GroupRejected（整笔不生效）。

    规则：从该果园已入账去向里至少选 2 个；一个去向在同一果园最多入一组；
    组名须与该果园的其他组名、单据去向都不同。冲突时 problems 里点名冲突名称。
    """
    name = (name or "").strip()
    destinations = list(dict.fromkeys((d or "").strip() for d in destinations if (d or "").strip()))

    problems = []
    if not name:
        problems.append("组名不能为空")
    elif len(name) > MAX_NAME_LEN:
        problems.append(f"组名超长（>{MAX_NAME_LEN} 字符）")
    if len(destinations) < MIN_GROUP_DESTINATIONS:
        problems.append(f"每个组至少要选 {MIN_GROUP_DESTINATIONS} 个去向")

    available = orchard_destinations(conn, orchard)
    unknown = [d for d in destinations if d not in available]
    if unknown:
        problems.append(
            f"以下去向不属于果园 {orchard} 的已入账去向：{', '.join(unknown)}"
        )

    if name:
        existing_names = {r["name"] for r in conn.execute(
            "SELECT name FROM dest_groups WHERE orchard = ?", (orchard,)
        )}
        if name in existing_names:
            problems.append(f"组名 “{name}” 与本果园已有组名冲突")
        if name in available:
            problems.append(f"组名 “{name}” 与本果园已有单据去向重名")

    if destinations:
        placeholders = ",".join("?" for _ in destinations)
        existing = conn.execute(
            f"""
            SELECT m.destination AS destination, g.name AS group_name
            FROM dest_group_members m JOIN dest_groups g ON g.id = m.group_id
            WHERE m.orchard = ? AND m.destination IN ({placeholders})
            ORDER BY m.destination
            """,
            (orchard, *destinations),
        ).fetchall()
        for r in existing:
            problems.append(
                f"去向 “{r['destination']}” 已在组 “{r['group_name']}” 中，同一果园内一个去向最多入一组"
            )

    if problems:
        raise GroupRejected(problems)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        cur = conn.execute(
            "INSERT INTO dest_groups (orchard, name, created_at) VALUES (?, ?, ?)",
            (orchard, name, now),
        )
        group_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO dest_group_members (group_id, orchard, destination) VALUES (?, ?, ?)",
            [(group_id, orchard, d) for d in destinations],
        )
    except sqlite3.IntegrityError as exc:
        raise GroupRejected([f"组名或去向与本果园现有分组冲突（{exc}），本次建组未生效"]) from exc
    return group_id


def dissolve_group(conn, orchard, group_id):
    """在调用方已开启的写事务内撤组；组内单据自动恢复按原去向出数。"""
    row = conn.execute(
        "SELECT id FROM dest_groups WHERE id = ? AND orchard = ?", (group_id, orchard)
    ).fetchone()
    if row is None:
        raise GroupRejected([f"果园 {orchard} 下不存在该组，可能已被撤销"])
    conn.execute("DELETE FROM dest_group_members WHERE group_id = ?", (group_id,))
    conn.execute("DELETE FROM dest_groups WHERE id = ?", (group_id,))


def orchard_entries(conn, orchard):
    """某果园按去向的箱数汇总 —— 核对包的全部内容。

    已入组的去向按组名合成一项（组内全部新旧单据一起计入），未入组的按原名
    单独列出；条目数只随“去向 / 组”数量变化，不含任何明细。
    """
    rows = conn.execute(
        """
        SELECT d.destination AS destination, SUM(d.crates) AS crates
        FROM deliveries d JOIN sessions s ON d.session = s.name
        WHERE s.orchard = ?
        GROUP BY d.destination
        """,
        (orchard,),
    ).fetchall()
    grouping = destination_group_map(conn, orchard)
    merged = defaultdict(int)
    for r in rows:
        merged[grouping.get(r["destination"], r["destination"])] += r["crates"]
    return [{"destination": d, "crates": merged[d]} for d in sorted(merged)]


def overview(conn):
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM uploads)   AS uploads,
          (SELECT COUNT(*) FROM sessions)  AS sessions,
          (SELECT COUNT(DISTINCT orchard) FROM sessions) AS orchards,
          (SELECT COUNT(DISTINCT destination) FROM deliveries) AS destinations
        """
    ).fetchone()
    return dict(row)
