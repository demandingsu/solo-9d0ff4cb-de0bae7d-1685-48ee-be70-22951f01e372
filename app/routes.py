"""Flask 路由：上传、冲突组列表、方案选择与复算撤销、入仓清单查看。"""
from __future__ import annotations

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import services
from .db import get_db, init_db

app = Flask(__name__)
app.secret_key = "recon-secret-key-change-in-prod"


@app.before_request
def ensure_schema():
    conn = get_db()
    try:
        init_db(conn)
    finally:
        conn.close()


@app.route("/")
def index():
    conn = get_db()
    try:
        batches = conn.execute(
            "SELECT * FROM batch ORDER BY id DESC LIMIT 20"
        ).fetchall()
        stats = {
            "assets": conn.execute("SELECT COUNT(*) c FROM asset").fetchone()["c"],
            "tickets": conn.execute("SELECT COUNT(*) c FROM ticket").fetchone()["c"],
            "active_confirms": conn.execute(
                "SELECT COUNT(*) c FROM confirmation WHERE active=1"
            ).fetchone()["c"],
            "verified": conn.execute("SELECT COUNT(*) c FROM verified").fetchone()[
                "c"
            ],
            "warehouses": conn.execute(
                "SELECT COUNT(*) c FROM warehouse"
            ).fetchone()["c"],
        }
        groups = services.list_groups(conn)
        return render_template("index.html", batches=batches, stats=stats, groups=groups)
    finally:
        conn.close()


@app.route("/upload", methods=["POST"])
def upload():
    kind = request.form.get("kind", "")
    file = request.files.get("file")
    if file is None or not file.filename:
        flash("请选择要上传的 CSV 文件", "error")
        return redirect(url_for("index"))
    raw = file.read()
    if not raw.strip():
        flash("上传文件为空", "error")
        return redirect(url_for("index"))
    importers = {
        "asset": services.import_assets,
        "ticket": services.import_tickets,
        "confirm": services.import_confirmations,
    }
    if kind not in importers:
        flash("未知上传类型", "error")
        return redirect(url_for("index"))
    try:
        msg = importers[kind](raw, file.filename)
        flash(msg, "ok")
    except ValueError as exc:
        flash(f"上传失败：{exc}（本次文件未写入任何数据）", "error")
    return redirect(url_for("index"))


@app.route("/groups/<group_key>")
def group_detail(group_key: str):
    conn = get_db()
    try:
        view = services.get_group_detail(conn, group_key)
        if view is None:
            flash("冲突组不存在，可能已因最新资料重新分组，请回到首页重选", "error")
            return redirect(url_for("index"))
        return render_template("group.html", view=view)
    finally:
        conn.close()


@app.route("/groups/<group_key>/apply", methods=["POST"])
def apply_plan(group_key: str):
    try:
        confirmation_id = int(request.form.get("confirmation_id", ""))
    except ValueError:
        flash("请先选择一条撤销方案", "error")
        return redirect(url_for("group_detail", group_key=group_key))
    conn = get_db()
    try:
        result = services.apply_plan(conn, group_key, confirmation_id)
    except ValueError as exc:
        # 条件不符或写入失败：原关系和归属保留
        flash(f"未能撤销：{exc}", "error")
        return redirect(url_for("group_detail", group_key=group_key))
    finally:
        conn.close()
    flash(
        "已按最新资料复算并撤销该同杆确认，组内每张单都有唯一资产号，整组入仓清单已生成",
        "ok",
    )
    return redirect(url_for("warehouse_view", warehouse_id=result["warehouse_id"]))


@app.route("/warehouse/<int:warehouse_id>")
def warehouse_view(warehouse_id: int):
    conn = get_db()
    try:
        wh = conn.execute(
            "SELECT * FROM warehouse WHERE id=?", (warehouse_id,)
        ).fetchone()
        if wh is None:
            flash("入仓清单不存在", "error")
            return redirect(url_for("index"))
        items = conn.execute(
            "SELECT t.source, t.ticket_no, t.pole_source, t.pole_no, a.asset_no, "
            "v.verified_at, wi.ticket_id IN (SELECT ticket_id FROM verified) AS is_verified "
            "FROM warehouse_item wi "
            "JOIN ticket t ON t.id=wi.ticket_id "
            "JOIN asset a ON a.id=wi.asset_id "
            "LEFT JOIN verified v ON v.ticket_id=wi.ticket_id "
            "WHERE wi.warehouse_id=? ORDER BY t.source, t.ticket_no",
            (warehouse_id,),
        ).fetchall()
        return render_template("warehouse.html", wh=wh, items=items)
    finally:
        conn.close()
