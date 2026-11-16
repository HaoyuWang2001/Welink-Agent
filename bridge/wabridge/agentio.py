"""CodeAgent 协议层：常驻主 agent（stream-json 多轮）与任务执行子 agent。"""
import json
import subprocess
import threading
import time
from pathlib import Path

from . import procs


def base_cmd(cfg, effort=None):
    cmd = [cfg.codeagent_bin, "-p",
           "--input-format", "stream-json",
           "--output-format", "stream-json",
           "--verbose"]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--dangerously-skip-permissions"]
    return cmd


class ResidentAgent:
    """常驻主 agent：接收批量消息，保持上下文；崩溃由 on_dead 回调触发重启。"""

    def __init__(self, cfg, logger, on_dead, prompt_text: str):
        self.cfg = cfg
        self.log = logger
        self.on_dead = on_dead
        self.base_prompt = prompt_text
        self.proc = None
        self.alive = False
        self.session_id = None
        self.stdin_lock = threading.Lock()
        self._lock = threading.Lock()
        self.result_event = threading.Event()
        self.assistant_parts = []
        self.timeout = cfg.resident_timeout
        self.registry = None   # ProcRegistry，main 注入

    def _rendered_prompt_file(self):
        p = self.cfg.workdir / "resident_prompt_rendered.md"
        p.write_text(self.base_prompt, encoding="utf-8")
        return str(p)

    def start(self):
        cmd = base_cmd(self.cfg, effort="low")
        pf = self._rendered_prompt_file()
        cmd += ["--append-system-prompt-file", pf]
        self.log.info(f"resident: 启动 {' '.join(cmd[:4])}... prompt={pf}")
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                cwd=str(self.cfg.workdir.parent), bufsize=1,
                **procs.popen_kwargs())
        except Exception as e:  # noqa: BLE001
            self.log.error(f"resident 启动失败: {e}")
            raise
        self.alive = True
        if self.registry:
            self.registry.register(self.proc.pid, "resident", "main-agent")
        threading.Thread(target=self._read_loop, daemon=True, name="resident-reader").start()

    def send(self, content) -> str:
        if not self.alive or self.proc is None:
            raise RuntimeError("resident agent not alive")
        line = json.dumps({"type": "user", "message": {"role": "user", "content": content}},
                          ensure_ascii=False) + "\n"
        with self._lock:
            self.assistant_parts = []
            self.result_event.clear()
        with self.stdin_lock:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self.alive = False
                raise RuntimeError(f"stdin 写入失败: {e}")
        if not self.result_event.wait(timeout=self.timeout):
            return f"⏱️ 主 agent 响应超时（{self.timeout:.0f}s）"
        with self._lock:
            text = "".join(self.assistant_parts).strip()
        return text or "(主 agent 无文本输出)"

    def _read_loop(self):
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = evt.get("type")
                if t == "system" and evt.get("subtype") == "init":
                    self.session_id = evt.get("session_id")
                    self.log.info(f"resident init session_id={self.session_id}")
                elif t == "assistant":
                    for part in evt.get("message", {}).get("content", []):
                        if part.get("type") == "text":
                            with self._lock:
                                self.assistant_parts.append(part["text"])
                elif t == "result":
                    self.result_event.set()
                elif t == "system" and evt.get("subtype") == "error":
                    self.log.error(f"resident 系统错误: {evt}")
        except Exception as e:  # noqa: BLE001
            self.log.error(f"resident reader 异常: {e}")
        finally:
            was_alive = self.alive
            self.alive = False
            if was_alive and self.on_dead:
                try:
                    self.on_dead("reader_thread_exit")
                except Exception:  # noqa: BLE001
                    pass

    def stop(self):
        self.alive = False
        if self.proc:
            pid = self.proc.pid
            try:
                self.proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                procs.kill_tree(pid)
            if self.registry:
                self.registry.unregister(pid)


