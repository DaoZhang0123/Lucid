"""lucid CLI 入口：

    python -m lucid "打开记事本，输入 hello"
    python -m lucid --smoke-test "你是谁？一句话。"

默认走本地 LiteLLM 代理（OpenAI 兼容 /chat/completions），
读取 config.toml 中 [llm.proxy].base_url / model / api_key，
api_key 留空时回退读 LITELLM_MASTER_KEY 环境变量。
"""
from __future__ import annotations

import argparse
import os
import sys

from rich.console import Console

from .config import load_config
from .dpi import set_dpi_aware
from .loop import Agent

_console = Console()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="lucid",
        description="Windows desktop agent driven by Claude vision (no MCP).",
    )
    parser.add_argument("instruction", nargs="*", help="自然语言任务描述")
    parser.add_argument("-c", "--config", default=None, help="config.toml 路径")
    parser.add_argument("--model", default=None, help="覆盖代理模型 ID（如 claude-opus-4.6）")
    parser.add_argument("--base-url", default=None, help="覆盖代理 base_url")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="只通过代理打一次 /chat/completions 验证连通性，不截图、不操作鼠标键盘。",
    )
    parser.add_argument(
        "--sidecar",
        action="store_true",
        help="以 stdio JSON-RPC sidecar 模式运行（供 Tauri/前端拉起，详见 sidecar.py）。",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.model:
        cfg.llm.proxy.model = args.model
    if args.base_url:
        cfg.llm.proxy.base_url = args.base_url

    # ------ sidecar 模式：stdio JSON-RPC ------
    if args.sidecar:
        from .sidecar import run_sidecar
        return run_sidecar(cfg)

    # ------ smoke test：纯文本一问一答 ------
    if args.smoke_test:
        from .proxy_client import chat_once

        prompt = " ".join(args.instruction).strip() or "Hello, who are you? Reply in one short sentence."
        proxy = cfg.llm.proxy
        _console.rule("[bold green]lucid · smoke test")
        _console.print(f"[dim]POST {proxy.base_url}/chat/completions  model={proxy.model}[/dim]")
        _console.print(f"[bold]Prompt[/bold]: {prompt}")
        try:
            reply = chat_once(proxy, prompt)
        except RuntimeError as e:
            _console.print(f"[red]{e}[/red]")
            return 1
        _console.rule("[bold green]Reply")
        try:
            _console.print(reply)
        except UnicodeEncodeError:
            # Legacy Windows console (cp936/GBK) can't render emoji etc.
            sys.stdout.buffer.write(reply.encode("utf-8", errors="replace"))
            sys.stdout.buffer.write(b"\n")
        return 0

    # ------ ReAct 主循环（也走代理） ------
    if not args.instruction:
        parser.error("instruction is required (or pass --smoke-test)")

    if sys.platform != "win32":
        _console.print("[red]lucid ReAct 主循环仅支持 Windows（鼠标键盘驱动）。[/red]")
        return 2

    if not (
        cfg.llm.proxy.api_key
        or os.environ.get("LITELLM_MASTER_KEY")
        or os.environ.get("OPENAI_API_KEY")
    ):
        _console.print(
            "[red]缺少代理 API Key：请设置 LITELLM_MASTER_KEY 环境变量，"
            "或在 config.toml [llm.proxy].api_key 填入。[/red]"
        )
        return 2

    set_dpi_aware()

    instruction = " ".join(args.instruction).strip()
    _console.rule("[bold green]lucid")
    _console.print(f"[bold]任务[/bold]: {instruction}")
    _console.print(
        f"[dim]代理: {cfg.llm.proxy.base_url}  ·  模型: {cfg.llm.proxy.model}[/dim]"
    )

    try:
        agent = Agent(cfg)
        result = agent.run(instruction)
    except KeyboardInterrupt:
        _console.print("\n[yellow]用户中断。[/yellow]")
        return 130
    except RuntimeError as e:
        _console.print(f"[red]{e}[/red]")
        return 1

    _console.rule("[bold green]结果")
    _console.print(result or "(无文本输出)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
