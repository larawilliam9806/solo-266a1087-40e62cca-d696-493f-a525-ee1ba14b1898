"""关键流程自测（不依赖 pytest，直接 python -I tests/selftest.py 运行）。

覆盖：
1. CSV 校验：中文表头、重复 (样本,项目)、负数/非数字/空值/缺列/缺批次号
2. 端到端：上传 → 预览(增/改/删/不变分类) → 发布 → 登记版本/行数 → 下游文件哈希一致
3. 并发：两个页面从同一版本发起，仅一个发布成功，另一个 409 须重新预览
4. 崩溃恢复：文件缺失、文件被改坏、遗留临时文件、游离文件、改名后未提交
5. 撤回语义：整批替换后旧行消失；会话取消/重复发布防护
6. 串号核查：配对算法、缺项、多最优、唯一最优更名、确认前版本被替换则失效
"""
import hashlib
import io
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wq_publish.app import create_app
from wq_publish.storage import LIVE_FILENAME
from wq_publish.swapcheck import (
    apply_mapping,
    common_analytes,
    optimal_assignments,
    possible_targets,
)
from wq_publish.validation import render_csv, validate_csv

PASS, FAIL = 0, 0
EXAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "examples")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def read_example(fname):
    with open(os.path.join(EXAMPLE_DIR, fname), "rb") as fh:
        return fh.read()


def upload(client, fname, batch_no="B1"):
    return client.post(
        "/preview",
        data={"batch_no": batch_no,
              "csv_file": (io.BytesIO(read_example(fname)), fname)},
        content_type="multipart/form-data",
    )


# ---------------- 1. 校验单元测试 ----------------
print("== 1. CSV 校验 ==")
rows, errs = validate_csv(read_example("batch_v1.csv"), "B1")
check("合法 CSV 通过并解析出 5 行", errs == [] and len(rows) == 5, str(errs))

rows, errs = validate_csv(read_example("batch_invalid.csv"), "B1")
check("重复(样本,项目)被拒", any("重复" in e for e in errs), str(errs))
check("负浓度被拒", any("负数" in e for e in errs), str(errs))
check("空浓度被拒", any("浓度为空" in e for e in errs), str(errs))
check("空项目被拒", any("检测项目为空" in e for e in errs), str(errs))

bad = "样本号,浓度\nS1,1.0\n".encode()
rows, errs = validate_csv(bad, "B1")
check("缺少检测项目列被拒", rows is None and any("表头" in e for e in errs), str(errs))

rows, errs = validate_csv(read_example("batch_v1.csv"), " ")
check("空批次号被拒", any("批次号" in e for e in errs), str(errs))

for text in ["S1,氨氮,nan", "S1,氨氮,inf", "S1,氨氮,abc"]:
    raw = f"样本号,检测项目,浓度\n{text}\n".encode()
    rows, errs = validate_csv(raw, "B1")
    check(f"非法浓度 {text!r} 被拒", rows is None and len(errs) > 0, str(errs))

raw_zero = "样本号,检测项目,浓度\nS1,氨氮,0\n".encode()
rows, errs = validate_csv(raw_zero, "B1")
check("零浓度（非负边界）通过", errs == [] and rows[0]["concentration"] == 0.0, str(errs))

# ---------------- 2-5. 端到端（隔离临时目录） ----------------
tmp = tempfile.mkdtemp(prefix="wq-selftest-")
data_dir = os.path.join(tmp, "data")
down_dir = os.path.join(tmp, "downstream")
app = create_app(data_dir=data_dir, downstream_dir=down_dir)
client = app.test_client()

print("== 2. 首次发布 v1 ==")
resp = upload(client, "batch_v1.csv", "BATCH-001")
check("预览页 200 且显示增删改", resp.status_code == 200
      and "新增".encode() in resp.data and "撤回".encode() in resp.data)
m = re.search(rb'/publish/([0-9a-f]+)', resp.data)
check("预览页含发布令牌", m is not None)
token1 = m.group(1).decode()

resp = client.post(f"/publish/{token1}")
check("确认发布后 302 回首页", resp.status_code == 302)
reg = app.storage.current_registry()
check("登记为 v1、5 行", reg["current_version"] == 1 and reg["row_count"] == 5,
      f"got {dict(reg) if reg else None}")
live = os.path.join(down_dir, LIVE_FILENAME)
check("下游 CSV 已生成", os.path.exists(live))
file_hash = hashlib.sha256(open(live, "rb").read()).hexdigest()
check("文件哈希与登记一致", file_hash == reg["content_hash"])
check("下游目录无临时文件残留",
      not [n for n in os.listdir(down_dir) if n.startswith(".tmp-")])
