# ctrlapp · App (Tauri + SvelteKit)

Desktop shell for the ctrlapp Windows agent. See repo root README + design.md
for the big picture; this file is a developer cheatsheet.

## Dev mode

```powershell
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
$env:CTRLAPP_PYTHON = "D:\Project\ctrlAppWithoutMCP\.venv\Scripts\python.exe"
$env:CTRLAPP_CWD    = "D:\Project\ctrlAppWithoutMCP"
$env:LITELLM_MASTER_KEY = (
    Get-Content D:\Project\litellm-ghc-proxy-lite\.env |
    Select-String '^LITELLM_MASTER_KEY=' |
    ForEach-Object { ($_ -split '=', 2)[1] }
)
cd D:\Project\ctrlAppWithoutMCP\app
pnpm install
pnpm tauri dev
```

First Rust build pulls Tauri/wry/webview2-com (~5–10 min); incremental is seconds.

## Self-checks (Phase 1.5)

In the Settings page click "多屏 + DPI" / "Win+R 别名" / "点击坐标偏差", or:

```powershell
python -m ctrlapp.selfcheck monitors
python -m ctrlapp.selfcheck winr
python -m ctrlapp.selfcheck click
```

## 5 example scenarios (Phase 1.6)

```powershell
python -m ctrlapp.examples list
python -m ctrlapp.examples run notepad
python -m ctrlapp.examples run wechat --autonomy confirm_each
python -m ctrlapp.examples run all
```

## Packaging (Phase 1.7)

```powershell
# (a) Bundle the Python sidecar
cd D:\Project\ctrlAppWithoutMCP
pip install pyinstaller
# Important: clean the previous bloated output first.
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
pyinstaller --noconfirm packaging/ctrlapp.spec
# Output: dist/ctrlapp/ctrlapp.exe (should be ~hundreds of files, NOT thousands)

# (b) Wrap shell + sidecar into NSIS installer
cd app
pnpm tauri build
# Output: app/src-tauri/target/release/bundle/nsis/ctrlapp_*.exe
```

> NSIS is the only enabled target. WiX/MSI was disabled because `light.exe`
> chokes on multi-thousand-file resource trees produced by PyInstaller.

Sidecar resolution priority (`src-tauri/src/sidecar.rs::build_command`):

1. `CTRLAPP_SIDECAR_EXE` env override
2. Bundled `<resource_dir>/ctrlapp/ctrlapp.exe`
3. `CTRLAPP_PYTHON -m ctrlapp --sidecar`
4. `python -m ctrlapp --sidecar`
0
---

## Recommended IDE Setup

[VS Code](https://code.visualstudio.com/) + [Svelte](https://marketplace.visualstudio.com/items?itemName=svelte.svelte-vscode) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer).
