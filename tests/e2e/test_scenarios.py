"""端到端场景测试：真实 bridge 子进程 × welink-cli/codeagent 模拟器。

覆盖设计 v1.0 验收链路：轮询→路由→确认门（裸答/修改/取消/超时/缺参补全）→
并发/exclusive/队列→心跳→停止（直接/指认/全部）→超时强杀→异常自终止→
常驻崩溃重启→bridge 崩溃恢复+队列续跑→非本人过滤→代操作拒绝→429 退避→
长消息分段→状态指令→未知模板→批处理。
"""
import time

import pytest

from harness import SimEnv


def spawn_resp(user_kw, text):
    return {"contains": user_kw, "text": text}


def wait_no_new(env, bridge_idx, seconds=1.5):
    """静默 seconds 后断言 bridge 消息总数保持 bridge_idx 不变。"""
    time.sleep(seconds)
    assert len(env.bridge_texts()) == bridge_idx, "不应有新 bridge 消息"


# ---------------- 基础链路 ----------------

def test_chat_reply(tmp_path):
    env = SimEnv(tmp_path, scenario={"resident": {"default": "你好，我是助手。"}})
    with env:
        env.user_say("你好")
        c = env.wait_for("我是助手")
        assert c.startswith("🤖 [Agent]")
        time.sleep(1.0)
        assert len(env.bridge_texts()) == 1  # 安静：只有一条


