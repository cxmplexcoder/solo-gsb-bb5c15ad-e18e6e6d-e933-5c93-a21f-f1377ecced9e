"""乐观并发版本：持久化 version、expected_versions 覆盖/校验、预演预测版本。"""
import sqlite3

from helpers import container, sample, volume_of

from app import db
from app.config import settings


def _batch(client, transfers, expected_versions=None):
    payload = {"transfers": transfers}
    if expected_versions is not None:
        payload["expected_versions"] = expected_versions
    return client.post("/transfers/batch", json=payload)


def _preview(client, transfers, expected_versions=None):
    payload = {"transfers": transfers}
    if expected_versions is not None:
        payload["expected_versions"] = expected_versions
    return client.post("/transfers/preview", json=payload)


def _version_of(client, cid):
    return client.get(f"/containers/{cid}").json()["version"]


def _versions_of(client, cids):
    return {cid: _version_of(client, cid) for cid in cids}


# --------------------------------------------------------------------------- #
# version 的持久化与返回
# --------------------------------------------------------------------------- #
def test_version_starts_at_zero_and_is_returned_by_get(client):
    sample(client, "S1", "10")
    container(client, "V1")
    assert _version_of(client, "S1") == 0
    assert _version_of(client, "V1") == 0


def test_batch_increments_each_touched_container_exactly_once(client):
    sample(client, "S1", "10")
    container(client, "V1")
    container(client, "V2")

    # V1 与 S1 在同批中各出现两次，版本也只 +1
    r = _batch(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "4"},
            {"source": "V1", "destination": "V2", "volume": "1"},
            {"source": "S1", "destination": "V2", "volume": "2"},
        ],
    )
    assert r.status_code == 200, r.text
    assert r.json()["versions"] == {"S1": 1, "V1": 1, "V2": 1}
    # 响应中的版本与 GET 一致
    assert _versions_of(client, ("S1", "V1", "V2")) == {"S1": 1, "V1": 1, "V2": 1}

    # 再提交一批只触碰 S1 -> V1：两者 +1，V2 不变
    r = _batch(client, [{"source": "S1", "destination": "V1", "volume": "1"}])
    assert r.status_code == 200
    assert r.json()["versions"] == {"S1": 2, "V1": 2}
    assert _versions_of(client, ("S1", "V1", "V2")) == {"S1": 2, "V1": 2, "V2": 1}


