# -*- coding: utf-8 -*-
"""HTTP 冒烟测试（固化版，每批改动后必跑）。

用法：先启动服务器（uvicorn app.main:app --port 8100），然后：
    python scripts/smoke.py [端口，默认 8100]

覆盖：核心 GET 端点 / 记忆 CRUD + 404 分支 / 学习模式 409 分支 /
章节 CRUD + 文档同步（临时项目，跑完自动清理）。不触发真实 LLM 调用。
"""
import json
import pathlib
import shutil
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:" + (sys.argv[1] if len(sys.argv) > 1 else "8100")

FAILED = []


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, (json.loads(raw) if raw else {})


def ok(name, cond, extra=""):
    if cond:
        print("  ✓ " + name)
    else:
        FAILED.append(name)
        print("  ✗ " + name + " " + str(extra)[:120])


def expect_http_error(method, path, code, name, body=None):
    try:
        call(method, path, body)
        ok(name, False, "未按预期报错")
    except urllib.error.HTTPError as e:
        ok(name, e.code == code, f"got {e.code}")


def main():
    print("冒烟:", BASE)

    print("== 核心端点 ==")
    for p in ("/api/health", "/api/config", "/api/settings", "/api/conversations",
              "/api/memory", "/api/metrics/summary", "/api/learning/projects", "/api/skills/market"):
        try:
            st, _ = call("GET", p)
            ok(p, st == 200)
        except Exception as e:  # noqa: BLE001
            ok(p, False, e)

    print("== 记忆 CRUD ==")
    _, r = call("POST", "/api/memory", {"layer": "longterm", "content": "SMOKE-ITEM", "importance": 2})
    mid = r["item"]["id"]
    ok("POST /api/memory", bool(mid))
    _, r = call("PUT", "/api/memory/" + mid, {"content": "SMOKE-EDITED", "importance": 3})
    ok("PUT 编辑", r["item"]["content"] == "SMOKE-EDITED")
    expect_http_error("PUT", "/api/memory/nonexist", 404, "编辑不存在 404", {"content": "x"})
    expect_http_error("PUT", "/api/memory/" + mid, 400, "importance 非整数 400", {"importance": "abc"})
    call("DELETE", "/api/memory/" + mid)
    ok("DELETE 记忆", True)

    print("== 学习模式 ==")
    from app import db, learning_skill as ls
    conv = db.create_conversation("SMOKE-CONV")
    cid = conv["id"]
    _, r = call("POST", "/api/learning/mode", {"conversation_id": cid})
    ok("进入引导", r.get("status") == "ask")
    expect_http_error("POST", "/api/learning/mode", 409, "非确认阶段 edit-plan 409",
                      {"conversation_id": cid, "action": "edit-plan", "field": "style", "value": "x"})
    db.delete_conversation(cid)

    print("== 章节 CRUD + 文档同步 ==")
    conv2 = db.create_conversation("SMOKE-CONV2")
    proj = db.create_learning_project("SMOKE-PROJ", "零基础", "入门", "精炼（结构清晰）", conv2["id"])
    pid = proj["id"]
    for i, t in enumerate(["甲", "乙", "丙"], 1):
        db.add_learning_chapter(pid, t, "目标", i)
    try:
        _, detail = call("GET", "/api/learning/projects/" + pid)
        c1 = detail["chapters"][0]
        _, r = call("PUT", "/api/learning/chapters/" + c1["id"], {"title": "甲改"})
        ok("PUT 章节改标题", r["status"] == "ok")
        _, r = call("POST", "/api/learning/projects/" + pid + "/chapters", {"title": "中插", "order_no": 2})
        ok("POST 插入章节", r["chapters"][1]["title"] == "中插")
        _, r = call("POST", "/api/learning/chapters/" + c1["id"] + "/move", {"direction": "down"})
        ok("POST 章节下移", r["chapters"][0]["title"] == "中插")
        _, detail = call("GET", "/api/learning/projects/" + pid)
        _, r = call("DELETE", "/api/learning/chapters/" + detail["chapters"][0]["id"])
        ok("DELETE 章节", [c["title"] for c in r["chapters"]] == ["甲改", "乙", "丙"])
        pdir = pathlib.Path("notes") / pid
        outline = (pdir / "大纲.md").read_text(encoding="utf-8")
        ok("大纲同步", "甲改" in outline and "中插" not in outline)
        expect_http_error("POST", "/api/learning/projects/nonexist/organize", 404, "organize 404")
        expect_http_error("POST", "/api/learning/projects/" + pid + "/organize/rollback", 400, "rollback 无历史 400")
    finally:
        db.delete_learning_project(pid)
        shutil.rmtree(pathlib.Path("notes") / pid, ignore_errors=True)
        db.delete_conversation(conv2["id"])

    print("== 人格画像 ==")
    from app import db as _db
    expect_http_error("GET", "/api/persona/nonexist", 404, "画像不存在会话 404")
    _pc = _db.create_conversation("SMOKE-PERSONA")
    _, r = call("GET", "/api/persona/" + _pc["id"])
    ok("画像默认关闭", r.get("enabled") is False)
    _, r = call("POST", "/api/persona/" + _pc["id"] + "/toggle", {"enabled": True})
    ok("画像开启", r.get("enabled") is True and r.get("turns") == 0)
    _, r = call("POST", "/api/persona/" + _pc["id"] + "/toggle", {"enabled": False})
    ok("画像关闭即删", r.get("enabled") is False and not r.get("snapshots"))
    _db.delete_conversation(_pc["id"])

    if FAILED:
        print("\n冒烟 FAILED：" + "、".join(FAILED))
        sys.exit(1)
    print("\n冒烟 PASS ✅（测试数据已清理）")


if __name__ == "__main__":
    main()
