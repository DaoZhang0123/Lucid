# Lucid · App (Tauri + SvelteKit)

Desktop shell for the Lucid Windows agent (Python sidecar still ships as `lucid.exe` for backward compat). See repo root README + design.md
for the big picture; this file is a developer cheatsheet.

## Dev mode

```powershell
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
$env:LUCID_PYTHON = "D:\Project\Lucid\.venv\Scripts\python.exe"
$env:LUCID_CWD    = "D:\Project\Lucid"
$env:LITELLM_MASTER_KEY = (
    Get-Content D:\Project\litellm-ghc-proxy-lite\.env |
    Select-String '^LITELLM_MASTER_KEY=' |
    ForEach-Object { ($_ -split '=', 2)[1] }
)
cd D:\Project\Lucid\app
pnpm install
pnpm tauri dev
```

First Rust build pulls Tauri/wry/webview2-com (~5–10 min); incremental is seconds.

## Self-checks (Phase 1.5)

In the Settings page click "多屏 + DPI" / "Win+R 别名" / "点击坐标偏差", or:

```powershell
python -m lucid.selfcheck monitors
python -m lucid.selfcheck winr
python -m lucid.selfcheck click
```

## 5 example scenarios (Phase 1.6)

```powershell
python -m lucid.examples list
python -m lucid.examples run notepad
python -m lucid.examples run wechat --autonomy confirm_each
python -m lucid.examples run all
```

## Packaging (Phase 1.7)

```powershell
# (a) Bundle the Python sidecar
cd D:\Project\Lucid
pip install pyinstaller
# Important: clean the previous bloated output first.
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
pyinstaller --noconfirm packaging/lucid.spec
# Output: dist/lucid/lucid.exe (should be ~hundreds of files, NOT thousands)

# (b) Wrap shell + sidecar into NSIS installer
cd app
pnpm tauri build
# Output: app/src-tauri/target/release/bundle/nsis/lucid_*.exe
```

> NSIS is the only enabled target. WiX/MSI was disabled because `light.exe`
> chokes on multi-thousand-file resource trees produced by PyInstaller.

Sidecar resolution priority (`src-tauri/src/sidecar.rs::build_command`):

1. `LUCID_SIDECAR_EXE` env override
2. Bundled `<resource_dir>/lucid/lucid.exe`
3. `LUCID_PYTHON -m lucid --sidecar`
4. `python -m lucid --sidecar`
0
---

## Recommended IDE Setup

[VS Code](https://code.visualstudio.com/) + [Svelte](https://marketplace.visualstudio.com/items?itemName=svelte.svelte-vscode) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer).
