"""跨平台进程管理：kill 进程树 / 平台适配的 Popen 参数 / PID 存活判断。"""
import os
import platform
import subprocess
import time

IS_WINDOWS = platform.system() == "Windows"


def popen_kwargs():
    """启动受管子进程时的平台参数。
    POSIX: 新建会话（setsid），kill_tree 可整组击杀；
    Windows: taskkill /T 按父进程杀树，无需新组。"""
    if IS_WINDOWS:
        return {}
    return {"start_new_session": True}


def kill_tree(pid: int, grace_s: float = 2.0):
    """强杀进程及其全部子孙。返回 True=确认已死。"""
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, timeout=15)
        except Exception as e:  # noqa: BLE001
            _log.warning(f"kill_tree taskkill 失败 pid={pid}: {e}")
        return not pid_alive(pid, timeout=grace_s + 3)
    # POSIX
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return not pid_alive(pid)
    for sig, wait_s in ((15, grace_s), (9, 3.0)):  # SIGTERM then SIGKILL
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return True
        if pid_alive(pid, timeout=wait_s):
            continue
        return True
    return not pid_alive(pid)


def pid_alive(pid: int, timeout: float = 0.0) -> bool:
    """timeout>0 时在时限内轮询存活（用于确认死亡）。"""
    deadline = time.time() + timeout
    while True:
        alive = _pid_alive_now(pid)
        if not alive or time.time() >= deadline:
            return alive
        time.sleep(0.2)


def _pid_alive_now(pid: int) -> bool:
    if IS_WINDOWS:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10).stdout
            return str(pid) in out
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            state = f.read().rsplit(b")", 1)[-1].split()[0]
        if state == b"Z":       # 僵尸进程视为已死
            return False
    except (FileNotFoundError, PermissionError, IndexError):
        pass
    return True


class _NoopLogger:
    def warning(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass


_log = _NoopLogger()


def set_logger(logger):
    global _log
    _log = logger
