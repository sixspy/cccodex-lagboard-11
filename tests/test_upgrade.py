"""等级升级写路径测试：告警升违约连坐改态（甲路径：同库一次提交，遇错全撤回）。

红线：
- 升级失败测：故障注入后事务全部撤回，目录仍降级只读，到达主键不丢，账本无成功。
- 抢升失败测：双端同点同一源升级须占升级锁，后交只记“等级升级争用”留痕。
"""
import threading

import pytest
from fastapi.testclient import TestClient

import app as appmod
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.UPGRADE_FAULT", False, raising=False)
    monkeypatch.setattr(appmod, "UPGRADE_LOCK_HELD_HOOK", lambda: None)
    init_db()
    yield


def _client():
    c = TestClient(app)
    tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return c, {"Authorization": "Bearer " + tok}


def _arrival(c, h, arrival_id):
    return next(a for a in c.get("/api/arrivals", headers=h).json()["items"] if a["id"] == arrival_id)


def _alert_arrival(c, h):
    return next(a for a in c.get("/api/arrivals", headers=h).json()["items"] if a["level"] == "告警")


def _breach_arrival(c, h):
    return next(a for a in c.get("/api/arrivals", headers=h).json()["items"] if a["level"] == "违约")


def _source(c, h, name):
    return next(s for s in c.get("/api/sources", headers=h).json()["items"] if s["name"] == name)


def _ledger(c, h):
    return c.get("/api/upgrade-ledger", headers=h).json()["items"]


def test_upgrade_failure_rolls_back_and_directory_stays_readonly(monkeypatch):
    """升级失败测：甲路径遇错全部撤回——目录仍只读须红（实现前无此写路径，本测必红）。"""
    c, h = _client()
    alert = _alert_arrival(c, h)
    monkeypatch.setattr("app.UPGRADE_FAULT", True, raising=False)  # 在同事务提交前注入故障

    r = c.post(f"/api/arrivals/{alert['id']}/upgrade", headers=h)

    assert r.status_code == 500
    # 到达主键仍在、等级未被改口
    row = _arrival(c, h, alert["id"])
    assert row["id"] == alert["id"] and row["level"] == "告警"
    # 连坐改态随事务撤回，源目录仍降级只读
    assert _source(c, h, alert["source"])["output_mode"] == "降级只读"
    # 账本不得留下成功记录
    assert all(x["result"] != "成功" for x in _ledger(c, h))


def test_concurrent_upgrade_lock_contention_leaves_trace(monkeypatch):
    """抢升失败测：双路同时点同一源升级须占升级锁，后交只记等级升级争用，缺留痕则红。"""
    import time
    c, h = _client()
    alert = _alert_arrival(c, h)
    url = f"/api/arrivals/{alert['id']}/upgrade"
    entered = threading.Event()
    release = threading.Event()

    def held_hook():
        # 赢方进入持锁关键区后挂起，保证后交请求一定落在锁被持有窗口内
        entered.set()
        release.wait(timeout=5)

    monkeypatch.setattr(appmod, "UPGRADE_LOCK_HELD_HOOK", held_hook)
    statuses, texts = [], []

    def worker():
        cc, hh = _client()
        rr = cc.post(url, headers=hh)
        statuses.append(rr.status_code)
        texts.append(rr.text)

    tw = threading.Thread(target=worker)
    tw.start()
    assert entered.wait(timeout=5)  # 等赢方拿锁并进入关键区
    # 后交此时必撞升级锁
    cl, hl = _client()
    r_loser = cl.post(url, headers=hl)
    assert r_loser.status_code == 409 and "等级升级争用" in r_loser.text
    statuses.append(r_loser.status_code)
    texts.append(r_loser.text)
    release.set()
    tw.join(timeout=5)

    assert sorted(statuses) == [200, 409]
    assert any("等级升级争用" in t for t in texts)

    ledger = _ledger(c, h)
    results = [x["result"] for x in ledger]
    assert results.count("成功") == 1
    assert results.count("等级升级争用") == 1
    # 赢方真实连坐：等级违约、目录冻结，主键未丢
    assert _arrival(c, h, alert["id"])["level"] == "违约"
    assert _source(c, h, alert["source"])["output_mode"] == "冻结"
    # 锁必须释放，后续请求不再被争用拦截（而是按已违约判升级偏了）
    time.sleep(0.05)
    r_again = c.post(url, headers=h)
    assert r_again.status_code == 409 and "升级偏了" in r_again.text


