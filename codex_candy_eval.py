#!/usr/bin/env python3
"""用本地 codex CLI 测试糖果问题，统计 reasoning tokens 并判分。
    python codex_candy_eval.py -m gpt-5.5 -r high -n 5
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata

CODEX_PROMPT = """不使用任何外部工具回答以下问题：

在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）

        苹果味  桃子味  西瓜味
圆形       7      9      8
五角星形   7      6      4
"""

# 正确答案为 21：只要回答中出现独立的 "21"（前后非数字）即判为正确。
ANSWER_PATTERN = re.compile(r"(?<!\d)21(?!\d)")

DEFAULT_TIMEOUT_SECONDS = 180.0
PROGRESS_INTERVAL_SECONDS = 10.0


def resolve_codex_executable() -> str:
    """找到可被 subprocess 直接启动的 codex 命令。

    Windows/npm 安装通常会同时生成 `codex`、`codex.cmd` 和 `codex.ps1`。
    `shutil.which("codex")` 可能先命中无扩展名 shim，CreateProcess 无法直接运行，
    会报 WinError 193；因此 Windows 上优先选择 `.cmd`。
    """
    candidates = (
        ("codex.cmd", "codex.exe", "codex")
        if os.name == "nt"
        else ("codex",)
    )
    for name in candidates:
        exe = shutil.which(name)
        if exe:
            return exe
    raise RuntimeError("找不到 codex 可执行文件，请确认已安装并加入 PATH。")


def _parse_codex_events(output: str) -> tuple[str, dict, list[str]]:
    """Extract the final answer, usage, and structured errors from JSONL output."""
    final_text = ""
    usage: dict = {}
    errors = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                final_text = item.get("text", final_text)
        elif event_type == "turn.completed":
            usage = event.get("usage") or {}
        elif event_type in {"error", "turn.failed"}:
            message = _event_error_message(event)
            if message and (not errors or errors[-1] != message):
                errors.append(message)
    return final_text, usage, errors


def _event_error_message(event: dict) -> str:
    error = event.get("error") or event.get("message") or event
    for _ in range(6):
        if isinstance(error, str):
            try:
                decoded = json.loads(error)
            except json.JSONDecodeError:
                return error
            if decoded == error:
                return error
            error = decoded
            continue
        if isinstance(error, dict):
            if error.get("message"):
                error = error["message"]
                continue
            if error.get("detail"):
                error = error["detail"]
                continue
            if "error" in error:
                error = error["error"]
                continue
        break
    return str(error) if error else ""


def _fallback_error(output: str) -> str:
    non_json_lines = [
        line.strip()
        for line in output.splitlines()
        if line.strip()
        and not line.lstrip().startswith("{")
        and line.strip() != "Reading prompt from stdin..."
    ]
    return "\n".join(non_json_lines[-10:]) or "codex exec failed"


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Force-stop the Codex launcher and any child process it spawned."""
    if os.name == "nt":
        result = None
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if (result is None or result.returncode != 0) and proc.poll() is None:
            proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_codex(
    model: str | None,
    effort: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
):
    exe = resolve_codex_executable()

    cmd = [
        exe, "exec", "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "-s", "read-only",
        # 关闭 codex 的跨会话记忆（~/.codex/memories），避免历史记忆注入提示词、污染
        # 评测结果，保证不同机器/不同记忆状态下结果可复现。等价于 -c features.memories=false。
        "--disable", "memories",
        "-c", f"model_reasoning_effort={effort}",
    ]
    if model:
        cmd += ["-m", model]

    # 多行题目通过 stdin 传入：作为命令行参数时，经 cmd.exe/codex.cmd 包装后换行会被
    # 吞掉，而管道里的内容能完整保留。codex exec 在无位置参数且 stdin 非 TTY 时读 stdin。
    process_group_options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **process_group_options,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    try:
        output, _ = proc.communicate(CODEX_PROMPT, timeout=timeout or None)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(proc)
        output, _ = proc.communicate()
        _, _, event_errors = _parse_codex_events(output)
        error = (event_errors[-1] if event_errors else "") or _fallback_error(output)
        detail = f": {error}" if error != "codex exec failed" else ""
        raise RuntimeError(
            f"codex exec timed out after {timeout:g}s and was terminated{detail}"
        )
    except BaseException:
        _terminate_process_tree(proc)
        raise

    # codex --json emits one JSON event per line. The final answer is the last
    # `agent_message` item; token usage (incl. reasoning) is in `turn.completed`.
    final_text, usage, event_errors = _parse_codex_events(output)
    if proc.returncode != 0:
        raise RuntimeError(event_errors[-1] if event_errors else _fallback_error(output))
    if not final_text:
        if event_errors:
            raise RuntimeError(event_errors[-1])
        raise RuntimeError("codex exec completed without an agent response")

    return (
        final_text,
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("reasoning_output_tokens"),
    )