def build_task_prompt(task, template, log_path):
    src = "、".join(f"{k}({s})" for k, s in sorted(task.sources.items())) or "无参数"
    lines = [f"[任务#{task.tid} {task.short}] 执行预定义任务「{task.name}」。"]
    lines.append(f"参数: " + ", ".join(f"{k}={v}" for k, v in sorted(task.params.items())))
    lines.append(f"参数来源: {src}")
    lines.append("执行步骤——必须逐字按顺序执行以下命令，禁止增删改命令、禁止重试、"
                 "禁止执行清单外命令、禁止修改源码；若任一步骤输出非零退出或含 error/Traceback 等异常，"
                 "立即终止你自己启动的子进程，输出失败简报后停止，不得尝试任何补救：")
    for i, step in enumerate(template.render_steps(task.params), 1):
        lines.append(f"{i}. {step}")
    lines.append(f"简报格式要求: {template.report}")
    lines.append(f"完整过程自行记录到: {log_path}")
    return "\n".join(lines)


class TaskRunner:
    """一个任务一个子进程：stream-json 单轮执行，安静模式（进度只进日志），
    超时/用户停止由本类 kill_tree 兜底。"""

    def __init__(self, cfg, logger, registry, task, template, on_heartbeat=None):
        self.cfg = cfg
        self.log = logger
        self.proc_registry = registry
        self.task = task
        self.template = template
        self.on_heartbeat = on_heartbeat
        self.proc = None

    def run(self):
        task = self.task
        prompt = build_task_prompt(task, self.template, task.log_path)
        cmd = base_cmd(self.cfg)
        cwd = str(Path(task.cwd)) if task.cwd else str(self.cfg.workdir.parent)
        try:
            with open(task.log_path, "w", encoding="utf-8") as logf:
                self.proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    cwd=cwd, bufsize=1, **procs.popen_kwargs())
                self.proc_registry.register(self.proc.pid, "task", task.label())
                task.proc_pid = self.proc.pid
                return self._stream(logf, prompt)
        except Exception as e:  # noqa: BLE001
            self.log.exception(f"task {task.label()} 启动异常: {e}")
            return "error", f"启动子 agent 失败: {e}"
        finally:
            if self.proc:
                self.proc_registry.unregister(self.proc.pid)

    def _stream(self, logf, prompt):
        task = self.task
        user_line = json.dumps({"type": "user",
                                "message": {"role": "user", "content": prompt}},
                               ensure_ascii=False) + "\n"
        try:
            self.proc.stdin.write(user_line)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            return "error", f"写 stdin 失败: {e}"

        parts = []
        killed = {"timeout": False}

        def _kill_timeout():
            killed["timeout"] = True
            procs.kill_tree(task.proc_pid)

        timer = threading.Timer(task.timeout, _kill_timeout)
        timer.daemon = True
        timer.start()
        hb = task.heartbeat
        hb_thread = None
        if hb and hb > 0:
            hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True,
                                         name=f"hb-{task.tid}")
            hb_thread.start()

        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                logf.write(line)
                logf.flush()
                s = line.strip()
                if not s:
                    continue
                try:
                    evt = json.loads(s)
                except json.JSONDecodeError:
                    continue
                t = evt.get("type")
                if t == "assistant":
                    for part in evt.get("message", {}).get("content", []):
                        if part.get("type") == "text":
                            parts.append(part["text"])
                elif t == "result":
                    break
                elif t == "system" and evt.get("subtype") == "error":
                    logf.write(f"[[sim-error]] {json.dumps(evt, ensure_ascii=False)}\n")
        except Exception as e:  # noqa: BLE001
            self.log.error(f"task {task.label()} 读流异常: {e}")
        finally:
            timer.cancel()
            try:
                self.proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            if self.proc.poll() is None:
                procs.kill_tree(task.proc_pid, grace_s=1.0)
            try:
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

        if task.stop_event.is_set():
            return "killed", ""
        if killed["timeout"]:
            return "timeout", f"执行超时（{task.timeout:.0f}s）"
        text = "".join(parts).strip()
        if not text:
            return "empty", "(子 agent 无输出，详见日志)"
        return "done", text

    def _heartbeat_loop(self):
        task = self.task
        while not task.stop_event.wait(task.heartbeat):
            if self.proc is None or self.proc.poll() is not None:
                return
            elapsed = time.time() - (task.started_ts or time.time())
            self.log.info(f"heartbeat {task.label()} elapsed={elapsed:.0f}")
            if self.on_heartbeat:
                try:
                    self.on_heartbeat(task, elapsed)
                except Exception as e:  # noqa: BLE001
                    self.log.warning(f"heartbeat 发送失败 {task.label()}: {e}")