resp = client.get("/published.csv")
check("已发布 CSV 可下载", resp.status_code == 200 and b"S001" in resp.data)
resp = client.post(f"/publish/{token1}")
check("同一会话不可重复发布（返回 409 过期页）", resp.status_code == 409
      and app.storage.get_session(token1)["status"] == "published")

print("== 3. 修订预览的增/改/删/不变分类 ==")
resp = upload(client, "batch_v2_revision.csv", "BATCH-002")
body = resp.data.decode()
# v2 相对 v1：S004氨氮新增；S001氨氮、S002COD 修改；S002氨氮 撤回；S001总磷/S003总磷 不变
check("新增 1 行", "+1" in body and "S004" in body)
check("修改 2 行", "~2" in body and "1.25" in body and "35.0" in body)
check("撤回 1 行（S002 氨氮）", "−1" in body and "S002" in body)
check("不变 2 行", "不变项 2" in body)
token2 = re.search(rb'/publish/([0-9a-f]+)', resp.data).group(1).decode()
client.post(f"/publish/{token2}")
reg = app.storage.current_registry()
check("发布后登记为 v2、5 行", reg["current_version"] == 2 and reg["row_count"] == 5)
current = app.storage.current_rows()
check("撤回生效：S002 氨氮已不存在",
      not any(r["sample_no"] == "S002" and r["analyte"] == "氨氮" for r in current))
check("修改生效：S001 氨氮=1.3",
      any(r["sample_no"] == "S001" and r["analyte"] == "氨氮"
          and r["concentration"] == 1.3 for r in current))
file_hash = hashlib.sha256(open(live, "rb").read()).hexdigest()
check("v2 文件哈希与登记一致", file_hash == reg["content_hash"])

print("== 4. 同版本并发修订：只允许一个成功 ==")
r_a = upload(client, "batch_v1.csv", "CONCUR-A")
r_b = upload(client, "batch_v2_revision.csv", "CONCUR-B")
tok_a = re.search(rb'/publish/([0-9a-f]+)', r_a.data).group(1).decode()
tok_b = re.search(rb'/publish/([0-9a-f]+)', r_b.data).group(1).decode()
ok = client.post(f"/publish/{tok_a}")
check("A 发布成功 → v3", ok.status_code == 302
      and app.storage.current_registry()["current_version"] == 3)
conflict = client.post(f"/publish/{tok_b}")
check("B 发布被拒并返回 409 过期预览页", conflict.status_code == 409
      and "该预览已过期".encode() in conflict.data)
sess_b = app.storage.get_session(tok_b)
check("B 会话被标记 obsolete", sess_b["status"] == "obsolete")
reg_before = app.storage.current_registry()["current_version"]
retry = upload(client, "batch_v2_revision.csv", "CONCUR-B-RETRY")
tok_b2 = re.search(rb'/publish/([0-9a-f]+)', retry.data).group(1).decode()
client.post(f"/publish/{tok_b2}")
check("B 重新预览后可发布 → v4",
      app.storage.current_registry()["current_version"] == reg_before + 1)
check("A 的发布未被覆盖：v4 文件行数=5",
      app.storage.current_registry()["row_count"] == 5)

print("== 5. 崩溃后重启自动恢复 ==")
# 5a 下游文件被删除
os.unlink(live)
app2 = create_app(data_dir=data_dir, downstream_dir=down_dir)
reg = app2.storage.current_registry()
check("5a 文件缺失后重建且哈希与登记一致",
      os.path.exists(live)
      and hashlib.sha256(open(live, "rb").read()).hexdigest() == reg["content_hash"])

# 5b 文件被外部改坏
with open(live, "ab") as fh:
    fh.write(b"CORRUPTED\n")
app3 = create_app(data_dir=data_dir, downstream_dir=down_dir)
reg = app3.storage.current_registry()
check("5b 损坏文件被按登记重写",
      hashlib.sha256(open(live, "rb").read()).hexdigest() == reg["content_hash"])

# 5c 遗留半写临时文件
tmp_orphan = os.path.join(down_dir, ".tmp-publish-deadbeef.csv")
open(tmp_orphan, "wb").write(b"partial")
create_app(data_dir=data_dir, downstream_dir=down_dir)
check("5c 遗留临时文件被清理", not os.path.exists(tmp_orphan))

