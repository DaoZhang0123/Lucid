# OtterScope · App (Tauri + SvelteKit)

Desktop shell for the OtterScope Windows agent (Python sidecar still ships as `otterscope.exe` for backward compat). See repo root README + design.md
for the big picture; this file is a developer cheatsheet.

## Dev mode

```powershell
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
$env:OTTERSCOPE_PYTHON = "D:\Project\OtterScope\.venv\Scripts\python.exe"
$env:OTTERSCOPE_CWD    = "D:\Project\OtterScope"
$env:LITELLM_MASTER_KEY = (
    Get-Content D:\Project\litellm-ghc-proxy-lite\.env |
    Select-String '^LITELLM_MASTER_KEY=' |
    ForEach-Object { ($_ -split '=', 2)[1] }
)
cd D:\Project\OtterScope\app
pnpm install
pnpm tauri dev
```

First Rust build pulls Tauri/wry/webview2-com (~5–10 min); incremental is seconds.

## Self-checks (Phase 1.5)

In the Settings page click "多屏 + DPI" / "Win+R 别名" / "点击坐标偏差", or:

```powershell
python -m otterscope.selfcheck monitors
python -m otterscope.selfcheck winr
python -m otterscope.selfcheck click
```

## 5 example scenarios (Phase 1.6)

```powershell
python -m otterscope.examples list
python -m otterscope.examples run notepad
python -m otterscope.examples run wechat --autonomy confirm_each
python -m otterscope.examples run all
```

## Packaging (Phase 1.7)

```powershell
# (a) Bundle the Python sidecar
cd D:\Project\OtterScope
pip install pyinstaller
# Important: clean the previous bloated output first.
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
pyinstaller --noconfirm packaging/otterscope.spec
# Output: dist/otterscope/otterscope.exe (should be ~hundreds of files, NOT thousands)

# (b) Wrap shell + sidecar into NSIS installer
cd app
pnpm tauri build
# Output: app/src-tauri/target/release/bundle/nsis/otterscope_*.exe
```

> NSIS is the only enabled target. WiX/MSI was disabled because `light.exe`
> chokes on multi-thousand-file resource trees produced by PyInstaller.

Sidecar resolution priority (`src-tauri/src/sidecar.rs::build_command`):

1. `OTTERSCOPE_SIDECAR_EXE` env override
2. Bundled `<resource_dir>/otterscope/otterscope.exe`
3. `OTTERSCOPE_PYTHON -m otterscope --sidecar`
4. `python -m otterscope --sidecar`
0
---

## Recommended IDE Setup

[VS Code](https://code.visualstudio.com/) + [Svelte](https://marketplace.visualstudio.com/items?itemName=svelte.svelte-vscode) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer).