def test_lifecycle_confirm_run_report(tmp_path):
    scen = {"resident": {"responses": [spawn_resp(
        "跑一下", "收到，开始执行。\n[[SPAWN_TASK: t-fast tag=0115]]")],
        "default": "好"},
        "tasks": {"name:t-fast": {"mode": "ok", "final": "结论：构建成功 R1"}}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("跑一下 fast tag=0115")
        gate = env.wait_for("[任务#1 t-fast 0115]")
        assert "🔐" in gate and "echo build TAG=0115" in gate
        assert "将逐字执行的命令" in gate and "（消息提取）" in gate
        assert "tag = '0115'" in gate
        env.user_say("确认")
        env.wait_for("参数已确认，进入排队")
        rep = env.wait_for("结论：构建成功 R1")
        assert "✅" in rep and "[任务#1" in rep and "完成（耗时" in rep
        assert not any("📝" in t for t in env.bridge_texts())  # 无流式进度泄漏
        log = env.work / "task_logs" / "task-1-t-fast.log"
        assert "构建成功" in log.read_text(encoding="utf-8")


def test_gate_modify_params(tmp_path):
    scen = {"resident": {"responses": [spawn_resp(
        "跑", "[[SPAWN_TASK: t-fast tag=0115]]")]},
        "tasks": {"name:t-fast": {"mode": "ok", "final": "DONE-MOD"}}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("跑 fast")
        env.wait_for("echo build TAG=0115")
        env.user_say("tag=9999")
        gate2 = env.wait_for("（更新确认）")
        assert "echo build TAG=9999" in gate2 and "（你修改）" in gate2
        env.user_say("确认")
        env.wait_for("DONE-MOD")


def test_gate_cancel(tmp_path):
    scen = {"resident": {"default": "[[SPAWN_TASK: t-fast]]"}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("跑 fast")
        env.wait_for("🔐")
        b = len(env.bridge_texts())
        env.user_say("取消")
        env.wait_for("已取消，未执行")
        wait_no_new(env, b + 1)  # 除取消响应外再无 bridge 消息


def test_gate_timeout(tmp_path):
    scen = {"resident": {"default": "[[SPAWN_TASK: t-fast]]"}}
    env = SimEnv(tmp_path, scenario=scen, cfg_over={"confirm": {"timeout": 2}})
    with env:
        env.user_say("跑 fast")
        env.wait_for("🔐")
        rep = env.wait_for("确认超时")
        assert "[任务#1" in rep and "未执行" in rep
        time.sleep(1.0)
        assert not any("完成（耗时" in t for t in env.bridge_texts())


def test_required_param_missing_then_fill(tmp_path):
    scen = {"resident": {"responses": [spawn_resp("调接口", "[[SPAWN_TASK: t-req]]")],
                         "default": "好"},
            "tasks": {"name:t-req": {"mode": "ok", "final": "PROBE-OK"}}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("调接口")
        gate = env.wait_for("缺失待补")
        assert "必填参数 url 缺失" in gate
        env.user_say("url=not-a-url")          # 校验失败
        g2 = env.wait_for("不满足校验")
        assert "url=not-a-url" not in g2 or "缺失待补" in g2
        env.user_say("url=https://x")
        g3 = env.wait_for("curl -sS https://x")
        env.user_say("确认")
        env.wait_for("PROBE-OK")


def test_readonly_skips_gate(tmp_path):
    scen = {"resident": {"responses": [spawn_resp("看日志", "[[SPAWN_TASK: t-ro]]")],
                         "default": "好"},
            "tasks": {"name:t-ro": {"mode": "ok", "final": "LOG-SUMMARY"}}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("看日志")
        ann = env.wait_for("只读任务免确认")
        assert "🚀" in ann and "[任务#1 t-ro" in ann
        assert env.wait_for("LOG-SUMMARY")
        time.sleep(0.8)
        assert not any(t.startswith("🔐") for t in env.bridge_texts())


# ---------------- 并发与寻址 ----------------

def test_three_parallel_disambiguation(tmp_path):
    text = ("[[SPAWN_TASK: t-fast tag=a1]]\n[[SPAWN_TASK: t-fast tag=b2]]\n"
            "[[SPAWN_TASK: t-fast tag=c3]]")
    scen = {"resident": {"responses": [spawn_resp("三件套", "三个一起来。\n" + text)],
                         "default": "好"},
            "tasks": {"1": {"mode": "ok", "final": "R-ONE", "pre_sleep": 0.3},
                      "2": {"mode": "ok", "final": "R-TWO", "pre_sleep": 0.3},
                      "3": {"mode": "ok", "final": "R-THREE", "pre_sleep": 0.3}}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("跑三件套")
        for tid in (1, 2, 3):
            env.wait_for(f"[任务#{tid} t-fast")
        idx = env.snapshot_idx()
        env.user_say("确认")                       # 多门裸答 → 追问
        amb = env.wait_for("有 3 个任务的确认待回复")
        assert amb.startswith("❓")
        env.user_say("#1 确认")
        env.user_say("#2 确认")
        env.user_say("#3 确认")
        for tid, final in ((1, "R-ONE"), (2, "R-TWO"), (3, "R-THREE")):
            rep = env.wait_for(final)
            assert f"[任务#{tid}" in rep
        # 报告与任务一一对应：每条报告只含自己的 final
        reports = [t for t in env.bridge_texts() if "完成（耗时" in t]
        assert len(reports) == 3
        for r in reports:
            assert sum(x in r for x in ("R-ONE", "R-TWO", "R-THREE")) == 1


def test_stop_single_running(tmp_path):
    scen = {"resident": {"responses": [spawn_resp("长任务", "[[SPAWN_TASK: t-hang]]")],
                         "default": "好"},
            "tasks": {"name:t-hang": {"mode": "hang", "children": 1,
                                      "child_sleep": 300,
                                      "parts": ["开始执行 sleep 300"]}}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("跑个长任务")
        env.wait_for("🔐")
        env.user_say("确认")
        pids = env.wait_children(1)
        env.user_say("停止")
        rep = env.wait_for("已被你停止")
        assert "[任务#1 t-hang" in rep and "🛑" in rep
        env.wait_all_children_dead(pids)


def test_stop_disambiguation(tmp_path):
    text = "[[SPAWN_TASK: t-hang tag=x1]]\n[[SPAWN_TASK: t-hang tag=x2]]"
    scen = {"resident": {"responses": [spawn_resp("两个长任务", text)], "default": "好"},
            "tasks": {"default": {"mode": "hang", "children": 1, "child_sleep": 300}}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("来两个长任务")
        env.wait_for("[任务#1 t-hang x1]")
        env.wait_for("[任务#2 t-hang x2]")
        env.user_say("#1 确认")
        env.user_say("#2 确认")
        pids = env.wait_children(2)
        env.user_say("停止")                       # 多个在跑 → 追问
        ask = env.wait_for("当前有多个任务在跑")
        assert ask.startswith("❓") and "#1" in ask and "#2" in ask
        env.user_say("#1")
        rep = env.wait_for("已被你停止")
        assert "[任务#1" in rep
        time.sleep(1.0)
        assert env.pid_alive(pids[1])              # #2 未受影响
        env.user_say("停止全部")
        sa = env.wait_for("已停止全部")
        assert "1 个在跑" in sa
        env.wait_all_children_dead(pids)


def test_exclusive_serialization(tmp_path):
    text = "[[SPAWN_TASK: t-excl tag=e1]]\n[[SPAWN_TASK: t-fast tag=f1]]"
    scen = {"resident": {"responses": [spawn_resp("独占", text)], "default": "好"},
            "tasks": {"name:t-excl": {"mode": "ok", "final": "EXC-DONE",
                                      "pre_sleep": 2.0},
                      "name:t-fast": {"mode": "ok", "final": "FAST-DONE"}}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("独占跑一个再跑快速")
        env.wait_for("[任务#1 t-excl")
        env.wait_for("[任务#2 t-fast")
        env.user_say("#1 确认")
        env.user_say("#2 确认")
        exc_idx = None
        # 等 EXC-DONE 出现，其间断言 FAST-DONE 尚未出现（被 exclusive 挡住）
        t0 = time.time()
        while time.time() - t0 < 20:
            texts = env.bridge_texts()
            if any("FAST-DONE" in t for t in texts):
                pytest.fail("exclusive 任务运行期间普通任务不应完成")
            if any("EXC-DONE" in t for t in texts):
                exc_idx = len(env.msgs())
                break
            time.sleep(0.2)
        assert exc_idx, "等不到 EXC-DONE"
        env.wait_for("FAST-DONE")                  # 然后才轮到
        hb = env.wait_for("⏳")                    # 期间有心跳
        assert "[任务#1 t-excl" in hb


def test_queue_capacity_reject(tmp_path):
    text = ("[[SPAWN_TASK: t-hang tag=q1]]\n[[SPAWN_TASK: t-hang tag=q2]]\n"
            "[[SPAWN_TASK: t-hang tag=q3]]")
    scen = {"resident": {"responses": [spawn_resp("三 hang", text)], "default": "好"},
            "tasks": {"default": {"mode": "hang", "children": 1}}}
    env = SimEnv(tmp_path, scenario=scen,
                 cfg_over={"agent": {"max_concurrent": 1, "queue_capacity": 2}})
    with env:
        env.user_say("来三 hang")
        env.wait_for("[任务#3")
        env.user_say("#1 确认")                    # running
        env.wait_for("参数已确认")
        env.user_say("#2 确认")                    # queued
        env.user_say("#3 确认")                    # 在跑1+排队1=2 已满 → 拒
        rej = env.wait_for("队列已满")
        assert "[任务#3" in rej and "⚠️" in rej
        env.user_say("停止全部")
        env.wait_for("已停止全部")


# ---------------- 超时/异常 ----------------

def test_task_timeout_kills_tree(tmp_path):
    scen = {"resident": {"responses": [spawn_resp("超时", "[[SPAWN_TASK: t-tmo]]")],
                         "default": "好"},
            "tasks": {"name:t-tmo": {"mode": "hang", "children": 1,
                                     "child_sleep": 300}}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("跑会超时的")
        env.wait_for("🔐")
        env.user_say("确认")
        pids = env.wait_children(1)
        rep = env.wait_for("超时")
        assert "进程树已终止" in rep and "[任务#1" in rep
        env.wait_all_children_dead(pids)


def test_agent_anomaly_self_terminate(tmp_path):
    scen = {"resident": {"responses": [spawn_resp("异常", "[[SPAWN_TASK: t-fast]]")],
                         "default": "好"},
            "tasks": {"name:t-fast": {"mode": "anomaly", "children": 1,
                                      "final": "检测到错误输出，已终止子进程并停止（失败简报）"}}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("跑会异常的")
        env.wait_for("🔐")
        env.user_say("确认")
        pids = env.wait_children(1)
        rep = env.wait_for("已终止子进程")
        assert "✅" in rep                      # agent 主动收尾，按完成转发
        env.wait_all_children_dead(pids)
        assert not any("超时" in t or "已被你停止" in t for t in env.bridge_texts())


# ---------------- 崩溃与恢复 ----------------

def test_resident_crash_restart(tmp_path):
    scen = {"resident": {"responses": [
        {"contains": "要崩溃", "text": "__CRASH__"},
        {"contains": "还好吗", "text": "没崩，还在"}], "default": "好"}}
    env = SimEnv(tmp_path, scenario=scen,
                 cfg_over={"agent": {"resident_timeout": 3}})
    with env:
        env.user_say("要崩溃")
        env.wait_for("⏱️ 主 agent 响应超时")
        ctl = env.wait_for("正在重启")
        assert ctl.startswith("📡")
        env.user_say("还好吗")                      # 重启后应正常服务
        env.wait_for("没崩，还在", timeout=30)


def test_bridge_hard_kill_recovery_and_queue_resume(tmp_path):
    text = "[[SPAWN_TASK: t-hang tag=r1]]\n[[SPAWN_TASK: t-fast tag=r2]]"
    scen = {"resident": {"responses": [spawn_resp("恢复", text),
                                       {"contains": "【消息", "text": "BATCH-ACK"},
                                       {"contains": "hello2", "text": "HELLO2-ACK"}],
                         "default": "好"},
            "tasks": {"name:t-hang": {"mode": "hang", "children": 1,
                                      "child_sleep": 300},
                      "2": {"mode": "ok", "final": "RESUMED-R2"}}}
    env = SimEnv(tmp_path, scenario=scen,
                 cfg_over={"agent": {"max_concurrent": 1}})
    env.start()
    try:
        env.user_say("跑 hang+fast 我要演练恢复")
        env.wait_for("[任务#1 t-hang r1]")
        env.wait_for("[任务#2 t-fast r2]")
        env.user_say("#1 确认")                    # running
        env.user_say("#2 确认")                    # queued (并发=1)
        pids = env.wait_children(1)
        env.wait_for("参数已确认")
        hang_sim_pids_before = _python_procs()
    finally:
        env.proc.kill()                            # 硬杀 bridge：残留孤儿
        env.proc.wait(timeout=10)
    alive_hang = [p for p in _python_procs() if p in hang_sim_pids_before]
    time.sleep(0.5)
    env.start()                                    # 重启：kill_stale + 队列续跑
    try:
        env.wait_all_children_dead(pids, timeout=25)   # 旧任务子进程被清
        rep = env.wait_for("RESUMED-R2", timeout=30)
        assert "[任务#2 t-fast r2]" in rep
        env.user_say("hello2")                     # watermark 续跑：新消息仍处理
        env.wait_for("HELLO2-ACK")
        # 中断任务状态落盘：不再有 #1 的完成/停止报告
        time.sleep(1.0)
        assert not any("[任务#1" in t and ("完成（耗时" in t or "已被你停止" in t)
                       for t in env.bridge_texts())
    finally:
        env.stop()


def _python_procs():
    import subprocess
    out = subprocess.run(["pgrep", "-f", "codeagent.py"], capture_output=True,
                         text=True).stdout
    return {int(x) for x in out.split()}


# ---------------- 过滤与安全 ----------------

def test_nonself_skipped_and_deny(tmp_path):
    env = SimEnv(tmp_path, scenario={"resident": {"default": "REPLIES"}})
    with env:
        env.user_say("跑一下别人触发的", sender="other")
        wait_no_new(env, 0)
        env.user_say("帮我把结果发邮件给他人")
        d = env.wait_for("无法代为操作")
        assert "⚠️" in d


def test_429_backoff(tmp_path):
    env = SimEnv(tmp_path, scenario={"resident": {"default": "429TEST"}})
    env.inject_429(2)
    with env:
        env.user_say("随便说点什么触发回复")
        env.wait_for("429TEST", timeout=25)        # 退避后最终送达
        assert env.proc.poll() is None


def test_long_report_chunking(tmp_path):
    long_final = "报告头部MARK\n" + "很长" * 60 + "\nDONE-TAIL-MARK"
    scen = {"resident": {"responses": [spawn_resp("长报告", "[[SPAWN_TASK: t-fast]]")],
                         "default": "好"},
            "tasks": {"name:t-fast": {"mode": "ok", "final": long_final}}}
    env = SimEnv(tmp_path, scenario=scen, cfg_over={"welink": {"chunk_limit": 100}})
    with env:
        env.user_say("跑长报告任务")
        env.wait_for("🔐")
        env.user_say("确认")
        env.wait_for("DONE-TAIL-MARK")
        chunks = [t for t in env.bridge_texts() if t.startswith("🤖 [Agent]")]
        assert len(chunks) >= 2
        # 分段续段也带前缀 → 不会被回读成用户消息（无级联）
        wait_no_new(env, len(env.bridge_texts()))
        nonself = [m for m in env.msgs() if not m["content"].startswith(("🤖", "🔐", "❓", "📡"))
                   and m["content"] not in ("跑长报告任务", "确认")]
        assert not nonself


# ---------------- 指令与杂项 ----------------

def test_status_command(tmp_path):
    scen = {"resident": {"responses": [spawn_resp("长任务", "[[SPAWN_TASK: t-hang]]")],
                         "default": "好"},
            "tasks": {"default": {"mode": "hang", "children": 1}}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("跑长任务")
        env.wait_for("🔐")
        env.user_say("确认")
        env.wait_children(1)
        env.user_say("状态")
        st = env.wait_for("在跑 1")
        assert "#1 t-hang" in st and "已跑" in st
        env.user_say("停止全部")
        env.wait_for("已停止全部")


def test_unknown_template(tmp_path):
    scen = {"resident": {"default": "[[SPAWN_TASK: no-such x=1]]"}}
    env = SimEnv(tmp_path, scenario=scen)
    with env:
        env.user_say("用不存在的模板")
        err = env.wait_for("不存在")
        assert "no-such" in err and "❌" in err
        time.sleep(1.0)
        assert not any(t.startswith("🔐") for t in env.bridge_texts())


def test_batch_two_messages_one_turn(tmp_path):
    scen = {"resident": {"default": "BATCH-REPLIES"}}
    env = SimEnv(tmp_path, scenario=scen, cfg_over={"welink": {"poll_interval": 1.5}})
    with env:
        env.user_say("第一条随便")
        env.user_say("第二条随便")                 # 同一轮询窗口
        env.wait_for("BATCH-REPLIES")
        time.sleep(1.2)
        assert sum(1 for t in env.bridge_texts() if "BATCH-REPLIES" in t) == 1


def test_heartbeat_present(tmp_path):
    scen = {"resident": {"responses": [spawn_resp("心跳", "[[SPAWN_TASK: t-hang]]")],
                         "default": "好"},
            "tasks": {"default": {"mode": "hang", "children": 1, "child_sleep": 300}}}
    env = SimEnv(tmp_path, scenario=scen,
                 cfg_over={"agent": {"heartbeat_interval": 1.2}})
    with env:
        env.user_say("跑心跳任务")
        env.wait_for("🔐")
        env.user_say("确认")
        hb = env.wait_for("⏳")
        assert "[任务#1 t-hang" in hb and "剩余" in hb
        env.user_say("停止全部")
        env.wait_for("已停止全部")