def get_codex_models(timeout: float = 15.0) -> list[dict]:
    """Read the model catalog exposed by the installed Codex CLI."""
    exe = resolve_codex_executable()
    try:
        proc = subprocess.run(
            [exe, "debug", "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"codex model catalog timed out after {timeout:g}s") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip()
            or proc.stdout.strip()
            or "failed to read the Codex model catalog"
        )
    try:
        models = json.loads(proc.stdout).get("models")
    except (AttributeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex returned an invalid model catalog") from exc
    if not isinstance(models, list):
        raise RuntimeError("Codex returned an invalid model catalog")
    return models


@contextmanager
def report_progress(index: int, total: int, model: str | None, effort: str,
                    timeout: float, use_ansi: bool):
    """Report that the buffered Codex subprocess is still running."""
    label = f"[{index}/{total}]"
    timeout_text = f"{timeout:g}s" if timeout else "off"
    lock = threading.Lock()

    def write_status(message: str) -> None:
        with lock:
            if use_ansi:
                width = max(20, shutil.get_terminal_size((120, 24)).columns - 1)
                status = preview(f"{label} {message}", width)
                sys.stderr.write(f"\r\033[2K{status}")
                sys.stderr.flush()
            else:
                print(f"{label} {message}", file=sys.stderr, flush=True)

    write_status(
        f"Starting Codex: model={model or '<local default>'} "
        f"effort={effort} timeout={timeout_text}"
    )
    stop_event = threading.Event()

    def heartbeat() -> None:
        started = time.monotonic()
        while not stop_event.wait(PROGRESS_INTERVAL_SECONDS):
            elapsed = time.monotonic() - started
            if timeout and elapsed >= timeout:
                return
            write_status(f"Codex is still running ({elapsed:.0f}s elapsed)")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join()
        if use_ansi:
            with lock:
                sys.stderr.write("\r\033[2K")
                sys.stderr.flush()


def non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite number at least 0")
    return parsed


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def char_width(char: str) -> int:
    """终端显示宽度：组合字符 0，东亚全角/宽字符 2，其余 1。"""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def pad(text: str, width: int, align: str) -> str:
    """按显示宽度补空格对齐（中文宽字符按 2 计）。"""
    gap = width - display_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def render_table(headers: list[str], rows: list[list], aligns: list[str]) -> str:
    """原生渲染对齐表格（tabulate "simple" 风格），列宽按显示宽度计算。"""
    str_rows = [[str(c) for c in row] for row in rows]
    widths = [
        max(display_width(headers[i]), *(display_width(r[i]) for r in str_rows)) if str_rows
        else display_width(headers[i])
        for i in range(len(headers))
    ]

    def fmt(cells: list[str]) -> str:
        return "  ".join(pad(cells[i], widths[i], aligns[i]) for i in range(len(headers)))

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in str_rows]
    return "\n".join(lines)


def preview(text: str, limit: int = 40) -> str:
    flat = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\n")
    if display_width(flat) <= limit:
        return flat

    result = []
    width = 0
    for char in flat:
        next_width = char_width(char)
        if width + next_width > limit - 3:
            break
        result.append(char)
        width += next_width
    return "".join(result) + "..."


def _enable_windows_ansi() -> bool:
    """开启 Windows 控制台的 VT 处理，让 ANSI 转义序列（含光标定位）生效。"""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        )
    except Exception:
        return False