def test_upgrade_happy_path_links_level_and_output_mode():
    """告警升违约：等级与源输出态同库一次提交，五处数据源同态。"""
    c, h = _client()
    alert = _alert_arrival(c, h)

    r = c.post(f"/api/arrivals/{alert['id']}/upgrade", headers=h)

    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "成功" and body["arrival_id"] == alert["id"]
    assert body["from_level"] == "告警" and body["to_level"] == "违约"
    assert body["output_mode"] == "冻结"

    row = _arrival(c, h, alert["id"])
    assert row["id"] == alert["id"] and row["level"] == "违约"
    assert _source(c, h, alert["source"])["output_mode"] == "冻结"

    ledger = _ledger(c, h)
    win = next(x for x in ledger if x["result"] == "成功")
    assert win["arrival_id"] == alert["id"] and win["source"] == alert["source"]
    assert win["from_level"] == "告警" and win["to_level"] == "违约"


def test_reupgrade_same_arrival_is_rejected_as_deviation():
    """对已违约到达再升级：偏离升级说明，驳回并留“升级偏了”，不改态。"""
    c, h = _client()
    alert = _alert_arrival(c, h)
    assert c.post(f"/api/arrivals/{alert['id']}/upgrade", headers=h).status_code == 200

    r = c.post(f"/api/arrivals/{alert['id']}/upgrade", headers=h)
    assert r.status_code == 409 and "升级偏了" in r.text
    assert _source(c, h, alert["source"])["output_mode"] == "冻结"
    assert any(x["result"] == "升级偏了" for x in _ledger(c, h))


def test_upgrade_non_alert_arrival_is_deviation():
    """违约到达不是告警，不在升级说明许可范围，判升级偏了。"""
    c, h = _client()
    breach = _breach_arrival(c, h)
    r = c.post(f"/api/arrivals/{breach['id']}/upgrade", headers=h)
    assert r.status_code == 409 and "升级偏了" in r.text
    assert _source(c, h, breach["source"])["output_mode"] == "冻结"


def test_unknown_arrival_id_404():
    c, h = _client()
    r = c.post("/api/arrivals/9999/upgrade", headers=h)
    assert r.status_code == 404


def test_upgrade_requires_login():
    c = TestClient(app)
    r = c.post("/api/arrivals/1/upgrade")
    assert r.status_code == 401


def test_five_views_are_homomorphic_and_frontend_exposes_all_views():
    """五处须同态可读：到达详情、源目录、策略附页、水位卡片、审计列表。"""
    c, h = _client()
    alert = _alert_arrival(c, h)
    assert c.post(f"/api/arrivals/{alert['id']}/upgrade", headers=h).status_code == 200

    # 后端三处数据同源：到达详情=违约、源目录=冻结、审计列表含成功留痕
    assert _arrival(c, h, alert["id"])["level"] == "违约"
    assert _source(c, h, alert["source"])["output_mode"] == "冻结"
    ledger = _ledger(c, h)
    assert any(x["result"] == "成功" and x["arrival_id"] == alert["id"] for x in ledger)
    # 到达主键在账本中可回溯，未被升级弄丢
    assert {x["arrival_id"] for x in ledger} >= {alert["id"]}

    # 前端五处视图均有落点（仅改徽章颜色、缺任一视图即红）
    html = (appmod.FRONTEND / "index.html").read_text(encoding="utf-8")
    for marker in [
        "源目录",                    # 源目录（左侧源卡）
        "策略附页",                  # 策略附页（滞后分级页，升级入口挂载处）
        "水位卡片",                  # 水位卡片
        "升级审计",                  # 审计列表（升级专册页）
        "/api/arrivals/'+arrival.id+'/upgrade",  # 真实升级写路径，不是纯徽章
        "doUpgrade",
        "#{{a.id}}",                # 到达详情展示到达主键
        "升级偏了",                  # 驳回留痕文案在审计页可见
    ]:
        assert marker in html, f"前端缺少同态视图标记: {marker}"
    # 升级入口挂在滞后分级页内，不存在独立“升级台”导航
    assert "升级台" not in html and "nav=['到达登记','滞后分级','补齐恢复','升级审计']" in html
