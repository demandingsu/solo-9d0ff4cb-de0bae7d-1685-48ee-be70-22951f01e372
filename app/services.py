"""业务逻辑：CSV 入仓、同杆分组、唯一性匹配、单条撤销方案与复算入仓。

核心模型
--------
灯杆按 (来源台账, 灯杆号) 区分；故障单按 (来源台账, 单号) 区分。
活动的同杆确认把若干灯杆引用并成一个连通组（传递）。
组内每张故障单必须分到不同资产号（二分图匹配，Kuhn 算法，结果确定性）。
已核对单子的归属作为强制匹配边，复算时不得改变。
"""
from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
from dataclasses import dataclass

from .db import get_db, init_db


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def pole_label(source: str, pole_no: str) -> str:
    return f"{source}/{pole_no}"


def ticket_label(source: str, ticket_no: str) -> str:
    return f"{source}/{ticket_no}"


def group_key_for(poles: set[str]) -> str:
    return hashlib.sha1("|".join(sorted(poles)).encode("utf-8")).hexdigest()[:16]


def _read_csv(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("CSV 为空或缺少表头")
    rows = []
    for i, row in enumerate(reader, start=2):  # 表头是第 1 行
        clean = {k: (v or "").strip() for k, v in row.items() if k is not None}
        if not any(clean.values()):
            continue  # 跳过空行
        clean["_line"] = i
        rows.append(clean)
    return rows


def _require(row: dict[str, str], fields: tuple[str, ...]) -> None:
    for f in fields:
        if not row.get(f):
            raise ValueError(f"第 {row['_line']} 行缺少必填列 {f}")


# --------------------------------------------------------------------------- #
# 入仓：资产号清单（全量替换）、故障单与同杆确认（可补传，upsert）
# --------------------------------------------------------------------------- #
def import_assets(raw: bytes, filename: str) -> str:
    rows = _read_csv(raw)
    _require(rows[0], ("asset_no", "pole_source", "pole_no")) if rows else None
    seen: set[str] = set()
    parsed: list[tuple[str, str, str]] = []
    for row in rows:
        _require(row, ("asset_no", "pole_source", "pole_no"))
        if row["asset_no"] in seen:
            raise ValueError(f"第 {row['_line']} 行资产号重复：{row['asset_no']}")
        seen.add(row["asset_no"])
        parsed.append((row["asset_no"], row["pole_source"], row["pole_no"]))

    conn = get_db()
    try:
        init_db(conn)
        used = conn.execute(
            "SELECT COUNT(*) c FROM verified v JOIN asset a ON a.id=v.asset_id"
        ).fetchone()["c"]
        used += conn.execute(
            "SELECT COUNT(*) c FROM warehouse_item wi JOIN asset a ON a.id=wi.asset_id"
        ).fetchone()["c"]
        if used:
            raise ValueError("已有单子核对到资产号，资产清单不能整体替换；请保留原资产号后再上传")
        with conn:
            conn.execute("DELETE FROM asset")
            conn.executemany(
                "INSERT INTO asset(asset_no, pole_source, pole_no) VALUES (?,?,?)",
                parsed,
            )
            conn.execute(
                "INSERT INTO batch(kind, filename) VALUES ('asset', ?)", (filename,)
            )
        return f"资产清单已全量写入，共 {len(parsed)} 条"
    finally:
        conn.close()


def import_tickets(raw: bytes, filename: str) -> str:
    rows = _read_csv(raw)
    parsed: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        _require(row, ("ticket_no", "source", "pole_source", "pole_no"))
        key = (row["source"], row["ticket_no"])
        if key in seen:
            raise ValueError(
                f"第 {row['_line']} 行单号在本文件内重复：{ticket_label(*key)}"
            )
        seen.add(key)
        parsed.append(
            (row["source"], row["ticket_no"], row["pole_source"], row["pole_no"])
        )

    conn = get_db()
    try:
        init_db(conn)
        inserted = updated = 0
        with conn:
            for source, ticket_no, pole_source, pole_no in parsed:
                old = conn.execute(
                    "SELECT t.id, v.asset_id FROM ticket t "
                    "LEFT JOIN verified v ON v.ticket_id=t.id "
                    "WHERE t.source=? AND t.ticket_no=?",
                    (source, ticket_no),
                ).fetchone()
                if old:
                    if old["asset_id"] is not None and (
                        pole_source, pole_no
                    ) != (
                        conn.execute(
                            "SELECT pole_source, pole_no FROM ticket WHERE id=?",
                            (old["id"],),
                        ).fetchone()
                    ):
                        raise ValueError(
                            f"单号 {ticket_label(source, ticket_no)} 已核对，不能改挂灯杆"
                        )
                    conn.execute(
                        "UPDATE ticket SET pole_source=?, pole_no=?, "
                        "updated_at=datetime('now','localtime') WHERE id=?",
                        (pole_source, pole_no, old["id"]),
                    )
                    updated += 1
                else:
                    conn.execute(
                        "INSERT INTO ticket(source, ticket_no, pole_source, pole_no) "
                        "VALUES (?,?,?,?)",
                        (source, ticket_no, pole_source, pole_no),
                    )
                    inserted += 1
            conn.execute(
                "INSERT INTO batch(kind, filename) VALUES ('ticket', ?)", (filename,)
            )
        return f"故障单已写入：新增 {inserted} 张，更新 {updated} 张"
    finally:
        conn.close()


def import_confirmations(raw: bytes, filename: str) -> str:
    rows = _read_csv(raw)
    parsed: list[tuple[str, str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        _require(row, ("source_a", "pole_no_a", "source_b", "pole_no_b"))
        a = (row["source_a"], row["pole_no_a"])
        b = (row["source_b"], row["pole_no_b"])
        if a == b:
            raise ValueError(f"第 {row['_line']} 行同杆确认的两端是同一根灯杆")
        a, b = sorted((a, b))
        pair = (a, b)
        if pair in seen:
            raise ValueError(
                f"第 {row['_line']} 行确认关系在本文件内重复："
                f"{pole_label(*a)} ~ {pole_label(*b)}"
            )
        seen.add(pair)
        parsed.append(
            (a[0], a[1], b[0], b[1], row.get("confirmed_by", ""))
        )

    conn = get_db()
    try:
        init_db(conn)
        added = reactivated = skipped = 0
        with conn:
            for sa, pa, sb, pb, by in parsed:
                old = conn.execute(
                    "SELECT id, active FROM confirmation "
                    "WHERE source_a=? AND pole_no_a=? AND source_b=? AND pole_no_b=?",
                    (sa, pa, sb, pb),
                ).fetchone()
                if old:
                    if old["active"]:
                        skipped += 1  # 活动确认已存在，幂等跳过
                    else:
                        conn.execute(
                            "UPDATE confirmation SET active=1, confirmed_by=?, "
                            "updated_at=datetime('now','localtime') WHERE id=?",
                            (by, old["id"]),
                        )
                        reactivated += 1
                else:
                    conn.execute(
                        "INSERT INTO confirmation(source_a, pole_no_a, source_b, "
                        "pole_no_b, confirmed_by) VALUES (?,?,?,?,?)",
                        (sa, pa, sb, pb, by),
                    )
                    added += 1
            conn.execute(
                "INSERT INTO batch(kind, filename) VALUES ('confirm', ?)", (filename,)
            )
        return f"同杆确认已写入：新增 {added} 条，重新确认 {reactivated} 条，已存在跳过 {skipped} 条"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 分组与匹配
# --------------------------------------------------------------------------- #
@dataclass
class Snapshot:
    tickets: list[sqlite3.Row]          # 全部故障单
    pole_assets: dict[str, set[int]]    # 灯杆 -> 资产 id 集合
    edges: list[sqlite3.Row]            # 活动同杆确认
    locks: dict[int, int]               # 已核对 ticket_id -> asset_id
    verified_group: dict[int, str]      # 已核对 ticket_id -> 旧 group_key


def load_snapshot(conn) -> Snapshot:
    tickets = conn.execute("SELECT * FROM ticket ORDER BY source, ticket_no").fetchall()
    pole_assets: dict[str, set[int]] = {}
    for a in conn.execute("SELECT * FROM asset").fetchall():
        pole_assets.setdefault(pole_label(a["pole_source"], a["pole_no"]), set()).add(
            a["id"]
        )
    edges = conn.execute(
        "SELECT * FROM confirmation WHERE active=1 ORDER BY id"
    ).fetchall()
    locks = {
        r["ticket_id"]: r["asset_id"]
        for r in conn.execute("SELECT ticket_id, asset_id FROM verified").fetchall()
    }
    verified_group = {
        r["ticket_id"]: r["group_key"]
        for r in conn.execute("SELECT ticket_id, group_key FROM verified").fetchall()
    }
    return Snapshot(tickets, pole_assets, edges, locks, verified_group)


class DSU:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def add(self, x: str):
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str):
        self.add(a)
        self.add(b)
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_components(snap: Snapshot, disabled_edge: int | None = None):
    """返回 list[dict]：每个连通组含 poles / tickets / assets。"""
    dsu = DSU()
    for t in snap.tickets:
        dsu.add(pole_label(t["pole_source"], t["pole_no"]))
    for p in snap.pole_assets:
        dsu.add(p)
    edge_pairs: list[tuple[int, str, str]] = []
    for e in snap.edges:
        if e["id"] == disabled_edge:
            continue
        a = pole_label(e["source_a"], e["pole_no_a"])
        b = pole_label(e["source_b"], e["pole_no_b"])
        edge_pairs.append((e["id"], a, b))
        dsu.union(a, b)

    groups: dict[str, dict] = {}
    for t in snap.tickets:
        p = pole_label(t["pole_source"], t["pole_no"])
        root = dsu.find(p)
        g = groups.setdefault(
            root,
            {"poles": set(), "ticket_ids": [], "asset_ids": set(), "edge_ids": set()},
        )
        g["poles"].add(p)
        g["ticket_ids"].append(t["id"])
    for p, assets in snap.pole_assets.items():
        root = dsu.find(p)
        if root in groups:
            groups[root]["poles"].add(p)
            groups[root]["asset_ids"].update(assets)
    for eid, a, b in edge_pairs:
        root = dsu.find(a)
        if root in groups:
            groups[root]["edge_ids"].add(eid)
    return list(groups.values())


def bipartite_match(
    ticket_ids: list[int],
    asset_ids: set[int],
    locks: dict[int, int],
) -> dict[int, int] | None:
    """组内每张单匹配唯一资产号；已核对单强制保留原归属。"""
    tickets = sorted(ticket_ids)
    assets = sorted(asset_ids)
    forced = {t: locks[t] for t in tickets if t in locks}
    for t, a in forced.items():
        if a not in asset_ids:
            return None  # 已核对单的原归属已不在组内，无法保持
    if len(set(forced.values())) != len(forced):
        return None  # 已核对单之间共用资产号，无法保持唯一归属
    match: dict[int, int] = {a: t for t, a in forced.items()}  # asset -> ticket
    locked_assets = set(forced.values())

    def try_kuhn(t: int, seen: set[int]) -> bool:
        for a in assets:
            if a in seen:
                continue
            seen.add(a)
            if a not in match or try_kuhn(match[a], seen):
                match[a] = t
                return True
        return False

    for t in tickets:
        if t in forced:
            continue
        if not try_kuhn(t, set(locked_assets)):
            return None
    return {t: a for a, t in match.items()}


def _component_view(conn, snap: Snapshot, comp: dict) -> dict:
    assignment = bipartite_match(comp["ticket_ids"], comp["asset_ids"], snap.locks)
    key = group_key_for(comp["poles"])
    trows = {t["id"]: t for t in snap.tickets}
    arows = {a["id"]: a for a in conn.execute("SELECT * FROM asset").fetchall()}
    return {
        "group_key": key,
        "poles": sorted(comp["poles"]),
        "edge_ids": sorted(comp["edge_ids"]),
        "ticket_ids": sorted(comp["ticket_ids"]),
        "asset_ids": sorted(comp["asset_ids"]),
        "feasible": assignment is not None,
        "assignment": assignment or {},
        "trows": trows,
        "arows": arows,
        "verified_ticket_ids": sorted(
            t for t in comp["ticket_ids"] if t in snap.locks
        ),
    }


def list_groups(conn) -> list[dict]:
    """冲突组：含多张灯杆引用且有故障单的连通组（同杆误认发生处）。"""
    snap = load_snapshot(conn)
    comps = build_components(snap)
    views = []
    selections = {
        r["group_key"]: r
        for r in conn.execute("SELECT * FROM undo_plan ORDER BY id DESC").fetchall()
    }
    warehouses = {
        r["group_key"]: r["id"]
        for r in conn.execute("SELECT group_key, id FROM warehouse").fetchall()
    }
    for comp in comps:
        if len(comp["poles"]) < 2 or not comp["ticket_ids"]:
            continue
        view = _component_view(conn, snap, comp)
        view["selection"] = selections.get(view["group_key"])
        view["warehouse_id"] = warehouses.get(view["group_key"])
        views.append(view)
    views.sort(key=lambda v: v["group_key"])
    return views


def get_group_detail(conn, group_key: str) -> dict | None:
    snap = load_snapshot(conn)
    for comp in build_components(snap):
        if group_key_for(comp["poles"]) != group_key:
            continue
        view = _component_view(conn, snap, comp)
        # 单条撤销方案：逐条模拟移除该确认后的匹配
        edge_rows = {e["id"]: e for e in snap.edges}
        options = []
        for eid in sorted(comp["edge_ids"]):
            sub_views = []
            ok = True
            for sub in build_components(snap, disabled_edge=eid):
                sub_poles = sub["poles"]
                if not (sub_poles & comp["poles"]) or not sub["ticket_ids"]:
                    continue
                sub_assign = bipartite_match(
                    sub["ticket_ids"], sub["asset_ids"], snap.locks
                )
                if sub_assign is None:
                    ok = False
                sub_views.append(_component_view(conn, snap, sub))
            if ok and sub_views:
                e = edge_rows[eid]
                options.append(
                    {
                        "confirmation_id": eid,
                        "edge_label": (
                            f"{pole_label(e['source_a'], e['pole_no_a'])} ～ "
                            f"{pole_label(e['source_b'], e['pole_no_b'])}"
                        ),
                        "partitions": sub_views,
                    }
                )
        view["options"] = options
        view["selection"] = conn.execute(
            "SELECT * FROM undo_plan WHERE group_key=? ORDER BY id DESC LIMIT 1",
            (group_key,),
        ).fetchone()
        view["warehouse_id"] = conn.execute(
            "SELECT id FROM warehouse WHERE group_key=?", (group_key,)
        ).fetchone()
        return view
    return None


# --------------------------------------------------------------------------- #
# 选定方案 -> 按最新资料复算 -> 条件满足才撤销并出整组入仓清单
# --------------------------------------------------------------------------- #
def apply_plan(conn, group_key: str, confirmation_id: int) -> dict:
    snap = load_snapshot(conn)

    # 1. 最新资料下重新定位该组
    target = None
    for comp in build_components(snap):
        if group_key_for(comp["poles"]) == group_key:
            target = comp
            break
    if target is None:
        raise ValueError("该冲突组在最新资料下已不存在（可能已撤销或确认关系已变化），请重新选择")

    edge = conn.execute(
        "SELECT * FROM confirmation WHERE id=? AND active=1", (confirmation_id,)
    ).fetchone()
    if edge is None:
        raise ValueError("选定的同杆确认不存在或已被撤销，请重新选择方案")
    if confirmation_id not in target["edge_ids"]:
        raise ValueError("选定的确认已不属于该冲突组，请重新选择方案")

    # 2. 复算：模拟撤销，每个子组都必须能一一匹配，且已核对单归属不变
    affected: list[dict] = []
    for sub in build_components(snap, disabled_edge=confirmation_id):
        if not (sub["poles"] & target["poles"]) or not sub["ticket_ids"]:
            continue
        assignment = bipartite_match(
            sub["ticket_ids"], sub["asset_ids"], snap.locks
        )
        if assignment is None:
            raise ValueError(
                "按最新资料复算：撤销该确认后仍有子组无法让每张单都有唯一资产号，"
                "请改选其他方案（无解则需补充资料）"
            )
        affected.append((sub, assignment))

    # 3. 条件核对：已核对单子归属不变；组内新单也都有唯一归属
    for sub, assignment in affected:
        for tid in sub["ticket_ids"]:
            if tid in snap.locks and assignment[tid] != snap.locks[tid]:
                raise ValueError(
                    "复算发现已核对单子的归属会发生变化，已保留原关系，请重新选择"
                )
    if not affected:
        raise ValueError("该组已没有待归属的故障单")

    # 4. 条件满足，单事务写入；任何失败整体回滚，原关系与归属保留
    arows = {a["id"]: a for a in conn.execute("SELECT * FROM asset").fetchall()}
    trows = {t["id"]: t for t in snap.tickets}
    try:
        with conn:  # 出错自动 rollback
            conn.execute(
                "UPDATE confirmation SET active=0, "
                "updated_at=datetime('now','localtime') WHERE id=?",
                (confirmation_id,),
            )
            conn.execute(
                "INSERT INTO undo_plan(group_key, confirmation_id, edge_label) "
                "VALUES (?,?,?)",
                (
                    group_key,
                    confirmation_id,
                    f"{pole_label(edge['source_a'], edge['pole_no_a'])} ～ "
                    f"{pole_label(edge['source_b'], edge['pole_no_b'])}",
                ),
            )
            wh = conn.execute(
                "SELECT id FROM warehouse WHERE group_key=?", (group_key,)
            ).fetchone()
            if wh:
                wid = wh["id"]
                conn.execute(
                    "DELETE FROM warehouse_item WHERE warehouse_id=?", (wid,)
                )
            else:
                cur = conn.execute(
                    "INSERT INTO warehouse(group_key) VALUES (?)", (group_key,)
                )
                wid = cur.lastrowid

            all_items: list[tuple[int, int, int]] = []
            for sub, assignment in affected:
                for tid, aid in assignment.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO verified(ticket_id, asset_id, group_key) "
                        "VALUES (?,?,?)",
                        (tid, aid, group_key),
                    )
                    all_items.append((wid, tid, aid))
            conn.executemany(
                "INSERT OR REPLACE INTO warehouse_item(warehouse_id, ticket_id, asset_id) "
                "VALUES (?,?,?)",
                all_items,
            )
    except sqlite3.Error as exc:  # 写入失败：保留原关系和归属
        raise ValueError(f"写入数据库失败，已回滚并保留原关系和归属：{exc}") from exc

    return {
        "group_key": group_key,
        "confirmation_id": confirmation_id,
        "partitions": [
            {
                "poles": sorted(sub["poles"]),
                "items": [
                    {
                        "ticket": ticket_label(
                            trows[tid]["source"], trows[tid]["ticket_no"]
                        ),
                        "asset_no": arows[aid]["asset_no"],
                        "locked": tid in snap.locks,
                    }
                    for tid, aid in sorted(assignment.items())
                ],
            }
            for sub, assignment in affected
        ],
        "warehouse_id": wid,
    }
