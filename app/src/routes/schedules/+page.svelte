<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";

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

  const WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

  // 时区下拉：按 UTC 偏移从西到东各取一个代表，覆盖 UTC-12 ~ UTC+14。
  // 同一偏移不重复列；含半小时 / 45 分钟时区。空值 = 本机时间。
  const TZS: { value: string; label: string }[] = [
    { value: "", label: "本机时间 (默认)" },
    { value: "Etc/GMT+12",         label: "UTC-12  Baker Island" },
    { value: "Pacific/Pago_Pago",  label: "UTC-11  美属萨摩亚 / 纽埃" },
    { value: "Pacific/Honolulu",   label: "UTC-10  夏威夷 / 大溪地" },
    { value: "America/Anchorage",  label: "UTC-9   阿拉斯加" },
    { value: "America/Los_Angeles",label: "UTC-8   洛杉矶 / 温哥华 (PT)" },
    { value: "America/Denver",     label: "UTC-7   丹佛 / 凤凰城 (MT)" },
    { value: "America/Chicago",    label: "UTC-6   芝加哥 / 墨西哥城 (CT)" },
    { value: "America/New_York",   label: "UTC-5   纽约 / 多伦多 (ET)" },
    { value: "America/Caracas",    label: "UTC-4   加拉加斯 / 圣地亚哥" },
    { value: "America/Sao_Paulo",  label: "UTC-3   圣保罗 / 布宜诺斯艾利斯" },
    { value: "Atlantic/South_Georgia", label: "UTC-2   南乔治亚" },
    { value: "Atlantic/Azores",    label: "UTC-1   亚速尔 / 佛得角" },
    { value: "UTC",                label: "UTC±0   协调世界时 / 伦敦冬季" },
    { value: "Europe/Paris",       label: "UTC+1   巴黎 / 柏林 / 罗马" },
    { value: "Europe/Athens",      label: "UTC+2   雅典 / 开罗 / 约翰内斯堡" },
    { value: "Europe/Moscow",      label: "UTC+3   莫斯科 / 伊斯坦布尔 / 利雅得" },
    { value: "Asia/Dubai",         label: "UTC+4   迪拜 / 巴库" },
    { value: "Asia/Karachi",       label: "UTC+5   卡拉奇 / 塔什干" },
    { value: "Asia/Dhaka",         label: "UTC+6   达卡 / 阿拉木图" },
    { value: "Asia/Bangkok",       label: "UTC+7   曼谷 / 雅加达 / 河内" },
    { value: "Asia/Shanghai",      label: "UTC+8   上海 / 香港 / 台北 / 新加坡" },
    { value: "Asia/Tokyo",         label: "UTC+9   日本 / 韩国" },
    { value: "Australia/Sydney",   label: "UTC+10  悉尼 / 关岛" },
    { value: "Pacific/Noumea",     label: "UTC+11  新喀里多尼亚 / 所罗门" },
    { value: "Pacific/Auckland",   label: "UTC+12  奥克兰 / 斐济" },
    { value: "Pacific/Apia",       label: "UTC+13  萨摩亚 / 汤加" },
    { value: "Pacific/Kiritimati", label: "UTC+14  基里巴斯" },
  ];

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
      if (!instruction.trim()) { err = "instruction 不能为空"; return; }
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
    if (!confirm("删除这个定时任务？")) return;
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
    const tzTag = s.tz ? ` @ ${s.tz}` : "";
    if (s.kind === "interval") return `每 ${s.every_minutes} 分钟${tzTag}`;
    if (s.kind === "daily") return `每天 ${s.time}${tzTag}`;
    return `每${WD[s.weekday] ?? "?"} ${s.time}${tzTag}`;
  }

  function fmtTime(ms?: number): string {
    if (!ms) return "—";
    return new Date(ms).toLocaleString();
  }

  onMount(load);
</script>

<div class="page">
  <header>
    <a class="back" href="/">‹ 返回</a>
    <h1>定时任务</h1>
  </header>
  <p class="hint">
    调度器随主进程常驻，60s tick 一次。当任务到点时若另一任务在跑，会跳过本次（事件
    <code>schedule_skipped</code>）。<strong>仅当应用在前台或托盘运行时生效。</strong>
  </p>

  {#if err}<p class="err">{err}</p>{/if}

  <section class="editor">
    <h2>{editing ? "编辑定时" : "新建定时"}</h2>
    <label>名称 <input bind:value={name} placeholder="例：每天检查邮件" /></label>
    <label>Instruction
      <textarea rows="3" bind:value={instruction} placeholder="到点要让 Agent 做什么…"></textarea>
    </label>

    <fieldset class="trigger">
      <legend>触发</legend>
      <label><input type="radio" bind:group={kind} value="interval" /> 间隔</label>
      <label><input type="radio" bind:group={kind} value="daily" /> 每天</label>
      <label><input type="radio" bind:group={kind} value="weekly" /> 每周</label>

      <div class="trigger-detail">
        {#if kind === "interval"}
          <label>每 <input type="number" min="1" max="10080" bind:value={everyMinutes} /> 分钟跑一次</label>
        {:else if kind === "daily"}
          <label>时间 <input type="time" bind:value={time} /></label>
        {:else}
          <label>星期
            <select bind:value={weekday}>
              {#each WD as w, i}<option value={i}>{w}</option>{/each}
            </select>
          </label>
          <label>时间 <input type="time" bind:value={time} /></label>
        {/if}
        {#if kind !== "interval"}
          <label>时区
            <select bind:value={tz}>
              {#each TZS as t}<option value={t.value}>{t.label}</option>{/each}
            </select>
          </label>
        {/if}
      </div>
    </fieldset>

    <label>自动度
      <select bind:value={autonomy}>
        <option value="full">full</option>
        <option value="confirm_critical">confirm_critical</option>
        <option value="confirm_each">confirm_each</option>
      </select>
    </label>
    <label>步数 <input type="number" min="1" max="200" bind:value={maxSteps} /></label>
    <label><input type="checkbox" bind:checked={enabled} /> 启用</label>

    <div class="actions">
      <button onclick={save}>{editing ? "保存修改" : "添加定时"}</button>
      {#if editing}<button class="ghost" onclick={reset}>取消</button>{/if}
    </div>
  </section>

  <section class="list">
    <h2>已有定时（{items.length}）</h2>
    {#each items as s (s.id)}
      <div class="row" class:active={editing?.id === s.id} class:disabled={!s.enabled}>
        <div class="info">
          <div class="name">
            {s.enabled ? "🟢" : "⚪"} {s.name}
            <span class="trigger-tag">{fmtSpec(s.spec)}</span>
          </div>
          <div class="instr">{s.instruction}</div>
          <div class="meta">
            下次：{fmtTime(s.next_ms)} · 上次：{fmtTime(s.last_run_ms)} ·
            {s.autonomy} · {s.max_steps} 步
          </div>
        </div>
        <div class="ops">
          <button class="ghost" onclick={() => toggle(s)}>{s.enabled ? "暂停" : "启用"}</button>
          <button class="ghost" onclick={() => startEdit(s)}>编辑</button>
          <button class="danger" onclick={() => del(s.id)}>✕</button>
        </div>
      </div>
    {/each}
    {#if !items.length}<p class="empty">还没有定时任务，先在上面新建。</p>{/if}
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
