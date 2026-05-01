<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";

  let enabled = $state(true);
  let path = $state("");
  let text = $state("");
  let saving = $state(false);
  let savedAt = $state("");
  let err = $state("");

  // 手动追加一条
  let newTip = $state("");
  let newKind = $state<"success" | "failure" | "tip">("tip");

  async function load() {
    err = "";
    try {
      const r = await invoke<any>("tools_read");
      enabled = !!r.enabled;
      path = r.path ?? "";
      text = r.text ?? "";
    } catch (e) {
      err = String(e);
    }
  }

  async function save() {
    saving = true;
    err = "";
    try {
      await invoke("tools_write", { text });
      savedAt = new Date().toLocaleTimeString();
    } catch (e) {
      err = String(e);
    } finally {
      saving = false;
    }
  }

  async function appendTip() {
    const t = newTip.trim();
    if (!t) return;
    err = "";
    try {
      await invoke("tools_append", { text: t, kind: newKind, source: "user" });
      newTip = "";
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  async function reset() {
    if (!confirm("把 tools.md 重置为初始 seed？所有学到的技巧会被清掉。")) return;
    try {
      await invoke("tools_reset");
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  onMount(load);
</script>

<div class="page">
  <header>
    <a class="back" href="/">‹ 返回</a>
    <h1>操作技巧 · tools.md</h1>
  </header>

  <p class="hint">
    每次任务起手会把这里的内容追加到 system prompt 末尾，作为"操作技巧库"。Agent 在任务中可以用
    <code>learn_tip</code> 函数主动追加成功/失败经验。这里只放**操作技法**（怎么操作 App / 对话框
    最稳妥），不要放用户偏好（那是 memory.md）或单次任务的临时事实。
  </p>
  <p class="meta">
    状态：{enabled ? "已启用" : "已禁用（在 config.toml [tools] 里开启）"}<br/>
    路径：<code>{path}</code>
  </p>

  {#if err}<p class="err">{err}</p>{/if}

  <div class="quick">
    <input
      type="text"
      placeholder="手动追加一条技巧（例：Outlook 用 Ctrl+R 回复比点回复按钮稳）"
      bind:value={newTip}
      onkeydown={(e) => { if (e.key === "Enter") appendTip(); }}
      disabled={!enabled}
    />
    <select bind:value={newKind} disabled={!enabled}>
      <option value="tip">tip</option>
      <option value="success">success</option>
      <option value="failure">failure</option>
    </select>
    <button onclick={appendTip} disabled={!enabled || !newTip.trim()}>追加</button>
  </div>

  <textarea bind:value={text} rows="22" disabled={!enabled}></textarea>

  <div class="actions">
    <button onclick={save} disabled={saving || !enabled}>{saving ? "保存中…" : "保存全文"}</button>
    <button class="danger" onclick={reset} disabled={!enabled}>重置为 seed</button>
    <button onclick={load}>重新加载</button>
    {#if savedAt}<span class="ok">已保存 {savedAt}</span>{/if}
  </div>
</div>

<style>
  .page { max-width: 56rem; margin: 0 auto; padding: 1rem 1.5rem; font: 14px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 0.5rem; }
  .back { color: #2563eb; text-decoration: none; font-size: 0.9rem; }
  h1 { margin: 0.5rem 0; }
  .hint { font-size: 0.85rem; color: #4b5563; }
  .meta { font-size: 0.78rem; color: #6b7280; }
  .err { color: #b91c1c; }
  .quick { display: flex; gap: 0.4rem; margin: 0.6rem 0; }
  .quick input { flex: 1; padding: 0.4rem 0.6rem; border: 1px solid #d1d5db; border-radius: 4px; font: inherit; }
  .quick select { padding: 0.4rem; border: 1px solid #d1d5db; border-radius: 4px; }
  .quick button { padding: 0.4rem 1rem; background: #2563eb; color: #fff; border: 0; border-radius: 4px; cursor: pointer; }
  .quick button:disabled { opacity: 0.5; cursor: not-allowed; }
  textarea { width: 100%; box-sizing: border-box; font: 13px ui-monospace, Consolas, monospace;
             padding: 0.6rem; border: 1px solid #d1d5db; border-radius: 6px; }
  textarea:disabled { background: #f3f4f6; color: #9ca3af; }
  .actions { display: flex; gap: 0.5rem; margin-top: 0.6rem; align-items: center; }
  .actions button { padding: 0.4rem 1rem; background: #2563eb; color: #fff; border: 0;
                    border-radius: 4px; cursor: pointer; }
  .actions button:disabled { opacity: 0.5; cursor: not-allowed; }
  .actions button.danger { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
  .ok { color: #047857; font-size: 0.85rem; }
  code { background: #f3f4f6; padding: 0 0.25rem; border-radius: 3px; }
</style>
