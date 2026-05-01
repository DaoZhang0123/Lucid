<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { _ } from "svelte-i18n";
  import { startTask, ensureChatListeners } from "$lib/chatStore.svelte";

  type Tpl = { id: string; name: string; instruction: string; autonomy: string; max_steps: number };

  let items = $state<Tpl[]>([]);
  let err = $state("");
  let editing = $state<Tpl | null>(null);

  let name = $state("");
  let instruction = $state("");
  let autonomy = $state<"full" | "confirm_critical" | "confirm_each">("confirm_critical");
  let maxSteps = $state(25);

  async function load() {
    err = "";
    try {
      const r = await invoke<any>("template_list");
      items = (r?.templates ?? []) as Tpl[];
    } catch (e) {
      err = String(e);
    }
  }

  function reset() {
    editing = null;
    name = "";
    instruction = "";
    autonomy = "confirm_critical";
    maxSteps = 25;
  }

  function startEdit(t: Tpl) {
    editing = t;
    name = t.name;
    instruction = t.instruction;
    autonomy = (t.autonomy as any) ?? "confirm_critical";
    maxSteps = t.max_steps ?? 25;
  }

  async function save() {
    err = "";
    try {
      if (!instruction.trim()) { err = $_("templates.instruction_required"); return; }
      if (editing) {
        await invoke("template_update", {
          id: editing.id, name, instruction, autonomy, maxSteps,
        });
      } else {
        await invoke("template_add", {
          name, instruction, autonomy, maxSteps,
        });
      }
      reset();
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  async function del(id: string) {
    if (!confirm($_("templates.delete_confirm"))) return;
    try {
      await invoke("template_delete", { id });
      if (editing?.id === id) reset();
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  async function runNow(t: Tpl) {
    await ensureChatListeners();
    await startTask(t.instruction, t.autonomy, t.max_steps);
    await goto("/");
  }

  onMount(load);
</script>

<svelte:head>
  <title>{$_("templates.page_title")}</title>
</svelte:head>

<div class="page">
  <header>
    <a class="back" href="/">{$_("common.back")}</a>
    <h1>{$_("templates.heading")}</h1>
  </header>
  <p class="hint">{$_("templates.hint")}</p>

  {#if err}<p class="err">{err}</p>{/if}

  <section class="editor">
    <h2>{editing ? $_("templates.edit_heading") : $_("templates.new_heading")}</h2>
    <label>{$_("templates.name_label")} <input bind:value={name} placeholder={$_("templates.name_placeholder")} /></label>
    <label>{$_("templates.instruction_label")}
      <textarea rows="4" bind:value={instruction} placeholder={$_("templates.instruction_placeholder")}></textarea>
    </label>
    <label>{$_("templates.autonomy_label")}
      <select bind:value={autonomy}>
        <option value="full">full</option>
        <option value="confirm_critical">confirm_critical</option>
        <option value="confirm_each">confirm_each</option>
      </select>
    </label>
    <label>{$_("templates.max_steps_label")} <input type="number" min="1" max="200" bind:value={maxSteps} /></label>
    <div class="actions">
      <button onclick={save}>{editing ? $_("templates.save_edit_button") : $_("templates.save_new_button")}</button>
      {#if editing}<button class="ghost" onclick={reset}>{$_("templates.cancel_button")}</button>{/if}
    </div>
  </section>

  <section class="list">
    <h2>{$_("templates.list_heading", { values: { n: items.length } })}</h2>
    {#each items as t (t.id)}
      <div class="row" class:active={editing?.id === t.id}>
        <div class="info">
          <div class="name">{t.name}</div>
          <div class="instr">{t.instruction}</div>
          <div class="meta">{t.autonomy} · {$_("templates.step_count", { values: { n: t.max_steps } })}</div>
        </div>
        <div class="ops">
          <button onclick={() => runNow(t)}>{$_("templates.run_button")}</button>
          <button class="ghost" onclick={() => startEdit(t)}>{$_("templates.edit_button")}</button>
          <button class="danger" onclick={() => del(t.id)}>{$_("templates.delete_button")}</button>
        </div>
      </div>
    {/each}
    {#if !items.length}<p class="empty">{$_("templates.empty")}</p>{/if}
  </section>
</div>

<style>
  .page { max-width: 56rem; margin: 0 auto; padding: 1rem 1.5rem; font: 14px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  header { display: flex; align-items: baseline; gap: 1rem; }
  .back { color: #2563eb; text-decoration: none; font-size: 0.9rem; }
  h1 { margin: 0.5rem 0; }
  h2 { margin: 1rem 0 0.4rem; font-size: 1.05rem; }
  .hint { font-size: 0.85rem; color: #4b5563; }
  .err { color: #b91c1c; }
  .editor label { display: block; margin: 0.4rem 0; }
  .editor label > input, .editor label > select { margin-left: 0.4rem; padding: 0.25rem 0.4rem;
                                                   border: 1px solid #d1d5db; border-radius: 4px; }
  .editor label > textarea { display: block; width: 100%; box-sizing: border-box; margin-top: 0.2rem;
                              padding: 0.4rem; border: 1px solid #d1d5db; border-radius: 4px;
                              font: inherit; }
  .actions button { padding: 0.4rem 1rem; background: #2563eb; color: #fff; border: 0;
                    border-radius: 4px; cursor: pointer; margin-right: 0.4rem; }
  .actions button.ghost { background: #fff; color: #2563eb; border: 1px solid #93c5fd; }
  .row { display: flex; align-items: center; gap: 0.6rem; padding: 0.6rem;
         border: 1px solid #e5e7eb; border-radius: 6px; margin-bottom: 0.4rem; background: #fff; }
  .row.active { border-color: #2563eb; }
  .info { flex: 1; min-width: 0; }
  .name { font-weight: 600; }
  .instr { font-size: 0.85rem; color: #374151; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .meta { font-size: 0.72rem; color: #6b7280; }
  .ops button { padding: 0.3rem 0.6rem; background: #2563eb; color: #fff; border: 0;
                border-radius: 4px; cursor: pointer; margin-left: 0.3rem; font-size: 0.85rem; }
  .ops button.ghost { background: #fff; color: #2563eb; border: 1px solid #93c5fd; }
  .ops button.danger { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
  .empty { color: #9ca3af; font-size: 0.85rem; }
</style>