def setup_console() -> bool:
    """统一输出为 UTF-8（避免 ✓/✗、中文在 GBK 等控制台编码下抛 UnicodeEncodeError），
    并探测是否可用 ANSI 光标控制做表格原地刷新。

    返回 True 表示可原地重绘；否则（如输出被重定向、或旧版 Windows 无法开 VT）退化为
    结束后一次性打印整张表，避免屏幕上出现 `←[s` 之类的转义乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        return _enable_windows_ansi()
    return True


def main() -> None:
    use_ansi = setup_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-m", "--model", help="Codex model name; omit for the local default.")
    parser.add_argument(
        "-r", "--reasoning-effort", default="medium",
        metavar="EFFORT",
        help="Reasoning effort passed to Codex (default: medium; support is model-specific).",
    )
    parser.add_argument("-n", "--tests", type=positive_int, default=1)
    parser.add_argument(
        "-t", "--timeout", type=non_negative_float, default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"Per-run timeout; 0 disables it (default: {DEFAULT_TIMEOUT_SECONDS:g}).",
    )
    parser.add_argument(
        "--list-models", action="store_true",
        help="List models and reasoning efforts reported by the installed Codex CLI, then exit.",
    )
    args = parser.parse_args()

    if args.list_models:
        try:
            model_rows = []
            for model in get_codex_models():
                efforts = ",".join(
                    level.get("effort", "")
                    for level in model.get("supported_reasoning_levels") or []
                    if level.get("effort")
                )
                model_rows.append([
                    model.get("slug", "-"),
                    efforts or "-",
                    model.get("visibility", "-"),
                ])
            print(render_table(
                ["Model", "Reasoning efforts", "Visibility"],
                model_rows,
                ["left", "left", "left"],
            ))
        except Exception as exc:
            parser.exit(1, f"error: {exc}\n")
        return

    headers = ["Run", "Codex", "In Tok", "Out Tok", "Reason Tok", "Time(s)", "TPS", "OK"]
    aligns = ["right", "left", "right", "right", "right", "right", "right", "center"]
    progress_ansi = use_ansi and sys.stderr.isatty()
    deferred_errors: list[str] = []

    def run_one(index: int) -> tuple[list, bool | None]:
        start = time.perf_counter()
        try:
            with report_progress(
                index, args.tests, args.model, args.reasoning_effort,
                args.timeout, progress_ansi,
            ):
                text, in_tok, out_tok, rea_tok = run_codex(
                    args.model, args.reasoning_effort, args.timeout
                )
            elapsed = time.perf_counter() - start
            tps = out_tok / elapsed if out_tok and elapsed > 0 else None
            ok = bool(ANSWER_PATTERN.search(text))
            return [index, preview(text), in_tok, out_tok, rea_tok, f"{elapsed:.1f}",
                    f"{tps:.1f}" if tps else "-", "✓" if ok else "✗"], ok
        except Exception as exc:
            elapsed = time.perf_counter() - start
            error = f"[{index}/{args.tests}] ERROR: {exc}"
            if progress_ansi:
                deferred_errors.append(error)
            else:
                print(error, file=sys.stderr, flush=True)
            return [index, f"ERROR: {preview(str(exc))}", "-", "-", "-",
                    f"{elapsed:.1f}", "-", "-"], None

    # 串行执行：逐个请求，完成一个立即打印该行结果。
    rows = []
    graded = []
    prev_lines = 0  # 上一次绘制的表格占据的屏幕行数，用于原地重绘时上移光标
    for index in range(1, args.tests + 1):
        row, ok = run_one(index)
        rows.append(row)
        if ok is not None:
            graded.append(ok)
        if use_ansi:
            # 用“行数计数 + 光标上移（CSI A）”替代 save/restore（CSI s/u）。
            # macOS Terminal.app 不支持 CSI s/u，会导致表格每轮向下堆叠、表头重复；
            # 光标上移序列所有常见终端都支持，最稳妥。
            if prev_lines > 0:
                sys.stdout.write(f"\033[{prev_lines}A\033[J")
            table = render_table(headers, rows, aligns)
            sys.stdout.write(table + "\n")
            sys.stdout.flush()
            prev_lines = table.count("\n") + 1
    if not use_ansi:
        print(render_table(headers, rows, aligns), flush=True)
    for error in deferred_errors:
        print(error, file=sys.stderr, flush=True)

    correct = sum(graded)
    print(f"\nGraded {len(graded)}/{args.tests}  correct={correct}  "
          f"accuracy={correct / len(graded) * 100:.1f}%"
          if graded else f"\nGraded 0/{args.tests}")
    if len(graded) != args.tests:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