# 5d 下游目录出现未登记游离文件
stranger = os.path.join(down_dir, "stranger.csv")
open(stranger, "wb").write(b"x")
create_app(data_dir=data_dir, downstream_dir=down_dir)
check("5d 游离文件被移入 quarantine",
      not os.path.exists(stranger)
      and any(f.endswith("stranger.csv")
              for f in os.listdir(os.path.join(data_dir, "quarantine"))))

# 5e 模拟“文件已改名但事务未提交”：文件内容比登记新，重启应回滚到登记版本
with app3.storage._connect() as conn:
    old_csv = app3.storage._render_version(conn, reg["current_version"])
newer = render_csv([{"sample_no": "ZX", "analyte": "氨氮", "concentration": 9.9}])
app3.storage._atomic_write(newer)
check("预置：文件已被换成未登记的新版本",
      open(live, "rb").read().decode() != old_csv)
app4 = create_app(data_dir=data_dir, downstream_dir=down_dir)
check("5e 改名后未提交 → 恢复为登记的完整旧版",
      open(live, "rb").read().decode() == old_csv)
events = app4.storage.recent_events(50)
check("恢复动作写入事件日志", any(e["kind"] == "recover" for e in events))

print("== 6. 会话取消 ==")
r = upload(client, "batch_v1.csv", "CANCEL-ME")
tok = re.search(rb'/publish/([0-9a-f]+)', r.data).group(1).decode()
v_before = app.storage.current_registry()["current_version"]
client.post(f"/cancel/{tok}")
check("取消后发布被拒绝（409 且会话 cancelled）",
      client.post(f"/publish/{tok}").status_code == 409
      and app.storage.get_session(tok)["status"] == "cancelled")
check("取消不影响当前版本",
      app.storage.current_registry()["current_version"] == v_before)

# ---------------- 7. 串号核查 ----------------
print("== 7. 串号核查 ==")

# 7a 算法单元测试
costs = [[1.0, 0.0], [0.0, 1.0]]
best, optima = optimal_assignments(costs)
check("唯一最优指派（交换）", best == 0.0 and optima == [(1, 0)],
      f"best={best} optima={optima}")
best, optima = optimal_assignments([[0.0, 0.0], [0.0, 0.0]])
check("并列最优被全部列出", best == 0.0 and len(optima) == 2, f"optima={optima}")
pts = possible_targets(["A", "B"], [(0, 1), (1, 0)])
check("多最优时逐样本列出可能编号",
      pts[0]["candidates"] == ["A", "B"] and pts[1]["candidates"] == ["A", "B"])
common, per = common_analytes(
    [{"sample_no": "S1", "analyte": "氨氮", "concentration": 1.0},
     {"sample_no": "S1", "analyte": "总磷", "concentration": 0.1}],
    [{"sample_no": "S1", "analyte": "氨氮", "concentration": 1.0}],
    ["S1"])
check("共有项目组与缺项对照", common == ["氨氮"] and per[0]["missing_in_cur"] == ["总磷"],
      f"common={common} per={per}")
remapped = apply_mapping(
    [{"sample_no": "A", "analyte": "x", "concentration": 1.0},
     {"sample_no": "B", "analyte": "x", "concentration": 2.0},
     {"sample_no": "C", "analyte": "x", "concentration": 3.0}],
    {"A": "B", "B": "A"})
check("更名映射只置换编号、浓度与其他行不变",
      remapped[0] == {"sample_no": "B", "analyte": "x", "concentration": 1.0}
      and remapped[1] == {"sample_no": "A", "analyte": "x", "concentration": 2.0}
      and remapped[2] == {"sample_no": "C", "analyte": "x", "concentration": 3.0})

# 7b 端到端：独立环境，v1 为参考版，v2 把 S001/S002 贴反
tmp2 = tempfile.mkdtemp(prefix="wq-selftest-swap-")
data2 = os.path.join(tmp2, "data")
down2 = os.path.join(tmp2, "downstream")
app2 = create_app(data_dir=data2, downstream_dir=down2)
client2 = app2.test_client()
live2 = os.path.join(down2, LIVE_FILENAME)


def csv_of(pairs):
    lines = ["样本号,检测项目,浓度(mg/L)"]
    lines += [f"{s},{a},{c}" for s, a, c in pairs]
    return "\n".join(lines) + "\n"


def publish_text(cli, text, batch_no):
    resp = cli.post("/preview",
                    data={"batch_no": batch_no,
                          "csv_file": (io.BytesIO(text.encode("utf-8")), "b.csv")},
                    content_type="multipart/form-data")
    assert resp.status_code == 200, resp.data[:200]
    token = re.search(rb'/publish/([0-9a-f]+)', resp.data).group(1).decode()
    assert cli.post(f"/publish/{token}").status_code == 302
    return app2.storage.current_registry()["current_version"]


