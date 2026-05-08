"""Phase 1.6 示例场景：5 个端到端用例。

运行方式：

    python -m klawbot.examples list                # 列出全部示例
    python -m klawbot.examples run notepad         # 单跑一个
    python -m klawbot.examples run all             # 顺序跑全部（耗时；每例之间 30s 间隔）

每个示例只是一段 *自然语言指令*，交给 ReAct 主循环执行。
设计目标见 design.md §3.2 / todo.md Phase 1.6。

⚠️ 安全提示：
* `wechat`、`excel` 例子涉及真实应用，会触发 confirm_critical 档位的 HITL；
* 跑前请在 LLM 代理就绪、Win+R 别名自检通过、目标程序已安装。
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from rich.console import Console

from .config import load_config
from .dpi import set_dpi_aware

_console = Console()


@dataclass
class Example:
    name: str
    title: str
    instruction: str
    autonomy: str = "confirm_critical"
    notes: str = ""


EXAMPLES: Dict[str, Example] = {
    "notepad": Example(
        name="notepad",
        title="记事本：写文本并保存",
        instruction=(
            "用 Win+R 打开记事本（notepad），输入一行『hello from klawbot』，"
            "然后按 Ctrl+S，在文件名框 type 完整路径 "
            "C:\\Users\\Public\\klawbot_demo.txt 然后回车保存。"
            "完成后报告：『任务完成: 已保存到该路径』。"
        ),
        autonomy="confirm_critical",
        notes="Phase 0 已验证，可作为冒烟。",
    ),
    "browser": Example(
        name="browser",
        title="浏览器：打开 URL 并描述页面",
        instruction=(
            "用 Win+R 打开默认浏览器，访问 https://example.com，"
            "等页面加载完成（最多 5s），截一张 L1 全屏图，"
            "用 1-2 句中文描述页面主要内容并报告：『任务完成: <描述>』。"
            "全程不要点击任何链接、不要关闭浏览器。"
        ),
        autonomy="confirm_critical",
    ),
    "excel": Example(
        name="excel",
        title="Excel：A1 输入『周报』并保存到桌面",
        instruction=(
            "用 Win+R 打开 Excel（excel），新建空白工作簿，"
            "在 A1 单元格 type『周报』，按回车确认，"
            "然后 Ctrl+S 另存为，在文件名框 type "
            "%USERPROFILE%\\Desktop\\klawbot_weekly.xlsx，回车保存。"
            "完成后报告：『任务完成: 已保存』。"
        ),
        autonomy="confirm_critical",
        notes="需本机已装 Office Excel；若是 WPS 请把 excel 改成 wps。",
    ),
    "wechat": Example(
        name="wechat",
        title="微信：找联系人发送一句消息（HITL 强确认）",
        instruction=(
            "打开 PC 微信主窗口（如已最小化先恢复），"
            "在搜索框输入联系人『文件传输助手』并选中，"
            "在输入框 type『klawbot 自动化测试一下』，按回车发送。"
            "发送前必须等待用户确认（这是 confirm_each 档位的强 HITL）。"
            "完成后报告：『任务完成: 已发送』。"
        ),
        autonomy="confirm_each",
        notes="第一次跑请把自动度强制设为 confirm_each。",
    ),
    "explorer": Example(
        name="explorer",
        title="文件管理器：在指定文件夹新建子文件夹并重命名",
        instruction=(
            "用 Win+E 打开文件资源管理器，"
            "在地址栏 type C:\\Users\\Public 然后回车，"
            "右键空白处选择『新建』→『文件夹』，"
            "在出现的可编辑名称里 type『klawbot_demo_folder』并回车确认。"
            "完成后报告：『任务完成: 已创建』。"
        ),
        autonomy="confirm_critical",
    ),
}


def list_all() -> None:
    for ex in EXAMPLES.values():
        _console.rule(f"[bold]{ex.name}[/bold] · {ex.title}")
        _console.print(f"[dim]autonomy[/dim] = {ex.autonomy}")
        if ex.notes:
            _console.print(f"[yellow]notes[/yellow]: {ex.notes}")
        _console.print(ex.instruction)


def run_one(name: str, override_autonomy: Optional[str] = None) -> int:
    ex = EXAMPLES.get(name)
    if ex is None:
        _console.print(f"[red]unknown example: {name}[/red]")
        return 2

    if sys.platform != "win32":
        _console.print("[red]examples 只能在 Windows 上跑。[/red]")
        return 2

    cfg = load_config()
    cfg.safety.autonomy = override_autonomy or ex.autonomy
    set_dpi_aware()

    from .loop import Agent

    _console.rule(f"[bold green]example: {ex.name}")
    _console.print(f"[bold]任务[/bold]: {ex.instruction}")
    _console.print(f"[dim]autonomy={cfg.safety.autonomy}[/dim]")

    try:
        agent = Agent(cfg)
        result = agent.run(ex.instruction)
    except KeyboardInterrupt:
        _console.print("[yellow]用户中断。[/yellow]")
        return 130
    _console.rule("[bold]结果")
    _console.print(result)
    return 0


def run_all(override_autonomy: Optional[str] = None) -> int:
    rc = 0
    for i, name in enumerate(EXAMPLES.keys()):
        if i > 0:
            _console.print("[dim]30s 冷却中...[/dim]")
            time.sleep(30)
        sub = run_one(name, override_autonomy=override_autonomy)
        if sub != 0:
            rc = sub
            _console.print(f"[red]example {name} failed (rc={sub})；继续下一例。[/red]")
    return rc


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="klawbot.examples")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list", help="列出所有示例")
    p_run = sub.add_parser("run", help="运行某示例 (或 'all')")
    p_run.add_argument("name", help="示例名 / all")
    p_run.add_argument("--autonomy", default=None, choices=["full", "confirm_critical", "confirm_each"])
    args = parser.parse_args(argv)

    if args.cmd in (None, "list"):
        list_all()
        return 0
    if args.cmd == "run":
        if args.name == "all":
            return run_all(override_autonomy=args.autonomy)
        return run_one(args.name, override_autonomy=args.autonomy)
    parser.error("unknown subcommand")
    return 2


if __name__ == "__main__":
    sys.exit(main())
