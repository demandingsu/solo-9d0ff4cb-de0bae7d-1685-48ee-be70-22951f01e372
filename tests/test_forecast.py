"""周报到报估算测试（Flask test client + 临时 SQLite）。

覆盖：
1. 发生日可选列：新建/留空沿用/更正/非法日期拒收/老格式 CSV 兼容；
2. 发生日历史事件：首次入库与每次补填/更正各记一条，仅改灯杆不记；
3. 估算主场景（固定日期）：迟到补报进分母不进分子、跨周更正双向、
   截止日后补报不计入已报、缺发生日另计、截止日后补填仍属缺发生日；
4. 分母为零、比例为零时只给已报数与原因；
5. 整次估算在单个读事务内完成（同一份已提交数据）；
6. 老库迁移：无事件的存量单补初始事件；
7. 网页路由：表单、结果展示、截止日校验。
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from datetime import date, timedelta

_tmp = tempfile.mkdtemp(prefix="pole-forecast-test-")
os.environ["POLE_RECON_DB"] = os.path.join(_tmp, "test.db")

from app import services  # noqa: E402
from app.db import get_db, init_db  # noqa: E402
from app.routes import app  # noqa: E402

SRC = "LEDGER_F"


def csv_bytes(rows, header="ticket_no,source,pole_source,pole_no,occurred_on"):
    lines = [header] + [",".join(str(c) for c in r) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def at(d: date, hour: int = 9) -> str:
    """模拟事件时间（UTC）。"""
    return f"{d.isoformat()} {hour:02d}:00:00"


def row(no, occ, source=SRC):
    return (no, source, source, "PX", occ.isoformat() if occ else "")


def build_main_scenario() -> date:
    """固定日期主场景：待估周 2026-11-02~11-08，截止日 2026-11-10（周龄 2）。

    8 个参考周：每周 10 张周内即报 + 2 张周末后第 10 天迟到补报；
    最近参考周另有 X 跨周更正出（第 20 天改到下一周）、Y 跨周更正入。
    待估周：9 张周内已报、1 张截止日后才补报、1 张始终缺发生日、
    1 张截止日后才补填发生日。返回截止日。
    """
    cutoff = date(2026, 11, 10)
    sunday = date(2026, 11, 8)
    monday = date(2026, 11, 2)
    seq = 0

    def no() -> str:
        nonlocal seq
        seq += 1
        return f"F{seq:04d}"

    for k in range(8):
        ref_sunday = sunday - timedelta(days=28 + 7 * k)
        ref_monday = ref_sunday - timedelta(days=6)
        early = [row(no(), ref_monday + timedelta(days=i % 7)) for i in range(10)]
        services.import_tickets(csv_bytes(early), f"w{k}e.csv", now=at(ref_monday))
        late = [
            row(no(), ref_monday + timedelta(days=2)),
            row(no(), ref_monday + timedelta(days=5)),
        ]
        services.import_tickets(
            csv_bytes(late), f"w{k}l.csv", now=at(ref_sunday + timedelta(days=10))
        )
        if k == 0:
            x = no()  # 先报到本周，第 20 天更正到下一周 -> 离开本分母
            services.import_tickets(csv_bytes([row(x, ref_monday)]), "x1.csv",
                                    now=at(ref_monday))
            services.import_tickets(
                csv_bytes([row(x, ref_monday + timedelta(days=7))]), "x2.csv",
                now=at(ref_sunday + timedelta(days=20)))
            y = no()  # 先报到下一周，第 20 天更正回本周 -> 第 28 天进分母
            services.import_tickets(
                csv_bytes([row(y, ref_monday + timedelta(days=7))]), "y1.csv",
                now=at(ref_monday))
            services.import_tickets(csv_bytes([row(y, ref_monday)]), "y2.csv",
                                    now=at(ref_sunday + timedelta(days=20)))

    services.import_tickets(
        csv_bytes([row(no(), monday + timedelta(days=i % 7)) for i in range(9)]),
        "te.csv", now=at(monday + timedelta(days=1)))
    services.import_tickets(
        csv_bytes([row(no(), monday + timedelta(days=3))]), "tl.csv",
        now=at(cutoff + timedelta(days=1)))  # 截止日后才补报
    services.import_tickets(csv_bytes([row(no(), None)]), "tb.csv",
                            now=at(monday + timedelta(days=1)))  # 始终缺发生日
    z = no()  # 截止日后才补填发生日 -> 截止日时仍属缺发生日
    services.import_tickets(csv_bytes([row(z, None)]), "tz1.csv",
                            now=at(monday + timedelta(days=1)))
    services.import_tickets(csv_bytes([row(z, monday + timedelta(days=4))]),
                            "tz2.csv", now=at(cutoff + timedelta(days=2)))
    return cutoff


class CountingConn:
    """连接代理：统计 BEGIN 次数与事务内 SELECT 数，验证单事务快照。"""

    def __init__(self, conn):
        self._conn = conn
        self.begins = 0
        self.selects_in_tx = 0
        self.commits = 0
        self._in_tx = False

    def execute(self, sql, parameters=()):
        head = sql.strip().split(None, 1)[0].upper()
        if head == "BEGIN":
            self.begins += 1
            self._in_tx = True
        elif head == "SELECT" and self._in_tx:
            self.selects_in_tx += 1
        return self._conn.execute(sql, parameters)

    def commit(self):
        self.commits += 1
        self._in_tx = False
        self._conn.commit()

    def rollback(self):
        self._in_tx = False
        self._conn.rollback()


class ForecastTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()

    def setUp(self):
        conn = get_db()
        init_db(conn)
        for table in (
            "warehouse_item", "warehouse", "undo_plan", "verified",
            "ticket_event", "confirmation", "ticket", "asset", "batch",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------ #
    def test_01_occurred_on_import_flow(self):
        # 新建带发生日
        msg = services.import_tickets(csv_bytes([row("K1", date(2026, 11, 4))]), "a.csv")
        self.assertIn("新增 1 张", msg)
        conn = get_db()
        self.assertEqual(
            conn.execute("SELECT occurred_on FROM ticket WHERE ticket_no='K1'")
            .fetchone()["occurred_on"],
            "2026-11-04",
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM ticket_event").fetchone()["c"], 1
        )
        # 留空沿用已知日期，不记事件
        msg = services.import_tickets(csv_bytes([("K1", SRC, SRC, "PY", "")]), "b.csv")
        self.assertIn("更新 1 张", msg)
        self.assertNotIn("发生日", msg)
        self.assertEqual(
            conn.execute("SELECT occurred_on FROM ticket WHERE ticket_no='K1'")
            .fetchone()["occurred_on"],
            "2026-11-04",
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM ticket_event").fetchone()["c"], 1
        )
        # 更正发生日 -> 记事件并提示
        msg = services.import_tickets(
            csv_bytes([("K1", SRC, SRC, "PY", "2026-11-06")]), "c.csv"
        )
        self.assertIn("发生日补填/更正 1 张", msg)
        self.assertEqual(
            conn.execute("SELECT occurred_on FROM ticket WHERE ticket_no='K1'")
            .fetchone()["occurred_on"],
            "2026-11-06",
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM ticket_event").fetchone()["c"], 2
        )
        # 仅改灯杆（发生日留空）-> 不记事件
        services.import_tickets(csv_bytes([("K1", SRC, SRC, "PZ", "")]), "d.csv")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM ticket_event").fetchone()["c"], 2
        )
        # 非法发生日 -> 整文件拒收，什么都不写
        with self.assertRaises(ValueError):
            services.import_tickets(
                csv_bytes([("K2", SRC, SRC, "PX", "2026-13-40")]), "e.csv"
            )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM ticket WHERE ticket_no='K2'")
            .fetchone()["c"],
            0,
        )
        # 无 occurred_on 列的老格式 CSV 仍可用
        msg = services.import_tickets(
            csv_bytes(
                [("K3", SRC, SRC, "PX")],
                header="ticket_no,source,pole_source,pole_no",
            ),
            "f.csv",
        )
        self.assertIn("新增 1 张", msg)
        self.assertIsNone(
            conn.execute("SELECT occurred_on FROM ticket WHERE ticket_no='K3'")
            .fetchone()["occurred_on"]
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM ticket_event").fetchone()["c"], 3
        )
        conn.close()

    # ------------------------------------------------------------------ #
    def test_02_estimate_main_scenario(self):
        cutoff = build_main_scenario()
        conn = get_db()
        try:
            r = services.estimate_weekly(conn, SRC, cutoff)
        finally:
            conn.close()
        self.assertEqual((r["monday"], r["sunday"]), (date(2026, 11, 2), date(2026, 11, 8)))
        self.assertEqual(r["age"], 2)
        self.assertEqual(r["reported"], 9)   # 截止日后补报的不计
        self.assertEqual(r["missing"], 2)    # 始终缺发生日 + 截止日后才补填
        self.assertEqual(r["numer_total"], 80)
        self.assertEqual(r["denom_total"], 97)  # 7×12 + 13（含跨周更正入的 Y）
        self.assertAlmostEqual(r["ratio"], 80 / 97)
        self.assertAlmostEqual(r["estimate"], 9 * 97 / 80)
        self.assertIsNone(r["reason"])
        # 最近参考周：X 更正出、Y 更正入 -> 分子 10、分母 13
        self.assertEqual(
            (r["ref_weeks"][0]["numerator"], r["ref_weeks"][0]["denominator"]),
            (10, 13),
        )
        for k, w in enumerate(r["ref_weeks"]):
            self.assertEqual(w["sunday"], date(2026, 11, 8) - timedelta(days=28 + 7 * k))
            self.assertEqual(w["age"], 2 + 28 + 7 * k)
        for w in r["ref_weeks"][1:]:
            self.assertEqual((w["numerator"], w["denominator"]), (10, 12))

    # ------------------------------------------------------------------ #
    def test_03_zero_denominator_only_reported_and_reason(self):
        cutoff = date(2026, 11, 10)
        monday = date(2026, 11, 2)
        services.import_tickets(
            csv_bytes([row("A1", monday), row("A2", monday + timedelta(days=1))]),
            "only.csv", now=at(monday),
        )
        conn = get_db()
        try:
            r = services.estimate_weekly(conn, SRC, cutoff)
        finally:
            conn.close()
        self.assertEqual(r["reported"], 2)
        self.assertEqual(r["denom_total"], 0)
        self.assertIsNone(r["ratio"])
        self.assertIsNone(r["estimate"])
        self.assertIn("第 28 天", r["reason"])

    # ------------------------------------------------------------------ #
    def test_04_zero_ratio_only_reported_and_reason(self):
        cutoff = date(2026, 11, 10)
        sunday = date(2026, 11, 8)
        monday = date(2026, 11, 2)
        for k in range(8):
            ref_sunday = sunday - timedelta(days=28 + 7 * k)
            ref_monday = ref_sunday - timedelta(days=6)
            # 全部在周末后第 10 天才补报 -> 相同周龄（2 天）时都不在 -> 分子 0
            late = [row(f"L{k}{i}", ref_monday + timedelta(days=1)) for i in range(3)]
            services.import_tickets(
                csv_bytes(late), f"z{k}.csv", now=at(ref_sunday + timedelta(days=10))
            )
        services.import_tickets(csv_bytes([row("T1", monday)]), "t.csv", now=at(monday))
        conn = get_db()
        try:
            r = services.estimate_weekly(conn, SRC, cutoff)
        finally:
            conn.close()
        self.assertEqual(r["reported"], 1)
        self.assertEqual(r["denom_total"], 24)
        self.assertEqual(r["numer_total"], 0)
        self.assertIsNone(r["estimate"])
        self.assertIn("比例为 0", r["reason"])

    # ------------------------------------------------------------------ #
    def test_05_single_transaction_snapshot(self):
        cutoff = build_main_scenario()
        conn = get_db()
        proxy = CountingConn(conn)
        r = services.estimate_weekly(proxy, SRC, cutoff)
        in_tx_after = conn.in_transaction
        conn.close()
        self.assertEqual(r["reported"], 9)
        self.assertEqual(proxy.begins, 1)          # 整次估算一个读事务
        self.assertEqual(proxy.selects_in_tx, 17)  # 1 待估周 + 8×2 参考周
        self.assertEqual(proxy.commits, 1)
        self.assertFalse(in_tx_after)

    # ------------------------------------------------------------------ #
    def test_06_migration_backfills_events(self):
        conn = get_db()
        init_db(conn)
        conn.execute(
            "INSERT INTO ticket(source, ticket_no, pole_source, pole_no, occurred_on) "
            "VALUES ('LEDGER_M', 'M1', 'LEDGER_M', 'P1', '2026-11-01')"
        )
        conn.commit()
        init_db(conn)  # 迁移：为无事件的存量单补初始事件
        row_ = conn.execute(
            "SELECT e.occurred_on FROM ticket_event e "
            "JOIN ticket t ON t.id=e.ticket_id WHERE t.ticket_no='M1'"
        ).fetchone()
        self.assertIsNotNone(row_)
        self.assertEqual(row_["occurred_on"], "2026-11-01")
        conn.close()

    # ------------------------------------------------------------------ #
    def test_07_route_form_and_cutoff_validation(self):
        r = self.client.get("/forecast")
        self.assertEqual(r.status_code, 200)
        self.assertIn("周报到报估算", r.get_data(as_text=True))
        # 截止日为今天（未过去）-> 拒绝
        today = date.today().isoformat()
        r = self.client.get(f"/forecast?source={SRC}&cutoff={today}")
        self.assertIn("已过去", r.get_data(as_text=True))
        # 非法格式
        r = self.client.get(f"/forecast?source={SRC}&cutoff=abc")
        self.assertIn("格式不正确", r.get_data(as_text=True))

    # ------------------------------------------------------------------ #
    def test_08_route_result_page(self):
        today = date.today()
        cutoff = today - timedelta(days=1)
        sunday = services.week_sunday(cutoff)
        monday = sunday - timedelta(days=6)
        rows = [row(f"R{i}", monday + timedelta(days=i)) for i in range(3)]
        services.import_tickets(csv_bytes(rows), "r.csv", now=at(monday))
        r = self.client.get(f"/forecast?source={SRC}&cutoff={cutoff.isoformat()}")
        text = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("已报单数", text)
        self.assertIn("<b>3</b>", text)
        # 无参考周数据 -> 只给已报数和原因
        self.assertIn("暂无法给出预计数", text)
        self.assertIn("缺发生日", text)

    # ------------------------------------------------------------------ #
    def test_09_route_upload_with_occurred_on(self):
        content = csv_bytes([("U1", SRC, SRC, "PX", "2026-11-04")])
        r = self.client.post(
            "/upload",
            data={"kind": "ticket", "file": (io.BytesIO(content), "u.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("新增 1 张", r.get_data(as_text=True))
        conn = get_db()
        self.assertEqual(
            conn.execute("SELECT occurred_on FROM ticket WHERE ticket_no='U1'")
            .fetchone()["occurred_on"],
            "2026-11-04",
        )
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
