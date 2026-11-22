#!/usr/bin/env python3
"""本地演示：生成一批带历史时间戳的故障单，用于演示“周报到报估算”。

场景（全部相对 UTC 今天生成，立即可在页面上查看）：
- 台账 LEDGER_DEMO；建议截止日 = 昨天（页面默认允许的最大值）。
- 8 个参考周：每周 10 张周内即报、2 张周末后第 10 天迟到补报；
  最近的参考周另有 1 张跨周更正（先报到本周、第 20 天改到下一周）
  和 1 张反向更正（先报到下一周、第 20 天改回本周）。
- 待估周（最近一个已结束周）：9 张周内已报、1 张截止日后才补报
  （不影响已报数）、1 张始终缺发生日、1 张截止日后才补填发生日
  （截止日时仍属“缺发生日”）。

用法（数据库默认 ./data/recon.db，与 docker compose 挂载一致）：
    python scripts/load_demo.py            # 宿主机直接跑
    docker compose exec web python scripts/load_demo.py   # 容器内跑

幂等保护：已存在 LEDGER_DEMO 数据时直接退出，不重复写入。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import services  # noqa: E402
from app.db import get_db, init_db  # noqa: E402

SOURCE = "LEDGER_DEMO"


def csv_bytes(rows: list[tuple]) -> bytes:
    lines = ["ticket_no,source,pole_source,pole_no,occurred_on"]
    for r in rows:
        lines.append(",".join(str(c) for c in r))
    return ("\n".join(lines) + "\n").encode("utf-8")


def at(d: date, hour: int = 9) -> str:
    return datetime(d.year, d.month, d.day, hour).strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    conn = get_db()
    try:
        init_db(conn)
        exists = conn.execute(
            "SELECT COUNT(*) c FROM ticket WHERE source=?", (SOURCE,)
        ).fetchone()["c"]
    finally:
        conn.close()
    if exists:
        print(f"已存在 {exists} 张 {SOURCE} 故障单，演示数据不重复加载。")
        return

    today = date.today()  # 容器/宿主机时区均为 UTC 口径的演示
    cutoff = today - timedelta(days=1)
    sunday = services.week_sunday(cutoff)          # 待估周周日
    monday = sunday - timedelta(days=6)            # 待估周周一
    age = (cutoff - sunday).days

    seq = 0

    def no() -> str:
        nonlocal seq
        seq += 1
        return f"D{seq:04d}"

    def row(ticket_no: str, occ: date | None) -> tuple:
        return (ticket_no, SOURCE, SOURCE, "PX", occ.isoformat() if occ else "")

    # ---- 8 个参考周 ----
    for k in range(8):
        ref_sunday = sunday - timedelta(days=28 + 7 * k)
        ref_monday = ref_sunday - timedelta(days=6)
        # 10 张周内即报（周一入库）
        early = [row(no(), ref_monday + timedelta(days=i % 7)) for i in range(10)]
        services.import_tickets(csv_bytes(early), f"demo-w{k}-early.csv",
                                now=at(ref_monday))
        # 2 张迟到补报（周末后第 10 天入库，发生日仍在本周）
        late = [row(no(), ref_monday + timedelta(days=2)),
                row(no(), ref_monday + timedelta(days=5))]
        services.import_tickets(csv_bytes(late), f"demo-w{k}-late.csv",
                                now=at(ref_sunday + timedelta(days=10)))
        if k == 0:
            # 跨周更正：X 先报到本周，第 20 天改到下一周（离开本分母）
            x = no()
            services.import_tickets(csv_bytes([row(x, ref_monday)]),
                                    "demo-x-first.csv", now=at(ref_monday))
            services.import_tickets(
                csv_bytes([row(x, ref_monday + timedelta(days=7))]),
                "demo-x-fix.csv", now=at(ref_sunday + timedelta(days=20)))
            # 反向更正：Y 先报到下一周，第 20 天改回本周（第 28 天计入分母）
            y = no()
            services.import_tickets(
                csv_bytes([row(y, ref_monday + timedelta(days=7))]),
                "demo-y-first.csv", now=at(ref_monday))
            services.import_tickets(csv_bytes([row(y, ref_monday)]),
                                    "demo-y-fix.csv",
                                    now=at(ref_sunday + timedelta(days=20)))

    # ---- 待估周 ----
    services.import_tickets(
        csv_bytes([row(no(), monday + timedelta(days=i % 7)) for i in range(9)]),
        "demo-target-early.csv", now=at(monday + timedelta(days=1)))
    # 截止日后才补报的迟到单（不影响截止日已报数）
    services.import_tickets(csv_bytes([row(no(), monday + timedelta(days=3))]),
                            "demo-target-late.csv",
                            now=at(cutoff + timedelta(days=1)))
    # 始终缺发生日的单
    services.import_tickets(csv_bytes([row(no(), None)]), "demo-target-blank.csv",
                            now=at(monday + timedelta(days=1)))
    # 截止日后才补填发生日的单（截止日时仍属缺发生日）
    z = no()
    services.import_tickets(csv_bytes([row(z, None)]), "demo-target-z1.csv",
                            now=at(monday + timedelta(days=1)))
    services.import_tickets(csv_bytes([row(z, monday + timedelta(days=4))]),
                            "demo-target-z2.csv",
                            now=at(cutoff + timedelta(days=2)))

    print(f"演示数据已写入台账 {SOURCE}（共 {seq} 张单）。")
    print(f"打开 http://localhost:8000/forecast ，台账选 {SOURCE}，"
          f"截止日选 {cutoff}（昨天）。")
    print(f"待估周 {monday} ~ {sunday}（周龄 {age} 天），预期：已报 9 张，"
          f"缺发生日另计 2 张；参考周分子合计 80、分母合计 97，"
          f"到报比例约 82.5%，第 28 天预计约 10.9 张。")


if __name__ == "__main__":
    main()
