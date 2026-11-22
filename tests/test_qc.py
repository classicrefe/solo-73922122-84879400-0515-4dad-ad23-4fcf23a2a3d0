"""质检流程：取数标记、坏点隔离、修测解除、标记失效。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import init_map, stage_tile, tile_grid


def qc_window(client, row0, col0, row1, col1):
    r = client.post("/qc/windows",
                    json={"row0": row0, "col0": col0,
                          "row1": row1, "col1": col1})
    assert r.status_code == 201, r.text
    return r.json()


def submit(client, mark, points):
    return client.post(
        "/qc/isolations",
        json={"mark": mark, "points": [{"row": r, "col": c} for r, c in points]})


def window_cell(client, row, col):
    r = client.get("/basemap/window",
                   params={"row0": row, "col0": col, "row1": row, "col1": col})
    assert r.status_code == 200, r.text
    return r.json()["grid"][0][0]


def isolated_points(client):
    r = client.get("/qc/isolations")
    assert r.status_code == 200, r.text
    return {(p["row"], p["col"]) for p in r.json()["points"]}


# ---- 质检取窗 ------------------------------------------------------------

def test_qc_window_returns_raw_elevations_and_mark(client):
    init_map(client, 1, 1, 100)
    body = qc_window(client, 0, 0, 16, 16)
    assert isinstance(body["mark"], str) and body["mark"]
    assert body["grid"] == tile_grid(100)  # 原高程原样返回
    assert body["fetch_seq"] == 0
    # 每次取窗签发新标记
    assert qc_window(client, 0, 0, 16, 16)["mark"] != body["mark"]


def test_qc_window_out_of_range(client):
    init_map(client, 1, 1, 100)
    r = client.post("/qc/windows",
                    json={"row0": 0, "col0": 0, "row1": 17, "col1": 0})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "window_out_of_range"


def test_qc_ops_require_basemap(client):
    r = client.post("/qc/windows",
                    json={"row0": 0, "col0": 0, "row1": 1, "col1": 1})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "no_basemap"
    assert client.get("/qc/isolations").status_code == 409


# ---- 隔离生效 ------------------------------------------------------------

def test_isolation_masks_window_api_only(client):
    init_map(client, 1, 1, 100)
    mark = qc_window(client, 0, 0, 16, 16)["mark"]
    r = submit(client, mark, [(3, 3)])
    assert r.status_code == 200, r.text
    assert r.json()["newly_isolated"] == [[3, 3]]
    assert r.json()["total_isolated"] == 1
    assert isolated_points(client) == {(3, 3)}

    assert window_cell(client, 3, 3) is None   # 原窗口 API 在隔离点返回 null
    assert window_cell(client, 4, 4) == 100
    # 单块与整图取数不遮蔽（修测、核对仍需要原值）
    assert client.get("/tiles/0/0").json()["data"][3][3] == 100
    assert client.get("/basemap/grid").json()["grid"][3][3] == 100
    # 质检取窗返回原高程，不受隔离影响
    assert qc_window(client, 3, 3, 3, 3)["grid"] == [[100]]


def test_shared_edge_point_treated_as_one(client):
    init_map(client, 1, 2, 100)  # 共边在整图第 16 列
    mark = qc_window(client, 0, 0, 16, 32)["mark"]
    # 同一提交内重复给出共边点，只隔离一次
    r = submit(client, mark, [(5, 16), (5, 16)])
    assert r.status_code == 200, r.text
    assert r.json()["newly_isolated"] == [[5, 16]]
    assert isolated_points(client) == {(5, 16)}
    assert window_cell(client, 5, 16) is None
    # 跨提交重复标记同一读数：仍只隔离一次
    r = submit(client, mark, [(5, 16)])
    assert r.status_code == 200, r.text
    assert r.json()["newly_isolated"] == []
    assert r.json()["already_isolated"] == [[5, 16]]
    assert r.json()["total_isolated"] == 1
    assert isolated_points(client) == {(5, 16)}


def test_points_out_of_window_rejected(client):
    init_map(client, 1, 1, 100)
    mark = qc_window(client, 0, 0, 8, 8)["mark"]
    r = submit(client, mark, [(3, 3), (10, 10)])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "points_out_of_window"
    assert detail["out_of_window"] == [[10, 10]]
    assert isolated_points(client) == set()  # 一个点都不隔离


# ---- 取数后被改过：整单退回 ----------------------------------------------

def test_stale_window_rejected_even_if_changed_back(client):
    init_map(client, 1, 1, 100)
    mark = qc_window(client, 0, 0, 16, 16)["mark"]

    t = tile_grid(100)
    t[3][3] = 5
    stage_tile(client, 0, 0, t)
    assert client.post("/publish", json={"row": 0, "col": 0}).status_code == 200
    stage_tile(client, 0, 0, tile_grid(100))  # 改回原值再发版
    assert client.post("/publish", json={"row": 0, "col": 0}).status_code == 200
    assert window_cell(client, 3, 3) == 100  # 值确实回到了原值

    r = submit(client, mark, [(8, 8)])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "window_stale"
    assert [3, 3] in detail["changed_points"]
    assert isolated_points(client) == set()  # 整单退回，无一隔离


def test_publish_outside_window_does_not_block(client):
    init_map(client, 1, 1, 100)
    mark = qc_window(client, 0, 0, 8, 8)["mark"]  # 窗口只覆盖左上角
    t = tile_grid(100)
    t[12][12] = 7  # 只改窗口外的点
    stage_tile(client, 0, 0, t)
    assert client.post("/publish", json={"row": 0, "col": 0}).status_code == 200
    r = submit(client, mark, [(3, 3)])
    assert r.status_code == 200, r.text
    assert isolated_points(client) == {(3, 3)}


def test_fetch_after_change_then_submit_ok(client):
    """取数之后没再动过窗口：提交成功（对照组）。"""
    init_map(client, 1, 1, 100)
    t = tile_grid(100)
    t[3][3] = 5
    stage_tile(client, 0, 0, t)
    assert client.post("/publish", json={"row": 0, "col": 0}).status_code == 200
    mark = qc_window(client, 0, 0, 16, 16)["mark"]  # 变更后重新取数
    assert submit(client, mark, [(3, 3)]).status_code == 200
    assert window_cell(client, 3, 3) is None


# ---- 修测解除隔离 ----------------------------------------------------------

def test_isolation_lifted_only_when_value_actually_changes(client):
    init_map(client, 1, 1, 100)
    mark = qc_window(client, 0, 0, 16, 16)["mark"]
    submit(client, mark, [(3, 3), (4, 4)])

    t = tile_grid(100)
    t[3][3] = 555  # 只真正改变 (3,3)
    stage_tile(client, 0, 0, t)
    r = client.post("/publish", json={"row": 0, "col": 0})
    assert r.status_code == 200, r.text
    assert r.json()["lifted_isolations"] == [[3, 3]]

    assert window_cell(client, 3, 3) == 555   # 修测后恢复供数
    assert window_cell(client, 4, 4) is None  # 未实际改变的点保持隔离
    assert isolated_points(client) == {(4, 4)}


def test_publish_without_actual_change_keeps_isolation(client):
    """发版换上的版本与现行完全一致（无实际改变）：隔离保持。"""
    init_map(client, 1, 1, 100)
    mark = qc_window(client, 0, 0, 16, 16)["mark"]
    submit(client, mark, [(3, 3)])
    stage_tile(client, 0, 0, tile_grid(100))  # 与现行版完全相同
    r = client.post("/publish", json={"row": 0, "col": 0})
    assert r.status_code == 200, r.text
    assert r.json()["lifted_isolations"] == []
    assert window_cell(client, 3, 3) is None
    assert isolated_points(client) == {(3, 3)}


def test_failed_publish_keeps_isolation(client):
    init_map(client, 1, 2, 100)
    mark = qc_window(client, 0, 0, 16, 16)["mark"]
    submit(client, mark, [(3, 3)])

    t = tile_grid(100)
    for row in t:
        row[16] = 200  # 右共边改值而邻块无暂存 -> 发版冲突
    t[3][3] = 555
    stage_tile(client, 0, 0, t)
    r = client.post("/publish", json={"row": 0, "col": 0})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "publish_conflict"
    # 失败的发版保留原状态：隔离不解除、底图不变
    assert window_cell(client, 3, 3) is None
    assert isolated_points(client) == {(3, 3)}
    assert client.get("/tiles/0/0").json()["data"][3][3] == 100


# ---- 标记生命周期 ----------------------------------------------------------

def test_unknown_mark_rejected(client):
    init_map(client, 1, 1, 100)
    r = submit(client, "no-such-mark", [(3, 3)])
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "mark_not_found"


def test_reset_invalidates_marks_and_isolations(client):
    init_map(client, 1, 1, 100)
    mark = qc_window(client, 0, 0, 16, 16)["mark"]
    assert submit(client, mark, [(3, 3)]).status_code == 200

    assert client.delete("/basemap").status_code == 200
    init_map(client, 1, 1, 100)  # 重录底图

    assert isolated_points(client) == set()  # 旧隔离一并清除
    r = submit(client, mark, [(5, 5)])       # 旧标记失效
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "mark_not_found"


def test_mark_survives_restart(tmp_path):
    """标记持久化在 SQLite：模拟重启（同一库文件重新起服务）后仍可用。"""
    db = str(tmp_path / "restart.db")
    with TestClient(create_app(db)) as c1:
        init_map(c1, 1, 1, 100)
        mark = qc_window(c1, 0, 0, 16, 16)["mark"]
    with TestClient(create_app(db)) as c2:
        r = submit(c2, mark, [(3, 3)])
        assert r.status_code == 200, r.text
        assert window_cell(c2, 3, 3) is None