def test_batch_without_expected_versions_keeps_compatible_behavior(client):
    sample(client, "S1", "10")
    container(client, "V1")
    r = _batch(client, [{"source": "S1", "destination": "V1", "volume": "3"}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == 1
    assert body["message"] == "已依次提交 1 条转移"
    assert body["versions"] == {"S1": 1, "V1": 1}
    assert volume_of(client, "V1") == "3.000000000"


# --------------------------------------------------------------------------- #
# expected_versions：覆盖校验（422）
# --------------------------------------------------------------------------- #
def test_expected_versions_must_cover_exactly_all_sources_and_destinations(client):
    sample(client, "S1", "10")
    container(client, "V1")
    container(client, "V2")
    transfers = [
        {"source": "S1", "destination": "V1", "volume": "1"},
        {"source": "S1", "destination": "V2", "volume": "1"},
    ]

    # 缺少 V2
    r = _batch(client, transfers, {"S1": 0, "V1": 0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_expected_versions"
    assert "V2" in r.json()["error"]["message"]

    # 多余 V3
    r = _batch(client, transfers, {"S1": 0, "V1": 0, "V2": 0, "V3": 0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_expected_versions"
    assert "V3" in r.json()["error"]["message"]

    # 空对象也算提供，必然覆盖不全
    r = _batch(client, transfers, {})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_expected_versions"

    # 覆盖错误不落库：体积与版本都保持原样
    assert volume_of(client, "S1") == "10.000000000"
    assert _versions_of(client, ("S1", "V1", "V2")) == {"S1": 0, "V1": 0, "V2": 0}


def test_expected_versions_success_then_must_track_increment(client):
    sample(client, "S1", "10")
    container(client, "V1")
    transfers = [{"source": "S1", "destination": "V1", "volume": "3"}]

    r = _batch(client, transfers, {"S1": 0, "V1": 0})
    assert r.status_code == 200, r.text
    assert r.json()["versions"] == {"S1": 1, "V1": 1}

    # 版本已推进，沿用旧期望版本不再成功
    r = _batch(client, transfers, {"S1": 0, "V1": 0})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_container"

    # 用最新版本则成功
    r = _batch(client, transfers, _versions_of(client, ("S1", "V1")))
    assert r.status_code == 200, r.text
    assert r.json()["versions"] == {"S1": 2, "V1": 2}
    assert volume_of(client, "S1") == "4.000000000"


# --------------------------------------------------------------------------- #
# expected_versions：版本校验（409）与整批原子性
# --------------------------------------------------------------------------- #
def test_stale_container_conflict_rolls_back_whole_batch(client):
    sample(client, "S1", "10")
    sample(client, "S2", "10")
    container(client, "V1")
    container(client, "V2")

    # 第二条转移的目标 V2 期望版本错误：整批拒绝，第一条也不落库
    r = _batch(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "5"},
            {"source": "S2", "destination": "V2", "volume": "5"},
        ],
        {"S1": 0, "S2": 0, "V1": 0, "V2": 7},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "stale_container"
    assert "V2" in body["error"]["message"]

    assert volume_of(client, "S1") == "10.000000000"
    assert volume_of(client, "S2") == "10.000000000"
    assert volume_of(client, "V1") == "0"
    assert volume_of(client, "V2") == "0"
    # 版本也未递增
    assert _versions_of(client, ("S1", "S2", "V1", "V2")) == {
        "S1": 0,
        "S2": 0,
        "V1": 0,
        "V2": 0,
    }


def test_stale_after_concurrent_committed_batch(client):
    """读到的版本被另一批提交推进后，携带旧期望版本的批次被拒绝。"""
    sample(client, "S1", "10")
    container(client, "V1")
    transfers = [{"source": "S1", "destination": "V1", "volume": "3"}]

    expected = _versions_of(client, ("S1", "V1"))  # 双方读到 0
    r = _batch(client, transfers)  # 另一客户端先提交（无版本保护）
    assert r.status_code == 200

    r = _batch(client, transfers, expected)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_container"
    assert volume_of(client, "S1") == "7.000000000"
    assert _versions_of(client, ("S1", "V1")) == {"S1": 1, "V1": 1}


def test_expected_versions_with_unknown_container_still_404(client):
    sample(client, "S1", "10")
    r = _batch(
        client,
        [{"source": "S1", "destination": "GHOST", "volume": "1"}],
        {"S1": 0, "GHOST": 0},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_container"


# --------------------------------------------------------------------------- #
# preview：复用覆盖与版本校验，不递增、不写库，返回预测提交后的版本
# --------------------------------------------------------------------------- #
def test_preview_reuses_coverage_and_version_validation(client):
    sample(client, "S1", "10")
    container(client, "V1")
    transfers = [{"source": "S1", "destination": "V1", "volume": "3"}]

    # 覆盖错误与真实提交一致
    r = _preview(client, transfers, {"S1": 0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_expected_versions"

    # 版本不符与真实提交一致
    r = _preview(client, transfers, {"S1": 0, "V1": 5})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_container"

    # 校验失败同样不落库
    assert _versions_of(client, ("S1", "V1")) == {"S1": 0, "V1": 0}
    assert volume_of(client, "S1") == "10.000000000"


def test_preview_returns_predicted_post_commit_versions_without_writing(client):
    sample(client, "S1", "10")
    container(client, "V1")
    container(client, "V2")
    transfers = [
        {"source": "S1", "destination": "V1", "volume": "4"},
        {"source": "V1", "destination": "V2", "volume": "1"},
    ]

    r = _preview(client, transfers, {"S1": 0, "V1": 0, "V2": 0})
    assert r.status_code == 200, r.text
    predicted = {c["container_id"]: c["version"] for c in r.json()["containers"]}
    # 预测提交后的版本：当前 0 + 1（V1 出现两次也只 +1）
    assert predicted == {"S1": 1, "V1": 1, "V2": 1}

    # 预演不递增、不写库
    assert _versions_of(client, ("S1", "V1", "V2")) == {"S1": 0, "V1": 0, "V2": 0}
    assert volume_of(client, "V1") == "0"

    # 重复预演结果一致（幂等）
    assert _preview(client, transfers, {"S1": 0, "V1": 0, "V2": 0}).json() == r.json()


def test_preview_predicted_versions_match_subsequent_commit(client):
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    container(client, "V1")
    transfers = [
        {"source": "S1", "destination": "V1", "volume": "40"},
        {"source": "S2", "destination": "V1", "volume": "60"},
    ]
    expected = _versions_of(client, ("S1", "S2", "V1"))

    preview = _preview(client, transfers, expected).json()
    r = _batch(client, transfers, expected)
    assert r.status_code == 200, r.text

    # 预演的预测版本 == 真实提交后的版本（GET 与批量响应一致）
    for c in preview["containers"]:
        assert c["version"] == _version_of(client, c["container_id"])
    assert r.json()["versions"] == _versions_of(client, ("S1", "S2", "V1"))


def test_preview_without_expected_versions_also_predicts_versions(client):
    sample(client, "S1", "10")
    container(client, "V1")
    r = _preview(client, [{"source": "S1", "destination": "V1", "volume": "3"}])
    assert r.status_code == 200
    predicted = {c["container_id"]: c["version"] for c in r.json()["containers"]}
    assert predicted == {"S1": 1, "V1": 1}
    assert _versions_of(client, ("S1", "V1")) == {"S1": 0, "V1": 0}


# --------------------------------------------------------------------------- #
# 数据库初始化兼容已有数据
# --------------------------------------------------------------------------- #
def test_init_db_migrates_existing_database(tmp_path, monkeypatch):
    """老库（无 version 列）初始化后补列，已有行版本为 0，且幂等。"""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE containers (id TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE components ("
        "container_id TEXT NOT NULL, source_id TEXT NOT NULL, amount TEXT NOT NULL, "
        "PRIMARY KEY (container_id, source_id))"
    )
    conn.execute("INSERT INTO containers (id) VALUES ('S1')")
    conn.execute("INSERT INTO components VALUES ('S1', 'S1', '10')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "database_path", str(db_path))
    db.init_db()
    db.init_db()  # 幂等：重复初始化不报错

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT version FROM containers WHERE id = 'S1'"
        ).fetchone()
        assert row["version"] == 0
        # 迁移后的库可正常写入新版本
        conn.execute("UPDATE containers SET version = 1 WHERE id = 'S1'")
        row = conn.execute(
            "SELECT version FROM containers WHERE id = 'S1'"
        ).fetchone()
        assert row["version"] == 1
    finally:
        conn.close()
