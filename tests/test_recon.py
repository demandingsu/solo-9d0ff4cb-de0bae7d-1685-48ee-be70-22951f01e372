"""端到端业务测试（Flask test client + 临时 SQLite）。

覆盖：
1. 三类 CSV 上传、补传与按来源区分编号；
2. 同杆确认的传递性分组；
3. 冲突组方案选择、复算撤销、整组入仓清单（已核对单归属不变）；
4. 补传后仍可撤销，与补传后条件不符须重选的情况；
5. 无解提示；
6. 条件不符/写入失败时原关系与归属保留。
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="pole-recon-test-")
os.environ["POLE_RECON_DB"] = os.path.join(_tmp, "test.db")

from app import services  # noqa: E402
from app.db import get_db, init_db  # noqa: E402
from app.routes import app  # noqa: E402


def csv_bytes(header: str, rows: list[tuple]) -> bytes:
    lines = [header] + [",".join(str(c) for c in r) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


ASSET_CSV = csv_bytes(
    "asset_no,pole_source,pole_no",
    [
        ("AST-001", "LEDGER_A", "P1"),
        ("AST-002", "LEDGER_B", "P1"),
        ("AST-003", "LEDGER_A", "P2"),
    ],
)
# 先传两张单：A/P1 与 B/P1 各一张，两个资产号 -> 撤销后可各得唯一归属
TICKET_CSV_1 = csv_bytes(
    "ticket_no,source,pole_source,pole_no",
    [
        ("T100", "LEDGER_A", "LEDGER_A", "P1"),
        ("T200", "LEDGER_B", "LEDGER_B", "P1"),
    ],
)
# 同杆确认两条，传递后 A/P1 - B/P1 - A/P2 并为一组
CONFIRM_CSV = csv_bytes(
    "source_a,pole_no_a,source_b,pole_no_b,confirmed_by",
    [
        ("LEDGER_A", "P1", "LEDGER_B", "P1", "巡检甲"),
        ("LEDGER_A", "P2", "LEDGER_B", "P1", "巡检乙"),
    ],
)
# 补传第三张单挂在 A/P2
TICKET_CSV_2 = csv_bytes(
    "ticket_no,source,pole_source,pole_no",
    [("T300", "LEDGER_A", "LEDGER_A", "P2")],
)
# 再补一张单，使整组 4 张单抢 3 个资产号，任意单条撤销都无解
TICKET_CSV_3 = csv_bytes(
    "ticket_no,source,pole_source,pole_no",
    [("T301", "LEDGER_A", "LEDGER_A", "P2")],
)


class ReconFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_db()
        init_db(cls.conn)
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        conn = get_db()
        for table in (
            "warehouse_item", "warehouse", "undo_plan", "verified",
            "confirmation", "ticket", "asset", "batch",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()

    def upload(self, kind: str, content: bytes, name: str = "f.csv"):
        return self.client.post(
            "/upload",
            data={"kind": kind, "file": (io.BytesIO(content), name)},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ #
    def test_01_uploads_and_transitive_group(self):
        self.upload("asset", ASSET_CSV)
        r = self.upload("ticket", TICKET_CSV_1)
        self.assertIn("新增 2 张", r.get_data(as_text=True))
        r = self.upload("confirm", CONFIRM_CSV)
        self.assertIn("新增 2 条", r.get_data(as_text=True))

        conn = get_db()
        groups = services.list_groups(conn)
        conn.close()
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(set(g["poles"]), {"LEDGER_A/P1", "LEDGER_B/P1", "LEDGER_A/P2"})
        self.assertEqual(len(g["ticket_ids"]), 2)
        self.assertEqual(len(g["asset_ids"]), 3)
        self.assertTrue(g["feasible"])
        self.group_key = g["group_key"]

    def test_02_options_and_apply_undo(self):
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        self.upload("confirm", CONFIRM_CSV)
        conn = get_db()
        g = services.list_groups(conn)[0]
        key = g["group_key"]
        detail = services.get_group_detail(conn, key)
        conn.close()
        # 两条确认都可作为单条撤销方案（撤销后每张单都有唯一资产号）
        self.assertEqual(len(detail["options"]), 2)

        # 选第一条方案执行
        opt = detail["options"][0]
        r = self.client.post(
            f"/groups/{key}/apply",
            data={"confirmation_id": opt["confirmation_id"]},
            follow_redirects=True,
        )
        text = r.get_data(as_text=True)
        self.assertIn("整组入仓清单", text)
        self.assertIn("AST-001", text)
        self.assertIn("AST-002", text)

        conn = get_db()
        # 确认已撤销
        self.assertEqual(
            conn.execute(
                "SELECT active FROM confirmation WHERE id=?",
                (opt["confirmation_id"],),
            ).fetchone()["active"],
            0,
        )
        # 两张单都已核对，归属是各自灯杆上的资产号
        rows = {
            (r["source"], r["ticket_no"]): r["asset_no"]
            for r in conn.execute(
                "SELECT t.source, t.ticket_no, a.asset_no "
                "FROM verified v JOIN ticket t ON t.id=v.ticket_id "
                "JOIN asset a ON a.id=v.asset_id"
            ).fetchall()
        }
        self.assertEqual(rows[("LEDGER_A", "T100")], "AST-001")
        self.assertEqual(rows[("LEDGER_B", "T200")], "AST-002")
        # 入仓清单含两张单
        wh = conn.execute("SELECT id FROM warehouse WHERE group_key=?", (key,)).fetchone()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) c FROM warehouse_item WHERE warehouse_id=?",
                (wh["id"],),
            ).fetchone()["c"],
            2,
        )
        # 撤销后原三杆连通组被拆开：不再存在同时含 A/P1 与 B/P1 的组
        remaining = services.list_groups(conn)
        for g in remaining:
            self.assertFalse(
                {"LEDGER_A/P1", "LEDGER_B/P1"} <= set(g["poles"]),
                "撤销后两根灯杆不应仍在同一冲突组",
            )
        # 已核对单即使仍在剩余冲突组中，也带锁定标记
        verified_ids = {
            r["ticket_id"]
            for r in conn.execute("SELECT ticket_id FROM verified").fetchall()
        }
        for g in remaining:
            for tid in g["ticket_ids"]:
                if tid in verified_ids:
                    self.assertIn(tid, g["verified_ticket_ids"])
        conn.close()

    def test_03_supplement_then_undo_still_works_locks_kept(self):
        """补传后再次成组：已核对单归属不变，新单也有唯一归属 -> 可撤销。"""
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        self.upload("confirm", CONFIRM_CSV)
        conn = get_db()
        key = services.list_groups(conn)[0]["group_key"]
        first_edge = services.get_group_detail(conn, key)["options"][0]["confirmation_id"]
        services.apply_plan(conn, key, first_edge)
        conn.close()

        # 补传：把被撤销的确认重新确认（模拟新资料），再补第三张单
        reconfirm = csv_bytes(
            "source_a,pole_no_a,source_b,pole_no_b",
            [("LEDGER_A", "P1", "LEDGER_B", "P1")],
        )
        r = self.upload("confirm", reconfirm)
        self.assertIn("重新确认 1 条", r.get_data(as_text=True))
        r = self.upload("ticket", TICKET_CSV_2)
        self.assertIn("新增 1 张", r.get_data(as_text=True))

        conn = get_db()
        groups = services.list_groups(conn)
        self.assertEqual(len(groups), 1)
        key2 = groups[0]["group_key"]
        self.assertEqual(len(groups[0]["verified_ticket_ids"]), 2)  # 两张已核对单锁定
        detail = services.get_group_detail(conn, key2)
        self.assertTrue(detail["options"])  # 仍有可行方案

        # 执行任一可行方案
        result = services.apply_plan(conn, key2, detail["options"][0]["confirmation_id"])
        # 校验：已核对单归属不变
        kept = {
            item["ticket"]: item["asset_no"]
            for part in result["partitions"]
            for item in part["items"]
        }
        self.assertEqual(kept["LEDGER_A/T100"], "AST-001")
        self.assertEqual(kept["LEDGER_B/T200"], "AST-002")
        self.assertEqual(kept["LEDGER_A/T300"], "AST-003")  # 新单唯一归属
        # 三张单全部在入仓清单/verified 中
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM verified").fetchone()["c"], 3
        )
        conn.close()

    def test_04_supplement_makes_no_solution(self):
        """补传后 4 张单抢 3 个资产号：任意单条撤销都无解，页面提示且不动数据。"""
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        self.upload("confirm", CONFIRM_CSV)

        r = self.upload("ticket", TICKET_CSV_2)
        self.assertIn("新增 1 张", r.get_data(as_text=True))
        r = self.upload("ticket", TICKET_CSV_3)
        self.assertIn("新增 1 张", r.get_data(as_text=True))

        conn = get_db()
        key = services.list_groups(conn)[0]["group_key"]
        detail = services.get_group_detail(conn, key)
        self.assertEqual(detail["options"], [])  # 无解
        self.assertFalse(detail["feasible"])

        # 直接尝试撤销组内任意确认，应被拒绝，且确认仍活动
        edge_id = detail["edge_ids"][0]
        with self.assertRaises(ValueError):
            services.apply_plan(conn, key, edge_id)
        self.assertEqual(
            conn.execute("SELECT active FROM confirmation WHERE id=?", (edge_id,)).fetchone()["active"],
            1,
        )
        conn.close()

        # 网页路径同样提示
        r = self.client.post(
            f"/groups/{key}/apply",
            data={"confirmation_id": edge_id},
            follow_redirects=True,
        )
        self.assertIn("未能撤销", r.get_data(as_text=True))

    def test_05_stale_selection_must_reselect(self):
        """补传确认改变组结构后，旧方案的确认已不在新组 -> 须重选。"""
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        self.upload("confirm", CONFIRM_CSV)
        conn = get_db()
        key = services.list_groups(conn)[0]["group_key"]
        edges = {
            e["id"]: (e["source_a"], e["pole_no_a"], e["source_b"], e["pole_no_b"])
            for e in conn.execute("SELECT * FROM confirmation").fetchall()
        }
        conn.close()

        # 先撤销其中一条
        conn = get_db()
        detail = services.get_group_detail(conn, key)
        gone = detail["options"][0]["confirmation_id"]
        services.apply_plan(conn, key, gone)
        conn.close()

        # 补传新确认，把另一根灯杆也并进 A/P1，形成新组
        extra = csv_bytes(
            "source_a,pole_no_a,source_b,pole_no_b",
            [("LEDGER_A", "P1", "LEDGER_A", "P2")],
        )
        self.upload("confirm", extra)
        conn = get_db()
        groups = services.list_groups(conn)
        self.assertTrue(groups)
        new_key = groups[0]["group_key"]
        # 旧确认仍处于撤销状态，对新组执行应报“须重新选择”
        with self.assertRaises(ValueError) as ctx:
            services.apply_plan(conn, new_key, gone)
        self.assertIn("重新选择", str(ctx.exception))
        # 数据保持原样
        self.assertEqual(
            conn.execute("SELECT active FROM confirmation WHERE id=?", (gone,)).fetchone()["active"],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM undo_plan").fetchone()["c"], 1
        )
        conn.close()

    def test_06_duplicate_source_numbers_are_distinct(self):
        """编号按来源区分：不同台账相同灯杆号/单号互不覆盖。"""
        assets = csv_bytes(
            "asset_no,pole_source,pole_no",
            [("AST-1", "LEDGER_A", "X1"), ("AST-2", "LEDGER_B", "X1")],
        )
        tickets = csv_bytes(
            "ticket_no,source,pole_source,pole_no",
            [
                ("T1", "LEDGER_A", "LEDGER_A", "X1"),
                ("T1", "LEDGER_B", "LEDGER_B", "X1"),  # 同号不同来源
            ],
        )
        confirms = csv_bytes(
            "source_a,pole_no_a,source_b,pole_no_b",
            [("LEDGER_A", "X1", "LEDGER_B", "X1")],
        )
        self.upload("asset", assets)
        r = self.upload("ticket", tickets)
        self.assertIn("新增 2 张", r.get_data(as_text=True))
        self.upload("confirm", confirms)
        conn = get_db()
        groups = services.list_groups(conn)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]["poles"]), {"LEDGER_A/X1", "LEDGER_B/X1"})
        self.assertEqual(len(groups[0]["ticket_ids"]), 2)
        conn.close()

    def test_07_ticket_supplement_upsert_and_bad_csv(self):
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        # 补传单号已存在 -> 更新而不是新增
        upd = csv_bytes(
            "ticket_no,source,pole_source,pole_no",
            [("T100", "LEDGER_A", "LEDGER_A", "P1")],
        )
        r = self.upload("ticket", upd)
        self.assertIn("更新 1 张", r.get_data(as_text=True))
        # 缺列文件整体拒收
        bad = csv_bytes("ticket_no,source", [("X", "LEDGER_A")])
        r = self.upload("ticket", bad)
        self.assertIn("上传失败", r.get_data(as_text=True))
        conn = get_db()
        # 坏文件没有写入任何东西
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM ticket").fetchone()["c"], 2
        )
        conn.close()

    def test_08_write_failure_keeps_original_relations(self):
        """模拟写入失败：事务回滚，确认仍活动、无入仓清单。"""
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        self.upload("confirm", CONFIRM_CSV)
        conn = get_db()
        key = services.list_groups(conn)[0]["group_key"]
        edge = services.get_group_detail(conn, key)["options"][0]["confirmation_id"]

        # 让事务中的写入抛错（触发器），验证回滚
        conn.execute(
            "CREATE TRIGGER fail_demo BEFORE INSERT ON undo_plan "
            "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
        with self.assertRaises(ValueError) as ctx:
            services.apply_plan(conn, key, edge)
        self.assertIn("写入数据库失败", str(ctx.exception))
        # 原关系和归属保留
        self.assertEqual(
            conn.execute("SELECT active FROM confirmation WHERE id=?", (edge,)).fetchone()["active"],
            1,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM verified").fetchone()["c"], 0
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM warehouse").fetchone()["c"], 0
        )
        conn.execute("DROP TRIGGER fail_demo")
        conn.close()

    def test_09_idempotent_confirm_upload(self):
        self.upload("confirm", CONFIRM_CSV)
        r = self.upload("confirm", CONFIRM_CSV)
        self.assertIn("已存在跳过 2 条", r.get_data(as_text=True))
        conn = get_db()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM confirmation").fetchone()["c"], 2
        )
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
