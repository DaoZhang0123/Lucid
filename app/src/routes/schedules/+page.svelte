<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";
  import { _ } from "svelte-i18n";

  type Spec =
    | { kind: "interval"; every_minutes: number; tz?: string }
    | { kind: "daily"; time: string; tz?: string }
    | { kind: "weekly"; weekday: number; time: string; tz?: string };

  type Sched = {
    id: string; name: string; instruction: string; spec: Spec;
    autonomy: string; max_steps: number; enabled: boolean;
    next_ms?: number; last_run_ms?: number;
  };

  let items = $state<Sched[]>([]);
  let err = $state("");
  let editing = $state<Sched | null>(null);

  let name = $state("");
  let instruction = $state("");
  let autonomy = $state<"full" | "confirm_critical" | "confirm_each">("confirm_critical");
  let maxSteps = $state(25);
  let enabled = $state(true);
  let kind = $state<"interval" | "daily" | "weekly">("daily");
  let everyMinutes = $state(60);
  let time = $state("09:00");
  let weekday = $state(0);
  let tz = $state(""); // 空 = 本机时间

  const WD_KEYS = [
    "schedules.weekday_mon",
    "schedules.weekday_tue",
    "schedules.weekday_wed",
    "schedules.weekday_thu",
    "schedules.weekday_fri",
    "schedules.weekday_sat",
    "schedules.weekday_sun",
  ] as const;
  // Localised weekday list, recomputed when the locale changes.
  let WD = $derived(WD_KEYS.map((k) => $_(k)));

  // Timezone list: one IANA representative per UTC offset from -12 to +14.
  // Labels (city names) are i18n keys; the IANA `value` is what gets persisted.
  const TZS: { value: string; label: string }[] = $derived([
    { value: "",                       label: $_("schedules.tz_default") },
    { value: "Etc/GMT+12",             label: $_("schedules.tz_utc_m12") },
    { value: "Pacific/Pago_Pago",      label: $_("schedules.tz_utc_m11") },
    { value: "Pacific/Honolulu",       label: $_("schedules.tz_utc_m10") },
    { value: "America/Anchorage",      label: $_("schedules.tz_utc_m9")  },
    { value: "America/Los_Angeles",    label: $_("schedules.tz_utc_m8")  },
    { value: "America/Denver",         label: $_("schedules.tz_utc_m7")  },
    { value: "America/Chicago",        label: $_("schedules.tz_utc_m6")  },
    { value: "America/New_York",       label: $_("schedules.tz_utc_m5")  },
    { value: "America/Caracas",        label: $_("schedules.tz_utc_m4")  },
    { value: "America/Sao_Paulo",      label: $_("schedules.tz_utc_m3")  },
    { value: "Atlantic/South_Georgia", label: $_("schedules.tz_utc_m2")  },
    { value: "Atlantic/Azores",        label: $_("schedules.tz_utc_m1")  },
    { value: "UTC",                    label: $_("schedules.tz_utc_0")   },
    { value: "Europe/Paris",           label: $_("schedules.tz_utc_p1")  },
    { value: "Europe/Athens",          label: $_("schedules.tz_utc_p2")  },
    { value: "Europe/Moscow",          label: $_("schedules.tz_utc_p3")  },
    { value: "Asia/Dubai",             label: $_("schedules.tz_utc_p4")  },
    { value: "Asia/Karachi",           label: $_("schedules.tz_utc_p5")  },
    { value: "Asia/Dhaka",             label: $_("schedules.tz_utc_p6")  },
    { value: "Asia/Bangkok",           label: $_("schedules.tz_utc_p7")  },
    { value: "Asia/Shanghai",          label: $_("schedules.tz_utc_p8")  },
    { value: "Asia/Tokyo",             label: $_("schedules.tz_utc_p9")  },
    { value: "Australia/Sydney",       label: $_("schedules.tz_utc_p10") },
    { value: "Pacific/Noumea",         label: $_("schedules.tz_utc_p11") },
    { value: "Pacific/Auckland",       label: $_("schedules.tz_utc_p12") },
    { value: "Pacific/Apia",           label: $_("schedules.tz_utc_p13") },
    { value: "Pacific/Kiritimati",     label: $_("schedules.tz_utc_p14") },
  ]);

  async function load() {
    err = "";
    try {
      const r = await invoke<any>("schedule_list");
      items = (r?.schedules ?? []) as Sched[];
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
    enabled = true;
    kind = "daily";
    everyMinutes = 60;
    time = "09:00";
    weekday = 0;
    tz = "";
  }

  function startEdit(s: Sched) {
    editing = s;
    name = s.name;
    instruction = s.instruction;
    autonomy = (s.autonomy as any) ?? "confirm_critical";
    maxSteps = s.max_steps ?? 25;
    enabled = !!s.enabled;
    kind = s.spec.kind as any;
    if (s.spec.kind === "interval") everyMinutes = s.spec.every_minutes;
    if (s.spec.kind === "daily") time = s.spec.time;
    if (s.spec.kind === "weekly") { time = s.spec.time; weekday = s.spec.weekday; }
    tz = s.spec.tz ?? "";
  }

  function buildSpec(): Spec {
    const t = tz.trim();
    if (kind === "interval") return { kind: "interval", every_minutes: everyMinutes, ...(t ? { tz: t } : {}) };
    if (kind === "daily") return { kind: "daily", time, ...(t ? { tz: t } : {}) };
    return { kind: "weekly", weekday, time, ...(t ? { tz: t } : {}) };
  }

  async function save() {
    err = "";
    try {
      if (!instruction.trim()) { err = $_("schedules.instruction_required"); return; }
      const spec = buildSpec();
      if (editing) {
        await invoke("schedule_update", { id: editing.id, name, instruction, spec, autonomy, maxSteps, enabled });
      } else {
        await invoke("schedule_add", { name, instruction, spec, autonomy, maxSteps, enabled });
      }
      reset();
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  async function del(id: string) {
    if (!confirm($_("schedules.delete_confirm"))) return;
    try {
      await invoke("schedule_delete", { id });
      if (editing?.id === id) reset();
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  async function toggle(s: Sched) {
    try {
      await invoke("schedule_update", { id: s.id, enabled: !s.enabled });
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  function fmtSpec(s: Spec): string {
    const tzTag = s.tz ? $_("schedules.tz_suffix", { values: { tz: s.tz } }) : "";
    if (s.kind === "interval") return $_("schedules.fmt_interval", { values: { minutes: s.every_minutes, tz: tzTag } });
    if (s.kind === "daily") return $_("schedules.fmt_daily", { values: { time: s.time, tz: tzTag } });
    return $_("schedules.fmt_weekly", { values: { weekday: WD[s.weekday] ?? "?", time: s.time, tz: tzTag } });
  }

  function fmtTime(ms?: number): string {
    if (!ms) return $_("schedules.time_unknown");
    return new Date(ms).toLocaleString();
  }

  onMount(load);
</script>

<svelte:head>
  <title>{$_("schedules.page_title")}</title>
</svelte:head>

<div class="page">
  <header>
    <a class="back" href="/">{$_("common.back")}</a>
    <h1>{$_("schedules.heading")}</h1>
  </header>
  <p class="hint">
    {@html $_("schedules.hint")}
  </p>

  {#if err}<p class="err">{err}</p>{/if}

  <section class="editor">
    <h2>{editing ? $_("schedules.edit_heading") : $_("schedules.new_heading")}</h2>
    <label>{$_("schedules.name_label")} <input bind:value={name} placeholder={$_("schedules.name_placeholder")} /></label>
    <label>{$_("schedules.instruction_label")}
      <textarea rows="3" bind:value={instruction} placeholder={$_("schedules.instruction_placeholder")}></textarea>
    </label>

    <fieldset class="trigger">
      <legend>{$_("schedules.trigger_legend")}</legend>
      <label><input type="radio" bind:group={kind} value="interval" /> {$_("schedules.trigger_interval")}</label>
      <label><input type="radio" bind:group={kind} value="daily" /> {$_("schedules.trigger_daily")}</label>
      <label><input type="radio" bind:group={kind} value="weekly" /> {$_("schedules.trigger_weekly")}</label>

      <div class="trigger-detail">
        {#if kind === "interval"}
          <label>{$_("schedules.every_minutes_prefix")} <input type="number" min="1" max="10080" bind:value={everyMinutes} /> {$_("schedules.every_minutes_suffix")}</label>
        {:else if kind === "daily"}
          <label>{$_("schedules.time_label")} <input type="time" bind:value={time} /></label>
        {:else}
          <label>{$_("schedules.weekday_label")}
            <select bind:value={weekday}>
              {#each WD as w, i}<option value={i}>{w}</option>{/each}
            </select>
          </label>
          <label>{$_("schedules.time_label")} <input type="time" bind:value={time} /></label>
        {/if}
        {#if kind !== "interval"}
          <label>{$_("schedules.tz_label")}
            <select bind:value={tz}>
              {#each TZS as t}<option value={t.value}>{t.label}</option>{/each}
            </select>
          </label>
        {/if}
      </div>
    </fieldset>

    <label>{$_("schedules.autonomy_label")}
      <select bind:value={autonomy}>
        <option value="full">full</option>
        <option value="confirm_critical">confirm_critical</option>
        <option value="confirm_each">confirm_each</option>
      </select>
    </label>
    <label>{$_("schedules.max_steps_label")} <input type="number" min="1" max="200" bind:value={maxSteps} /></label>
    <label><input type="checkbox" bind:checked={enabled} /> {$_("schedules.enabled_label")}</label>

    <div class="actions">
      <button onclick={save}>{editing ? $_("schedules.save_edit_button") : $_("schedules.save_new_button")}</button>
      {#if editing}<button class="ghost" onclick={reset}>{$_("schedules.cancel_button")}</button>{/if}
    </div>
  </section>

  <section class="list">
    <h2>{$_("schedules.list_heading", { values: { n: items.length } })}</h2>
    {#each items as s (s.id)}
      <div class="row" class:active={editing?.id === s.id} class:disabled={!s.enabled}>
        <div class="info">
          <div class="name">
            {s.enabled ? "🟢" : "⚪"} {s.name}
            <span class="trigger-tag">{fmtSpec(s.spec)}</span>
          </div>
          <div class="instr">{s.instruction}</div>
          <div class="meta">
            {$_("schedules.next_label")} {fmtTime(s.next_ms)} · {$_("schedules.last_label")} {fmtTime(s.last_run_ms)} ·
            {s.autonomy} · {$_("schedules.step_count", { values: { n: s.max_steps } })}
          </div>
        </div>
        <div class="ops">
          <button class="ghost" onclick={() => toggle(s)}>{s.enabled ? $_("schedules.pause_button") : $_("schedules.enable_button")}</button>
          <button class="ghost" onclick={() => startEdit(s)}>{$_("schedules.edit_button")}</button>
          <button class="danger" onclick={() => del(s.id)}>{$_("schedules.delete_button")}</button>
        </div>
      </div>
    {/each}
    {#if !items.length}<p class="empty">{$_("schedules.empty")}</p>{/if}
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
  .editor input[type="text"], .editor input:not([type]), .editor input[type="number"],
  .editor input[type="time"], .editor select {
    margin-left: 0.4rem; padding: 0.25rem 0.4rem; border: 1px solid #d1d5db; border-radius: 4px;
  }
  .editor textarea { display: block; width: 100%; box-sizing: border-box; margin-top: 0.2rem;
                     padding: 0.4rem; border: 1px solid #d1d5db; border-radius: 4px; font: inherit; }
  fieldset.trigger { border: 1px solid #e5e7eb; padding: 0.5rem 0.8rem; border-radius: 4px; margin: 0.5rem 0; }
  fieldset.trigger label { display: inline-flex; align-items: center; gap: 0.2rem; margin-right: 0.8rem; }
  .trigger-detail { margin-top: 0.4rem; }
  .actions button { padding: 0.4rem 1rem; background: #2563eb; color: #fff; border: 0;
                    border-radius: 4px; cursor: pointer; margin-right: 0.4rem; }
  .actions button.ghost { background: #fff; color: #2563eb; border: 1px solid #93c5fd; }
  .row { display: flex; align-items: center; gap: 0.6rem; padding: 0.6rem;
         border: 1px solid #e5e7eb; border-radius: 6px; margin-bottom: 0.4rem; background: #fff; }
  .row.active { border-color: #2563eb; }
  .row.disabled { opacity: 0.6; }
  .info { flex: 1; min-width: 0; }
  .name { font-weight: 600; }
  .trigger-tag { background: #eff6ff; color: #1d4ed8; padding: 0 0.4rem; border-radius: 3px;
                 font-size: 0.75rem; margin-left: 0.4rem; font-weight: 500; }
  .instr { font-size: 0.85rem; color: #374151; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .meta { font-size: 0.72rem; color: #6b7280; margin-top: 0.15rem; }
  .ops button { padding: 0.3rem 0.6rem; background: #2563eb; color: #fff; border: 0;
                border-radius: 4px; cursor: pointer; margin-left: 0.3rem; font-size: 0.85rem; }
  .ops button.ghost { background: #fff; color: #2563eb; border: 1px solid #93c5fd; }
  .ops button.danger { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
  .empty { color: #9ca3af; font-size: 0.85rem; }
</style>
