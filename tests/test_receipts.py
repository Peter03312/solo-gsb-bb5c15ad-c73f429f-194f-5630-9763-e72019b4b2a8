"""实际投递回执（POST /deliveries/{id}/receipts、GET /deliveries/{id}/receipts）：

成功判定持久化后，按投递编号 + 终点邮箱 + 正整数尝试序号回传实际投递结果
（暂时失败 / 永久失败 / 已送达），并查询各终点尝试记录与汇总状态。
未知编号、原判定失败、非所属终点一律拒绝且不产生记录；同序号同结果重试原样返回，
同序号不同结果冲突；新序号必须大于已记录序号；终点已送达 / 永久失败后不再接收新尝试。
汇总：全部终点已送达 → delivered，任一终点永久失败 → failed，其余（含无回执）→ pending。
回执不影响既有判定查询、同编号重放内容与灰度熔断统计。
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

# 必须在导入 app 之前设置，config 在导入时读取
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    conn.executescript(
        "DELETE FROM draft_rules;"
        "DELETE FROM delivery_policy;"
        "DELETE FROM delivery_receipts;"
        "DELETE FROM delivery_decisions;"
        "DELETE FROM circuit_breaker_settings;"
        "DELETE FROM circuit_breaks;"
        "DELETE FROM version_rules;"
        "DELETE FROM versions;"
        "DELETE FROM sqlite_sequence WHERE name = 'versions';"
        "DELETE FROM draft_revisions;"
        "INSERT INTO draft_revisions (revision, rules, rule_count, created_at)"
        " VALUES (0, '[]', 0, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));"
        "UPDATE draft_meta SET revision = 0 WHERE id = 1;"
    )
    conn.close()
    with TestClient(app) as c:
        yield c


RULES = [
    {
        "match_type": "exact",
        "match_key": "multi@corp.example.com",
        "action": "route",
        "targets": [
            {"kind": "mailbox", "value": "alice@mail.example.com"},
            {"kind": "mailbox", "value": "bob@mail.example.com"},
        ],
    },
    {
        "match_type": "exact",
        "match_key": "blocked@corp.example.com",
        "action": "reject",
        "targets": [],
    },
]


def setup_decision(client, delivery_id="msg-1", address="multi@corp.example.com"):
    """发布单版本、配置 100% 候选策略并提交一笔判定，返回判定响应。"""
    assert client.put("/draft", json={"revision": 0, "rules": RULES}).status_code == 200
    assert client.post("/publish", json={"revision": 1}).status_code == 200
    resp = client.put("/policy", json={
        "revision": 0, "baseline_version": 1, "candidate_version": 1, "candidate_percent": 100,
    })
    assert resp.status_code == 200
    return client.post("/deliveries", json={"delivery_id": delivery_id, "address": address})


def report(client, delivery_id, mailbox, attempt, result):
    return client.post(f"/deliveries/{delivery_id}/receipts", json={
        "mailbox": mailbox, "attempt": attempt, "result": result,
    })


def receipts(client, delivery_id):
    return client.get(f"/deliveries/{delivery_id}/receipts")


# ---------- 回传：首次记录与幂等重试 ----------

def test_report_receipt_and_query_summary(client):
    assert setup_decision(client).status_code == 200
    resp = report(client, "msg-1", " Alice@Mail.Example.COM ", 1, "temporary_failure")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "delivery_id": "msg-1",
        "mailbox": "alice@mail.example.com",  # 规范化
        "attempt": 1,
        "result": "temporary_failure",
        "created_at": body["created_at"],
    }
    assert body["created_at"]

    # 暂时失败后可继续新尝试；同终点序号严格递增
    assert report(client, "msg-1", "alice@mail.example.com", 2, "delivered").status_code == 200
    assert report(client, "msg-1", "bob@mail.example.com", 1, "delivered").status_code == 200

    view = receipts(client, "msg-1")
    assert view.status_code == 200
    doc = view.json()
    assert doc["delivery_id"] == "msg-1"
    assert doc["summary"] == "delivered"  # 全部终点已送达
    endpoints = {e["mailbox"]: e for e in doc["endpoints"]}
    assert set(endpoints) == {"alice@mail.example.com", "bob@mail.example.com"}
    alice = endpoints["alice@mail.example.com"]
    assert alice["status"] == "delivered"
    assert [(a["attempt"], a["result"]) for a in alice["attempts"]] == [
        (1, "temporary_failure"), (2, "delivered"),
    ]
    assert all(a["created_at"] for a in alice["attempts"])
    assert endpoints["bob@mail.example.com"]["status"] == "delivered"


def test_replay_same_attempt_same_result_returns_stored_record(client):
    assert setup_decision(client).status_code == 200
    first = report(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure")
    assert first.status_code == 200
    replay = report(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure")
    assert replay.status_code == 200
    assert replay.json() == first.json()  # 原样返回（含原 created_at）
    # 重试不产生新记录
    view = receipts(client, "msg-1").json()
    assert len(view["endpoints"][0]["attempts"]) == 1


def test_no_receipts_summary_is_pending(client):
    assert setup_decision(client).status_code == 200
    view = receipts(client, "msg-1")
    assert view.status_code == 200
    doc = view.json()
    assert doc["summary"] == "pending"  # 无回执也为待完成
    assert all(e["status"] == "pending" and e["attempts"] == [] for e in doc["endpoints"])


def test_permanent_failure_makes_summary_failed(client):
    assert setup_decision(client).status_code == 200
    assert report(client, "msg-1", "alice@mail.example.com", 1, "delivered").status_code == 200
    assert report(client, "msg-1", "bob@mail.example.com", 1, "temporary_failure").status_code == 200
    assert receipts(client, "msg-1").json()["summary"] == "pending"  # 一送达一暂败：待完成
    assert report(client, "msg-1", "bob@mail.example.com", 2, "permanent_failure").status_code == 200
    doc = receipts(client, "msg-1").json()
    assert doc["summary"] == "failed"  # 任一终点永久失败 → 失败
    endpoints = {e["mailbox"]: e for e in doc["endpoints"]}
    assert endpoints["bob@mail.example.com"]["status"] == "permanent_failure"
    assert endpoints["alice@mail.example.com"]["status"] == "delivered"


# ---------- 拒绝：未知编号 / 原判定失败 / 非所属终点，且不产生记录 ----------

def test_unknown_delivery_id_rejected(client):
    resp = report(client, "nope", "alice@mail.example.com", 1, "delivered")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "delivery_not_found"
    assert receipts(client, "nope").status_code == 404


def test_failed_decision_rejects_receipts(client):
    resp = setup_decision(client, delivery_id="msg-f", address="blocked@corp.example.com")
    assert resp.status_code == 422  # 原判定失败（拒收）但已持久化
    resp = report(client, "msg-f", "alice@mail.example.com", 1, "delivered")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "decision_failed"
    assert receipts(client, "msg-f").status_code == 409  # 查询同样拒绝
    assert client.get("/deliveries/msg-f").json()["status"] == "failed"  # 判定本身不受影响


def test_endpoint_not_in_decision_rejected(client):
    assert setup_decision(client).status_code == 200
    resp = report(client, "msg-1", "carol@mail.example.com", 1, "delivered")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "endpoint_not_in_decision"
    assert detail["endpoints"] == ["alice@mail.example.com", "bob@mail.example.com"]
    # 不产生记录
    assert receipts(client, "msg-1").json()["summary"] == "pending"
    assert all(e["attempts"] == [] for e in receipts(client, "msg-1").json()["endpoints"])


def test_invalid_mailbox_and_bad_params_rejected(client):
    assert setup_decision(client).status_code == 200
    resp = report(client, "msg-1", "not-an-address", 1, "delivered")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_address"
    # 非正整数序号 / 非法结果 / 缺字段：请求体层 422
    assert report(client, "msg-1", "alice@mail.example.com", 0, "delivered").status_code == 422
    assert report(client, "msg-1", "alice@mail.example.com", -1, "delivered").status_code == 422
    assert report(client, "msg-1", "alice@mail.example.com", 1, "bounced").status_code == 422
    assert client.post("/deliveries/msg-1/receipts", json={"mailbox": "alice@mail.example.com"}).status_code == 422
    # 均未产生记录
    assert all(e["attempts"] == [] for e in receipts(client, "msg-1").json()["endpoints"])


# ---------- 序号约束与终态 ----------

def test_same_attempt_different_result_conflict(client):
    assert setup_decision(client).status_code == 200
    assert report(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure").status_code == 200
    resp = report(client, "msg-1", "alice@mail.example.com", 1, "delivered")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "receipt_conflict"
    assert detail["stored_result"] == "temporary_failure"
    # 冲突不改写已存记录
    attempts = receipts(client, "msg-1").json()["endpoints"][0]["attempts"]
    assert [(a["attempt"], a["result"]) for a in attempts] == [(1, "temporary_failure")]


def test_new_attempt_must_exceed_recorded(client):
    assert setup_decision(client).status_code == 200
    assert report(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure").status_code == 200
    assert report(client, "msg-1", "alice@mail.example.com", 3, "temporary_failure").status_code == 200  # 允许跳号
    resp = report(client, "msg-1", "alice@mail.example.com", 2, "delivered")  # 未大于已记录序号
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "attempt_not_increasing"
    assert detail["max_attempt"] == 3
    # 不产生记录；更大序号仍可接收
    assert report(client, "msg-1", "alice@mail.example.com", 4, "delivered").status_code == 200
    attempts = receipts(client, "msg-1").json()["endpoints"][0]["attempts"]
    assert [a["attempt"] for a in attempts] == [1, 3, 4]


def test_no_new_attempt_after_delivered(client):
    assert setup_decision(client).status_code == 200
    assert report(client, "msg-1", "alice@mail.example.com", 1, "delivered").status_code == 200
    resp = report(client, "msg-1", "alice@mail.example.com", 2, "temporary_failure")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "endpoint_finalized"
    assert detail["final_result"] == "delivered"
    # 已送达不影响同序号重试原样返回
    replay = report(client, "msg-1", "alice@mail.example.com", 1, "delivered")
    assert replay.status_code == 200
    assert replay.json()["result"] == "delivered"


def test_no_new_attempt_after_permanent_failure(client):
    assert setup_decision(client).status_code == 200
    assert report(client, "msg-1", "alice@mail.example.com", 1, "temporary_failure").status_code == 200
    assert report(client, "msg-1", "alice@mail.example.com", 2, "permanent_failure").status_code == 200
    resp = report(client, "msg-1", "alice@mail.example.com", 3, "delivered")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "endpoint_finalized"
    assert resp.json()["detail"]["final_result"] == "permanent_failure"
    # 另一终点不受影响
    assert report(client, "msg-1", "bob@mail.example.com", 1, "delivered").status_code == 200


# ---------- 并发：序号与汇总保持一致 ----------

def test_concurrent_same_receipt_single_record(client):
    assert setup_decision(client).status_code == 200
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(
            lambda _: report(client, "msg-1", "alice@mail.example.com", 1, "delivered"), range(16)
        ))
    assert all(r.status_code == 200 for r in responses)
    assert {r.text for r in responses} == {responses[0].text}  # 完全相同的已存记录
    assert len(receipts(client, "msg-1").json()["endpoints"][0]["attempts"]) == 1


def test_concurrent_conflicting_results_exactly_one_wins(client):
    assert setup_decision(client).status_code == 200
    results = ["delivered", "permanent_failure", "temporary_failure"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda r: report(client, "msg-1", "alice@mail.example.com", 1, r), results))
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409, 409]  # 先到者占号，其余冲突
    attempts = receipts(client, "msg-1").json()["endpoints"][0]["attempts"]
    assert len(attempts) == 1
    winner = next(r for r in responses if r.status_code == 200)
    assert attempts[0]["result"] == winner.json()["result"]


def test_concurrent_increasing_attempts_stay_consistent(client):
    assert setup_decision(client).status_code == 200
    # 并发提交不同序号（暂败，避免终态）：事务串行化后，只有提交时大于当前
    # 最大序号的请求被接收，后到的小序号被明确拒绝；记录始终严格递增、汇总一致
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(
            lambda n: report(client, "msg-1", "alice@mail.example.com", n, "temporary_failure"),
            range(1, 9),
        ))
    accepted = sorted(r.json()["attempt"] for r in responses if r.status_code == 200)
    rejected = [r for r in responses if r.status_code != 200]
    assert accepted  # 至少先到者被接收
    assert all(r.status_code == 409 and r.json()["detail"]["error"] == "attempt_not_increasing"
               for r in rejected)
    attempts = receipts(client, "msg-1").json()["endpoints"][0]["attempts"]
    assert [a["attempt"] for a in attempts] == accepted  # 记录与接收集完全一致、严格递增
    assert accepted[-1] == 8  # 全局最大序号必然被接收
    assert receipts(client, "msg-1").json()["summary"] == "pending"


# ---------- 回执不影响既有判定查询、同编号重放与灰度熔断统计 ----------

def test_receipts_do_not_affect_decision_query_and_replay(client):
    first = setup_decision(client)
    assert first.status_code == 200
    stored_before = client.get("/deliveries/msg-1").json()
    assert report(client, "msg-1", "alice@mail.example.com", 1, "delivered").status_code == 200
    assert report(client, "msg-1", "bob@mail.example.com", 1, "permanent_failure").status_code == 200
    # 判定查询内容不变
    assert client.get("/deliveries/msg-1").json() == stored_before
    # 同编号重放内容不变
    replay = client.post("/deliveries", json={"delivery_id": "msg-1", "address": "multi@corp.example.com"})
    assert replay.status_code == 200
    assert replay.json() == first.json()


def test_receipts_do_not_affect_circuit_breaker_stats(client):
    assert setup_decision(client).status_code == 200  # 候选侧成功判定（policy_revision 1）
    resp = client.put("/policy/circuit-breaker", json={
        "revision": 1, "failure_threshold_percent": 1, "min_candidate_samples": 1, "window_minutes": 5,
    })
    assert resp.status_code == 200
    # 回传永久失败回执：不构成新判定，不进入熔断统计
    assert report(client, "msg-1", "alice@mail.example.com", 1, "permanent_failure").status_code == 200
    assert report(client, "msg-1", "bob@mail.example.com", 1, "permanent_failure").status_code == 200
    breaker = client.get("/policy/circuit-breaker").json()
    assert breaker["breaks"] == []  # 未触发熔断
    assert client.get("/policy").json()["candidate_percent"] == 100  # 策略未被回执改变
    # 报表也不受回执影响：仍只有 1 笔 ok 判定
    report_view = client.get(
        "/deliveries/report",
        params={"start": "2020-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z"},
    ).json()
    assert report_view["total"] == 1
    assert report_view["groups"] == [
        {"version": 1, "selection": "candidate", "outcome": "ok", "count": 1}
    ]