def analyze(cli, ref, cur, samples):
    return cli.post("/swap-check/analyze",
                    data={"ref": ref, "cur": cur, "sample": samples})


V_REF = csv_of([("S001", "氨氮", 1.0), ("S001", "总磷", 0.10),
                ("S002", "氨氮", 2.0), ("S002", "总磷", 0.20),
                ("S003", "氨氮", 3.0), ("S003", "总磷", 0.30),
                ("S004", "氨氮", 4.0), ("S004", "总磷", 0.40)])
V_SWAP = csv_of([("S001", "氨氮", 2.0), ("S001", "总磷", 0.20),
                 ("S002", "氨氮", 1.0), ("S002", "总磷", 0.10),
                 ("S003", "氨氮", 3.0), ("S003", "总磷", 0.30),
                 ("S004", "氨氮", 4.0), ("S004", "总磷", 0.40)])
check("发布参考版 v1", publish_text(client2, V_REF, "REF-001") == 1)
check("发布疑似贴错版 v2", publish_text(client2, V_SWAP, "CUR-002") == 2)

resp = client2.get("/swap-check")
check("核查页 200 且列出两版", resp.status_code == 200
      and "v1".encode() in resp.data and "v2".encode() in resp.data)
resp = client2.get("/swap-check?ref=1&cur=2")
check("圈选页列出两版共有样本号", resp.status_code == 200
      and all(f'value="S00{i}"'.encode() in resp.data for i in range(1, 5)))
resp = client2.get("/swap-check?ref=2&cur=2")
check("参考版与当前版相同被拒", "不能相同".encode() in resp.data)

resp = analyze(client2, 1, 2, ["S001", "S002"])
body = resp.data.decode()
swap_token = re.search(r'/swap-check/confirm/([0-9a-f]+)', body)
check("唯一最优且优于同名配对 → 给出更名建议",
      resp.status_code == 200 and "建议更名" in body and swap_token, body[:200])
check("建议页注明两版编号", "v1" in body and "v2" in body)
check("建议映射为 S001→S002、S002→S001",
      "S001" in body and "S002" in body and "确认更名并发布新版本" in body)
swap_token = swap_token.group(1)

resp = client2.post(f"/swap-check/confirm/{swap_token}")
check("确认更名后 302 回首页", resp.status_code == 302)
reg2 = app2.storage.current_registry()
check("更名发布为 v3 且批次号沿用当前批", reg2["current_version"] == 3
      and reg2["batch_no"] == "CUR-002", f"got {dict(reg2)}")
rows2 = {(r["sample_no"], r["analyte"]): r["concentration"]
         for r in app2.storage.current_rows()}
check("更名后编号被置换且浓度不变",
      rows2 == {("S001", "氨氮"): 1.0, ("S001", "总磷"): 0.10,
                ("S002", "氨氮"): 2.0, ("S002", "总磷"): 0.20,
                ("S003", "氨氮"): 3.0, ("S003", "总磷"): 0.30,
                ("S004", "氨氮"): 4.0, ("S004", "总磷"): 0.40},
      f"got {rows2}")
check("更名后文件哈希与登记一致",
      hashlib.sha256(open(live2, "rb").read()).hexdigest() == reg2["content_hash"])

# 7c 同名配对已最优 → 无串号
resp = analyze(client2, 1, 3, ["S003", "S004"])
body = resp.data.decode()
check("同名配对最优 → 未发现串号、无确认入口",
      "未发现串号" in body and "/swap-check/confirm/" not in body, body[:200])

# 7d 圈选数量校验
check("只选 1 个样本被拒", analyze(client2, 1, 3, ["S001"]).status_code == 302)
check("选 9 个样本被拒",
      analyze(client2, 1, 3, [f"X{i}" for i in range(9)]).status_code == 302)
check("非两版共有样本被拒",
      analyze(client2, 1, 3, ["S001", "NOPE"]).status_code == 302)

# 7e 缺项：v4 中 S003 只剩氨氮一个项目
V_MISSING = csv_of([("S001", "氨氮", 1.0), ("S001", "总磷", 0.10),
                    ("S002", "氨氮", 2.0), ("S002", "总磷", 0.20),
                    ("S003", "氨氮", 3.0),
                    ("S004", "氨氮", 4.0), ("S004", "总磷", 0.40)])
