<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { _ } from "svelte-i18n";
  import { appConfirm } from "$lib/appConfirm.svelte";

  type Skill = {
    id: string;
    slug: string;
    name: string;
    description: string;
    body: string;
    source: "user" | "online" | "repo";
    source_url?: string | null;
    source_repo?: string | null;
    enabled?: boolean;
    version?: string | null;
    license?: string | null;
  };

  type Repo = {
    id: string;
    name: string;
    url: string;
    description?: string;
    enabled: boolean;
    builtin: boolean;
  };

  let items = $state<Skill[]>([]);
  let repos = $state<Repo[]>([]);
  let err = $state("");
  let editing = $state<Skill | null>(null);

  let name = $state("");
  let description = $state("");
  let body = $state("");

  let installUrl = $state("");
  let installErr = $state("");
  let installing = $state(false);

  let newRepoUrl = $state("");
  let newRepoName = $state("");
  let repoErr = $state("");
  let refreshing = $state(false);

  const BODY_PLACEHOLDER = `# Weekly report

When the user asks you to draft a weekly report:

1. Launch Outlook with \`launch_app: outlook\`.
2. Compose a new mail to the recipient the user mentions.
3. Use subject \`[Weekly] <today>\`.
4. Paste the user-supplied bullet points into the body.
5. Show the draft to the user (do NOT send) and ask for confirmation.`;

  async function load() {
    err = "";
    try {
      const r = await invoke<any>("skill_list");
      items = (r?.skills ?? []) as Skill[];
    } catch (e) {
      err = String(e);
    }
    try {
      const r2 = await invoke<any>("skill_repo_list");
      repos = (r2?.repos ?? []) as Repo[];
    } catch (e) {
      repoErr = String(e);
    }
  }

  async function toggleSkill(s: Skill, enabled: boolean) {
    try {
      await invoke("skill_set_enabled", { id: s.id, enabled });
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  async function toggleRepo(r: Repo, enabled: boolean) {
    repoErr = "";
    try {
      await invoke("skill_repo_set_enabled", { id: r.id, enabled });
      await load();
    } catch (e) {
      repoErr = String(e);
    }
  }

  async function addRepo() {
    repoErr = "";
    if (!newRepoUrl.trim()) return;
    try {
      await invoke("skill_repo_add", {
        url: newRepoUrl.trim(),
        name: newRepoName.trim() || null,
        description: null,
      });
      newRepoUrl = "";
      newRepoName = "";
      await load();
    } catch (e) {
      repoErr = String(e);
    }
  }

  async function deleteRepo(r: Repo) {
    if (!(await appConfirm($_("skills.repo_delete_confirm"), { danger: true }))) return;
    try {
      await invoke("skill_repo_delete", { id: r.id });
      await load();
    } catch (e) {
      repoErr = String(e);
    }
  }

  async function refreshRepos() {
    refreshing = true;
    repoErr = "";
    try {
      await invoke("skill_repo_refresh", { force: true });
    } catch (e) {
      repoErr = String(e);
    } finally {
      refreshing = false;
    }
  }

  function reset() {
    editing = null;
    name = "";
    description = "";
    body = "";
  }

  function startEdit(s: Skill) {
    editing = s;
    name = s.name;
    description = s.description;
    body = s.body;
  }

  async function save() {
    err = "";
    try {
      if (!name.trim()) { err = $_("skills.name_required"); return; }
      if (!description.trim()) { err = $_("skills.description_required"); return; }
      if (!body.trim()) { err = $_("skills.body_required"); return; }
      if (editing) {
        await invoke("skill_update", {
          id: editing.id, name, description, body,
        });
      } else {
        await invoke("skill_add", { name, description, body });
      }
      reset();
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  async function del(id: string) {
    if (!(await appConfirm($_("skills.delete_confirm"), { danger: true }))) return;
    try {
      await invoke("skill_delete", { id });
      if (editing?.id === id) reset();
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  async function runNow(s: Skill) {
    // Anthropic-style skills aren't "executed" — they're loaded into the
    // agent's context and the agent follows the body using its normal tools.
    // The "Use" button just navigates to chat with the request pre-filled in
    // the input box so the user can review / edit before sending.
    const tag = s.source === "online" ? " (online, untrusted)" : "";
    const prompt =
      `Use the skill \`${s.name}\`${tag}. Call \`read_skill(name="${s.name}")\` ` +
      `to load the full SKILL.md body, then follow it.`;
    try {
      sessionStorage.setItem("lucid:prefill", prompt);
    } catch {
      // ignore — sessionStorage unavailable in some sandbox modes
    }
    await goto("/");
  }

  async function doInstall() {
    installErr = "";
    if (!installUrl.trim()) return;
    installing = true;
    try {
      await invoke("skill_install_url", { url: installUrl.trim() });
      installUrl = "";
      await load();
    } catch (e) {
      installErr = String(e);
    } finally {
      installing = false;
    }
  }

  onMount(load);
</script>

<svelte:head>
  <title>{$_("skills.page_title")}</title>
</svelte:head>

<div class="page">
  <header>
    <a class="back" href="/">{$_("common.back")}</a>
    <h1>{$_("skills.heading")}</h1>
  </header>
  <p class="hint">{$_("skills.hint")}</p>

  {#if err}<p class="err">{err}</p>{/if}

  <section class="editor">
    <h2>{editing ? $_("skills.edit_heading") : $_("skills.new_heading")}</h2>
    <label>{$_("skills.name_label")}
      <input bind:value={name} placeholder={$_("skills.name_placeholder")} />
    </label>
    <label>{$_("skills.description_label")}
      <textarea rows="2" bind:value={description} placeholder={$_("skills.description_placeholder")}></textarea>
    </label>
    <label>{$_("skills.body_label")}
      <textarea class="body" rows="14" bind:value={body} placeholder={BODY_PLACEHOLDER}></textarea>
    </label>
    <p class="hint small">{$_("skills.body_hint")}</p>
    <div class="actions">
      <button onclick={save}>{editing ? $_("skills.save_edit_button") : $_("skills.save_new_button")}</button>
      {#if editing}<button class="ghost" onclick={reset}>{$_("skills.cancel_button")}</button>{/if}
    </div>
  </section>

  <section class="install">
    <h2>{$_("skills.repos_heading")}</h2>
    <p class="hint small">{$_("skills.repos_hint")}</p>
    {#if repoErr}<p class="err">{repoErr}</p>{/if}
    <div class="repo-list">
      {#each repos as r (r.id)}
        <div class="repo-row">
          <label class="repo-check">
            <input type="checkbox" checked={r.enabled} onchange={(e) => toggleRepo(r, (e.target as HTMLInputElement).checked)} />
            <div class="repo-info">
              <div class="repo-name">
                {r.name}
                {#if r.builtin}<span class="badge">{$_("skills.repo_badge_builtin")}</span>{:else}<span class="badge user">{$_("skills.repo_badge_user")}</span>{/if}
              </div>
              {#if r.description}<div class="instr">{r.description}</div>{/if}
              <div class="meta"><a href={r.url} target="_blank" rel="noreferrer">{r.url}</a></div>
            </div>
          </label>
          {#if !r.builtin}
            <button class="ghost small" onclick={() => deleteRepo(r)}>
              {$_("skills.repo_delete_button")}
            </button>
          {/if}
        </div>
      {/each}
    </div>
    <div class="add-repo">
      <input bind:value={newRepoUrl} placeholder={$_("skills.repo_add_url_placeholder")} />
      <input bind:value={newRepoName} placeholder={$_("skills.repo_add_name_placeholder")} />
      <button onclick={addRepo} disabled={!newRepoUrl.trim()}>{$_("skills.repo_add_button")}</button>
      <button class="ghost" onclick={refreshRepos} disabled={refreshing}>
        {refreshing ? $_("skills.repo_refreshing") : $_("skills.repo_refresh_button")}
      </button>
    </div>
  </section>

  <section class="install">
    <h2>{$_("skills.install_url_heading")}</h2>
    <p class="hint small">{$_("skills.install_url_hint")}</p>
    <div class="install-row">
      <input bind:value={installUrl} placeholder={$_("skills.install_url_placeholder")} disabled={installing} />
      <button onclick={doInstall} disabled={installing || !installUrl.trim()}>{$_("skills.install_url_button")}</button>
    </div>
    {#if installErr}<p class="err">{installErr}</p>{/if}
  </section>

  <section class="list">
    <h2>{$_("skills.list_heading", { values: { n: items.length } })}</h2>
    {#each items as s (s.id)}
      <div class="row" class:active={editing?.id === s.id}>
        <div class="info">
          <div class="name">
            <input type="checkbox" class="enable-check" checked={s.enabled ?? true} onchange={(e) => toggleSkill(s, (e.target as HTMLInputElement).checked)} title={$_("skills.enable_checkbox_title")} />
            {s.name}
            <span class="badge" class:online={s.source === "online"} class:repo={s.source === "repo"}>
              {s.source === "online" ? $_("skills.source_online") : s.source === "repo" ? $_("skills.source_repo") : $_("skills.source_user")}
            </span>
            {#if s.version}<span class="ver">v{s.version}</span>{/if}
            {#if s.enabled === false}<span class="ver">{$_("skills.disabled_label")}</span>{/if}
          </div>
          <div class="instr">{s.description}</div>
          <div class="meta">
            slug: {s.slug} · {s.body?.length ?? 0} chars
            {#if s.source_url} · <a href={s.source_url} target="_blank" rel="noreferrer">{s.source_url}</a>{/if}
          </div>
        </div>
        <div class="ops">
          <button onclick={() => runNow(s)}>{$_("skills.run_button")}</button>
          <button class="ghost" onclick={() => startEdit(s)}>{$_("skills.edit_button")}</button>
          <button class="danger" onclick={() => del(s.id)}>{$_("skills.delete_button")}</button>
        </div>
      </div>
    {/each}
    {#if !items.length}<p class="empty">{$_("skills.empty")}</p>{/if}
  </section>
</div>

<style>
  .page { max-width: 60rem; margin: 0; padding: 1rem 1.5rem; font: 14px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  header { display: flex; align-items: baseline; gap: 1rem; }
  .back { color: #2563eb; text-decoration: none; font-size: 0.9rem; }
  h1 { margin: 0.5rem 0; }
  h2 { margin: 1rem 0 0.4rem; font-size: 1.05rem; }
  .hint { font-size: 0.85rem; color: #4b5563; }
  .hint.small { font-size: 0.78rem; margin: 0.2rem 0 0.4rem; }
  .err { color: #b91c1c; }
  .editor label, .install label { display: block; margin: 0.4rem 0; }
  .editor label > input, .install input {
    margin-left: 0.4rem; padding: 0.25rem 0.4rem;
    border: 1px solid #d1d5db; border-radius: 4px;
  }
  .editor label > textarea {
    display: block; width: 100%; box-sizing: border-box; margin-top: 0.2rem;
    padding: 0.4rem; border: 1px solid #d1d5db; border-radius: 4px; font: inherit;
  }
  .editor label > textarea.body {
    font: 13px ui-monospace, Consolas, "Microsoft YaHei Mono", monospace;
    line-height: 1.45;
  }
  .actions button { padding: 0.4rem 1rem; background: #2563eb; color: #fff; border: 0;
                    border-radius: 4px; cursor: pointer; margin-right: 0.4rem; }
  .actions button.ghost { background: #fff; color: #2563eb; border: 1px solid #93c5fd; }
  .install-row { display: flex; gap: 0.4rem; align-items: center; }
  .install-row input { flex: 1; }
  .install-row button { padding: 0.35rem 0.9rem; background: #2563eb; color: #fff; border: 0;
                        border-radius: 4px; cursor: pointer; }
  .row { display: flex; align-items: center; gap: 0.6rem; padding: 0.6rem;
         border: 1px solid #e5e7eb; border-radius: 6px; margin-bottom: 0.4rem; background: #fff; }
  .row.active { border-color: #2563eb; }
  .info { flex: 1; min-width: 0; }
  .name { font-weight: 600; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .badge { font-size: 0.65rem; padding: 0.05rem 0.4rem; border-radius: 999px;
           background: #e0f2fe; color: #075985; border: 1px solid #bae6fd; font-weight: 500; }
  .badge.online { background: #fef3c7; color: #92400e; border-color: #fde68a; }
  .badge.repo { background: #dcfce7; color: #166534; border-color: #bbf7d0; }
  .badge.user { background: #e5e7eb; color: #374151; border-color: #d1d5db; }
  .enable-check { margin: 0 0.25rem 0 0; }
  .repo-list { display: flex; flex-direction: column; gap: 0.4rem; margin-bottom: 0.6rem; }
  .repo-row { display: flex; gap: 0.6rem; align-items: flex-start; padding: 0.5rem;
              border: 1px solid #e5e7eb; border-radius: 6px; background: #fff; }
  .repo-check { flex: 1; display: flex; gap: 0.6rem; align-items: flex-start; cursor: pointer; }
  .repo-info { flex: 1; min-width: 0; }
  .repo-name { font-weight: 600; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .add-repo { display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap; }
  .add-repo input { flex: 1 1 18rem; padding: 0.3rem 0.5rem; border: 1px solid #d1d5db; border-radius: 4px; }
  .add-repo button { padding: 0.35rem 0.9rem; background: #2563eb; color: #fff; border: 0;
                     border-radius: 4px; cursor: pointer; }
  .add-repo button.ghost { background: #fff; color: #2563eb; border: 1px solid #93c5fd; }
  .ghost.small { padding: 0.2rem 0.6rem; font-size: 0.78rem; background: #fff; color: #2563eb;
                 border: 1px solid #93c5fd; border-radius: 4px; cursor: pointer; align-self: center; }
  :global([data-theme="dark"]) .repo-row { background: #1f2937; color: #e5e7eb; }
  .ver { font-size: 0.7rem; color: #6b7280; font-weight: 500; }
  .instr { font-size: 0.85rem; color: #374151; }
  .meta { font-size: 0.72rem; color: #6b7280; margin-top: 0.15rem; word-break: break-all; }
  .ops button { padding: 0.3rem 0.6rem; background: #2563eb; color: #fff; border: 0;
                border-radius: 4px; cursor: pointer; margin-left: 0.3rem; font-size: 0.85rem; }
  .ops button.ghost { background: #fff; color: #2563eb; border: 1px solid #93c5fd; }
  .ops button.danger { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
  .empty { color: #9ca3af; font-size: 0.85rem; }
  :global([data-theme="dark"]) .row { background: #1f2937; color: #e5e7eb; }
</style>
