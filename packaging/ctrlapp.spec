# PyInstaller spec for ctrlapp sidecar (Phase 1.7).
#
# Build:
#     pip install pyinstaller
#     pyinstaller --noconfirm packaging/ctrlapp.spec
#
# Output:  dist/ctrlapp/ctrlapp.exe  (folder mode, lighter on AV scans than --onefile)
# 这个目录会被 Tauri 通过 tauri.conf.json `bundle.resources` 整体打进 .msi，
# 启动时 Rust 侧 (sidecar.rs::build_command) 会优先找
# `<resource_dir>/ctrlapp/ctrlapp.exe` 起子进程。
#
# Notes:
# * 不用 --onefile，启动更快、避免被 AV 解压一次又一次扫；
# * console=True 才能拿到 stdin/stdout（Tauri 侧也设了 CREATE_NO_WINDOW 隐藏黑窗）；
# * mss / pyautogui / pyperclip / openai 全部走自动收集；
# * datas 里把仓库根 config.toml 复制到 resources/ 当默认配置。

# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

hidden = []
hidden += collect_submodules("mss")
hidden += collect_submodules("pyautogui")
hidden += collect_submodules("pyperclip")
hidden += collect_submodules("rich")
hidden += collect_submodules("PIL")
hidden += collect_submodules("tzdata")  # zoneinfo on Windows needs the tzdata pkg
# NOTE: do NOT collect_submodules("openai") — it transitively drags in
# torch / transformers / tiktoken (>5GB), blowing past WiX / NSIS limits.
# We only need the OpenAI HTTP client; the chat.completions surface is enough.
hidden += [
    "openai",
    "openai._client",
    "openai._streaming",
    "openai._compat",
    "openai._exceptions",
    "openai.types",
    "openai.types.chat",
    "openai.resources",
    "openai.resources.chat",
    "openai.resources.chat.completions",
    "anthropic",
    "anthropic._client",
    "anthropic._streaming",
    "anthropic._exceptions",
    "anthropic.types",
    "anthropic.types.message",
    "anthropic.types.text_block",
    "anthropic.types.tool_use_block",
    "anthropic.resources",
    "anthropic.resources.messages",
    "httpx",
    "httpcore",
    "anyio",
    "sniffio",
    "distro",
    "jiter",
    "pydantic",
    "pydantic_core",
]

datas = [
    (os.path.join(ROOT, "config.toml"), "."),
]
datas += collect_data_files("tzdata")  # IANA 时区原始文件

a = Analysis(
    [os.path.join(ROOT, "packaging", "ctrlapp_entry.py")],
    pathex=[os.path.join(ROOT, "python")],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "test",
        "unittest",
        # Heavy ML deps that some openai-adjacent libs reference but we don't use:
        "torch",
        "torchvision",
        "torchaudio",
        "transformers",
        "tensorflow",
        "jax",
        "jaxlib",
        "datasets",
        "tokenizers",
        "tiktoken",
        "sentencepiece",
        "safetensors",
        "accelerate",
        "scipy",
        "sklearn",
        "sympy",
        "networkx",
        "matplotlib",
        "pandas",
        "notebook",
        "IPython",
        "ipykernel",
        "jupyter",
        "jedi",
        "black",
        "isort",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ctrlapp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,           # MUST be True so stdin/stdout pipes work
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
# NOTE: onefile mode — one self-extracting `dist/ctrlapp.exe`. Tauri's
# `bundle.resources` glob preserves no directory structure, so onedir mode
# would break PyInstaller's `_internal/` and Python packages (numpy/PIL/...).
# Onefile pays a one-time extraction cost on first launch but is bulletproof
# for installer packaging.