check("发布缺项版 v4", publish_text(client2, V_MISSING, "CUR-004") == 4)
resp = analyze(client2, 3, 4, ["S003", "S004"])
body = resp.data.decode()
check("共有项目不足两个 → 提示缺项且不给建议",
      "缺项" in body and "/swap-check/confirm/" not in body, body[:200])
check("缺项页注明两版编号", "v3" in body and "v4" in body)

# 7f 多最优：v5/v6 中 S003 与 S004 浓度完全相同
V_TWIN = csv_of([("S001", "氨氮", 1.0), ("S001", "总磷", 0.10),
                 ("S002", "氨氮", 2.0), ("S002", "总磷", 0.20),
                 ("S003", "氨氮", 3.0), ("S003", "总磷", 0.30),
                 ("S004", "氨氮", 3.0), ("S004", "总磷", 0.30)])
check("发布孪生版 v5", publish_text(client2, V_TWIN, "CUR-005") == 5)
check("发布孪生版 v6", publish_text(client2, V_TWIN, "CUR-006") == 6)
resp = analyze(client2, 5, 6, ["S003", "S004"])
body = resp.data.decode()
check("并列最优 → 列出可能编号且不允许确认更名",
      "并列" in body and "不允许确认更名" in body
      and "/swap-check/confirm/" not in body, body[:200])
check("多最优页列出 S003/S004 互为其候选", "S003、S004" in body)

# 7g 确认前当前版被他人替换 → 旧建议失效，原文件和登记不变
V_SWAP2 = csv_of([("S001", "氨氮", 2.0), ("S001", "总磷", 0.20),
                  ("S002", "氨氮", 1.0), ("S002", "总磷", 0.10),
                  ("S003", "氨氮", 3.0), ("S003", "总磷", 0.30),
                  ("S004", "氨氮", 3.0), ("S004", "总磷", 0.30)])
check("发布再次贴错版 v7", publish_text(client2, V_SWAP2, "CUR-007") == 7)
resp = analyze(client2, 6, 7, ["S001", "S002"])
stale_token = re.search(r'/swap-check/confirm/([0-9a-f]+)',
                        resp.data.decode()).group(1)
V_OTHER = csv_of([("S001", "氨氮", 2.0), ("S001", "总磷", 0.20),
                  ("S002", "氨氮", 1.0), ("S002", "总磷", 0.10),
                  ("S003", "氨氮", 3.0), ("S003", "总磷", 0.30),
                  ("S004", "氨氮", 3.5), ("S004", "总磷", 0.35)])
check("他人抢先发布 v8", publish_text(client2, V_OTHER, "OTHER-008") == 8)
hash_v8 = hashlib.sha256(open(live2, "rb").read()).hexdigest()
resp = client2.post(f"/swap-check/confirm/{stale_token}")
body = resp.data.decode()
check("确认时版本已被替换 → 409 且提示建议失效",
      resp.status_code == 409 and "已失效" in body and "原文件和登记" in body,
      f"{resp.status_code} {body[:200]}")
reg2 = app2.storage.current_registry()
check("失效后登记仍为 v8 且文件未变",
      reg2["current_version"] == 8
      and hashlib.sha256(open(live2, "rb").read()).hexdigest() == hash_v8)
check("失效建议会话被标记 obsolete",
      app2.storage.get_swap_session(stale_token)["status"] == "obsolete")

# 7h 更名建议可放弃；放弃后确认被拒
V_SWAP3 = csv_of([("S001", "氨氮", 1.0), ("S001", "总磷", 0.10),
                  ("S002", "氨氮", 2.0), ("S002", "总磷", 0.20),
                  ("S003", "氨氮", 3.0), ("S003", "总磷", 0.30),
                  ("S004", "氨氮", 3.5), ("S004", "总磷", 0.35)])
check("发布再次贴错版 v9", publish_text(client2, V_SWAP3, "CUR-009") == 9)
resp = analyze(client2, 8, 9, ["S001", "S002"])
cancel_token = re.search(r'/swap-check/confirm/([0-9a-f]+)',
                         resp.data.decode()).group(1)
client2.post(f"/swap-check/cancel/{cancel_token}")
resp = client2.post(f"/swap-check/confirm/{cancel_token}")
check("放弃后确认被拒（409 且会话 cancelled）",
      resp.status_code == 409
      and app2.storage.get_swap_session(cancel_token)["status"] == "cancelled")
check("放弃不影响当前版本",
      app2.storage.current_registry()["current_version"] == 9)

shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(tmp2, ignore_errors=True)
print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
