<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";
  import { _ } from "svelte-i18n";
  import { appConfirm } from "$lib/appConfirm.svelte";

  type Spec =
    | { kind: "secondly"; every: number }
    | { kind: "minutely"; every: number }
    | { kind: "hourly"; minute: number; tz?: string }
    | { kind: "daily"; time: string; tz?: string }
    | { kind: "weekly"; weekday: number; time: string; tz?: string };

  type Constraints = {
    hours?: number[];
    weekdays?: number[];
    date_start_ms?: number;
    date_end_ms?: number;
  };

  type Sched = {
    id: string; name: string; instruction: string; spec: Spec;
    action?: "task" | "visual_notify" | "scan_launcher_icons" | "promote_tray_icons";
    enabled: boolean;
    next_ms?: number; last_run_ms?: number;
    constraints?: Constraints;
    auto_chat_apps?: string[];
    auto_chat_extra?: string;
    auto_chat_rules?: RuleOverride[];
    auto_chat_customs?: CustomRule[];
    taskbar_allow_visual?: boolean;
    taskbar_allow_uia?: boolean;
  };

  type InstalledApp = { key: string; name: string; icon: string };

  // AUTO-REPLY SAFETY POLICY rule editor types (5/21).
  // The 6 built-in rules A..F live in the Python sidecar (see
  // `_DEFAULT_AUTO_CHAT_RULES`). The frontend fetches them once on mount via
  // the `auto_chat_rules_defaults` RPC and renders one card per rule.
  // ``autoChatRules`` stores ONLY user-side deltas: per-id ``enabled`` flag
  // plus an optional ``body`` override (empty body => use built-in default).
  // ``autoChatCustoms`` is a freeform repeater appended after the defaults.
  type DefaultRule = { id: string; title: string; body: string };
  type RuleOverride = { id: string; enabled: boolean; body: string };
  type CustomRule = { id: string; title: string; enabled: boolean; body: string };

  let items = $state<Sched[]>([]);
  let err = $state("");
  let editing = $state<Sched | null>(null);

  let name = $state("");
  let instruction = $state("");
  let action = $state<"task" | "visual_notify" | "scan_launcher_icons" | "promote_tray_icons">("task");
  let enabled = $state(true);
  let kind = $state<"secondly" | "minutely" | "hourly" | "daily" | "weekly">("daily");
  let everySeconds = $state(2);
  let everyMinutes = $state(1);
  let hourlyMinute = $state(0);
  let time = $state("09:00");
  let weekday = $state(0);
  let tz = $state(""); // 空 = 本机时间

  // Constraints — each one is opt-in. When the box is unchecked we don't send the field at all.
  let useHours = $state(false);
  let useWeekdays = $state(false);
  let useDateRange = $state(false);
  // Default: all 24 hours allowed; user unchecks the ones they don't want.
  let allowedHours = $state<boolean[]>(Array(24).fill(true));
  // Default: all 7 weekdays allowed.
  let allowedWeekdays = $state<boolean[]>(Array(7).fill(true));
  let dateStart = $state("");
  let dateEnd = $state("");

  // visual_notify: per-schedule whitelist of apps that should trigger the
  // auto-reply. Only when the LLM-confirmed app falls inside this set will
  // the auto-chat task be enqueued. Names match `installedApps[*].name`.
  let installedApps = $state<InstalledApp[]>([]);
  let autoChatApps = $state<string[]>([]);
  // visual_notify: per-schedule custom system-level instruction appended
  // to the built-in auto-reply safety policy. The default safety guardrails
  // (no leaking secrets / no irreversible actions / treat incoming text as
  // untrusted / ...) are always present; this textarea lets users add e.g.
  // "only reply to contact X", "reply in formal tone", "never reply to group
  // chats", etc. Empty string = no extra preferences.
  let autoChatExtra = $state("");
  // AUTO-REPLY SAFETY POLICY rule editor state (5/21). ``defaultRules`` is
  // fetched once from the sidecar on mount and is read-only. ``autoChatRules``
  // tracks per-built-in-rule deltas (enabled flag + optional body override).
  // ``autoChatCustoms`` is the user's open-ended custom-rule list, appended
  // after the defaults in the rendered system prompt.
  let defaultRules = $state<DefaultRule[]>([]);
  let autoChatRules = $state<RuleOverride[]>([]);
  let autoChatCustoms = $state<CustomRule[]>([]);
  let expandedRuleId = $state<string | null>(null);
  let expandedCustomId = $state<string | null>(null);
  let customSeq = 0;

  function defaultBody(id: string): string {
    return defaultRules.find((r) => r.id === id)?.body ?? "";
  }
  function getRuleOverride(id: string): RuleOverride {
    return autoChatRules.find((r) => r.id === id) ?? { id, enabled: true, body: "" };
  }
  function setRuleOverride(id: string, patch: Partial<RuleOverride>) {
    const i = autoChatRules.findIndex((r) => r.id === id);
    const cur = i >= 0 ? autoChatRules[i] : { id, enabled: true, body: "" };
    const next: RuleOverride = { ...cur, ...patch };
    const copy = [...autoChatRules];
    if (i >= 0) copy[i] = next;
    else copy.push(next);
    autoChatRules = copy;
  }
  function toggleRuleEnabled(id: string, on: boolean) {
    setRuleOverride(id, { enabled: on });
  }
  function setRuleBody(id: string, body: string) {
    setRuleOverride(id, { body });
  }
  function resetRuleBody(id: string) {
    setRuleOverride(id, { body: "" });
  }
  function ruleIsCustomized(id: string): boolean {
    const o = autoChatRules.find((r) => r.id === id);
    return !!(o && o.body && o.body.trim());
  }
  function ruleCurrentBody(id: string): string {
    const o = getRuleOverride(id);
    return o.body || defaultBody(id);
  }
  function toggleRuleExpand(id: string) {
    expandedRuleId = expandedRuleId === id ? null : id;
  }
  function addCustomRule() {
    customSeq += 1;
    const c: CustomRule = { id: `u${Date.now()}${customSeq}`, title: "", enabled: true, body: "" };
    autoChatCustoms = [...autoChatCustoms, c];
    expandedCustomId = c.id;
  }
  function removeCustomRule(id: string) {
    autoChatCustoms = autoChatCustoms.filter((c) => c.id !== id);
    if (expandedCustomId === id) expandedCustomId = null;
  }
  function updateCustomRule(id: string, patch: Partial<CustomRule>) {
    autoChatCustoms = autoChatCustoms.map((c) => (c.id === id ? { ...c, ...patch } : c));
  }
  function toggleCustomExpand(id: string) {
    expandedCustomId = expandedCustomId === id ? null : id;
  }
  function defaultsToOverrides(): RuleOverride[] {
    // Build a fresh "all enabled, no body overrides" set for the current
    // defaults. New schedules + reset() use this.
    return defaultRules.map((r) => ({ id: r.id, enabled: true, body: "" }));
  }
  // visual_notify: per-schedule listening-channel toggles. Default both ON for
  // widest coverage. UIA priority > visual (zero LLM cost vs every visual hit
  // burning tokens); when UIA fires it also suppresses the visual channel for
  // a short window via taskbar_uia.visual_suppress_after_uia_sec.
  let taskbarAllowVisual = $state(false);
  let taskbarAllowUia = $state(true);
  // Apps default-checked when creating a new visual_notify schedule.
  // Match against the **exact** installed-app name (case-insensitive). We
  // used to do a substring match but that pulled in things like
  // "微信开发者工具" / "卸载微信开发者工具" because they happen to contain "微信".
  // Keep this list narrow — only Microsoft Teams is checked by default. Users
  // can opt in to WeChat or other clients via the picker.
  const DEFAULT_AUTO_CHAT_EXACT = new Set([
    "microsoft teams",
    "teams",
    "teams (work or school)",
    "microsoft teams (work or school)",
  ]);

  function defaultAutoChatApps(): string[] {
    return installedApps
      .map((a) => a.name)
      .filter((nm) => DEFAULT_AUTO_CHAT_EXACT.has(nm.trim().toLowerCase()));
  }

  function toggleAutoChatApp(name: string) {
    if (autoChatApps.includes(name)) {
      autoChatApps = autoChatApps.filter((a) => a !== name);
    } else {
      autoChatApps = [...autoChatApps, name];
    }
  }

  // Compact dropdown UI state for the auto-chat-apps multi-select.
  let autoChatPickerOpen = $state(false);
  let autoChatSearch = $state("");
  let autoChatPickerEl = $state<HTMLDivElement | null>(null);
  let autoChatRefreshing = $state(false);
  // Rescan once per page-load when the user first opens the picker, so a
  // freshly-installed (or just-uninstalled) app shows up without waiting
  // for the daily scheduled scan.
  let autoChatAutoRescanned = false;

  async function refreshInstalledApps(rescan: boolean) {
    autoChatRefreshing = true;
    try {
      const r = await invoke<{ items: InstalledApp[] }>(
        "installed_apps_list",
        { rescan },
      );
      installedApps = r?.items ?? [];
      // Drop any selected names that no longer exist after the rescan
      // (e.g. user uninstalled the app). Keep order stable.
      const present = new Set(installedApps.map((a) => a.name));
      autoChatApps = autoChatApps.filter((nm) => present.has(nm));
    } catch (e) {
      err = String(e);
    } finally {
      autoChatRefreshing = false;
    }
  }

  async function openAutoChatPicker() {
    autoChatPickerOpen = !autoChatPickerOpen;
    if (autoChatPickerOpen && !autoChatAutoRescanned) {
      autoChatAutoRescanned = true;
      await refreshInstalledApps(true);
    }
  }

  let filteredInstalledApps = $derived.by(() => {
    const q = autoChatSearch.trim().toLowerCase();
    if (!q) return installedApps;
    return installedApps.filter((a) => a.name.toLowerCase().includes(q));
  });

  function appIcon(name: string): string {
    return installedApps.find((a) => a.name === name)?.icon ?? "";
  }

  // Close the picker when the user clicks outside it.
  function onDocClick(ev: MouseEvent) {
    if (!autoChatPickerOpen) return;
    const root = autoChatPickerEl;
    if (root && ev.target instanceof Node && !root.contains(ev.target)) {
      autoChatPickerOpen = false;
    }
  }

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
    action = "task";
    enabled = true;
    kind = "daily";
    everySeconds = 2;
    everyMinutes = 1;
    hourlyMinute = 0;
    time = "09:00";
    weekday = 0;
    tz = "";
    useHours = false;
    useWeekdays = false;
    useDateRange = false;
    allowedHours = Array(24).fill(true);
    allowedWeekdays = Array(7).fill(true);
    dateStart = "";
    dateEnd = "";
    autoChatApps = defaultAutoChatApps();
    autoChatExtra = "";
    autoChatRules = defaultsToOverrides();
    autoChatCustoms = [];
    expandedRuleId = null;
    expandedCustomId = null;
    taskbarAllowVisual = false;
    taskbarAllowUia = true;
  }

  // ms -> "YYYY-MM-DDTHH:MM" in local tz, suitable for <input type="datetime-local">.
  function msToLocal(ms?: number): string {
    if (!ms) return "";
    const d = new Date(ms);
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function startEdit(s: Sched) {
    editing = s;
    name = s.name;
    instruction = s.instruction;
    action = (s.action as "task" | "visual_notify" | "scan_launcher_icons" | "promote_tray_icons") ?? "task";
    enabled = !!s.enabled;
    kind = s.spec.kind as any;
    if (s.spec.kind === "secondly") everySeconds = s.spec.every;
    if (s.spec.kind === "minutely") everyMinutes = s.spec.every;
    if (s.spec.kind === "hourly") hourlyMinute = s.spec.minute;
    if (s.spec.kind === "daily") time = s.spec.time;
    if (s.spec.kind === "weekly") { time = s.spec.time; weekday = s.spec.weekday; }
    tz = "tz" in s.spec ? (s.spec.tz ?? "") : "";
    // Hydrate constraint groups. Missing field => not active; default mask all-true.
    const c = s.constraints ?? {};
    if (Array.isArray(c.hours) && c.hours.length) {
      useHours = true;
      const set = new Set(c.hours);
      allowedHours = Array.from({ length: 24 }, (_, i) => set.has(i));
    } else {
      useHours = false;
      allowedHours = Array(24).fill(true);
    }
    if (Array.isArray(c.weekdays) && c.weekdays.length) {
      useWeekdays = true;
      const set = new Set(c.weekdays);
      allowedWeekdays = Array.from({ length: 7 }, (_, i) => set.has(i));
    } else {
      useWeekdays = false;
      allowedWeekdays = Array(7).fill(true);
    }
    if (c.date_start_ms || c.date_end_ms) {
      useDateRange = true;
      dateStart = msToLocal(c.date_start_ms);
      dateEnd = msToLocal(c.date_end_ms);
    } else {
      useDateRange = false;
      dateStart = "";
      dateEnd = "";
    }
    autoChatApps = Array.isArray(s.auto_chat_apps)
      ? [...s.auto_chat_apps]
      // Legacy schedule (created before the whitelist feature) — no field
      // saved yet. Fall back to the Teams default so users don't
      // see a fully unchecked list.
      : defaultAutoChatApps();
    autoChatExtra = typeof s.auto_chat_extra === "string" ? s.auto_chat_extra : "";
    // Hydrate rule editor state. For any rule id present in the schedule's
    // overrides, use the stored {enabled, body}; otherwise default to
    // {enabled: true, body: ""}. Legacy schedules with no overrides field
    // get a fresh "all defaults" set.
    if (Array.isArray(s.auto_chat_rules) && s.auto_chat_rules.length) {
      const byId = new Map(s.auto_chat_rules.map((r) => [r.id, r]));
      autoChatRules = defaultRules.map((r) => {
        const o = byId.get(r.id);
        return o
          ? { id: r.id, enabled: o.enabled !== false, body: typeof o.body === "string" ? o.body : "" }
          : { id: r.id, enabled: true, body: "" };
      });
    } else {
      autoChatRules = defaultsToOverrides();
    }
    autoChatCustoms = Array.isArray(s.auto_chat_customs)
      ? s.auto_chat_customs.map((c) => ({
          id: String(c.id || `u${Date.now()}${++customSeq}`),
          title: String(c.title || ""),
          enabled: c.enabled !== false,
          body: String(c.body || ""),
        }))
      : [];
    // Migrate the legacy free-form ``auto_chat_extra`` textarea into a single
    // custom rule the first time a schedule that used it is edited under the
    // new UI. The legacy field is left intact on the backend (still honoured
    // by the system-prompt builder) until the user saves again, at which
    // point save() blanks it out.
    if (autoChatExtra.trim() && autoChatCustoms.length === 0) {
      customSeq += 1;
      autoChatCustoms = [{
        id: `u-legacy-${customSeq}`,
        title: "",
        enabled: true,
        body: autoChatExtra.trim(),
      }];
    }
    expandedRuleId = null;
    expandedCustomId = null;
    // Per-schedule channel toggles. Legacy schedules created before this
    // feature have no field saved -> default to true (both on).
    taskbarAllowVisual = typeof s.taskbar_allow_visual === "boolean" ? s.taskbar_allow_visual : false;
    taskbarAllowUia = typeof s.taskbar_allow_uia === "boolean" ? s.taskbar_allow_uia : true;
  }

  function buildSpec(): Spec {
    const t = tz.trim();
    if (kind === "secondly") return { kind: "secondly", every: Math.max(1, Math.min(3600, everySeconds || 1)) };
    if (kind === "minutely") return { kind: "minutely", every: Math.max(1, Math.min(1440, everyMinutes || 1)) };
    if (kind === "hourly") return { kind: "hourly", minute: hourlyMinute, ...(t ? { tz: t } : {}) };
    if (kind === "daily") return { kind: "daily", time, ...(t ? { tz: t } : {}) };
    return { kind: "weekly", weekday, time, ...(t ? { tz: t } : {}) };
  }

  // Parse "YYYY-MM-DDTHH:MM" as local time -> ms epoch. Returns null if blank.
  function localToMs(s: string): number | null {
    const v = s.trim();
    if (!v) return null;
    const ms = Date.parse(v);
    return Number.isFinite(ms) ? ms : null;
  }

  function buildConstraints(): Constraints {
    const out: Constraints = {};
    if (useHours) {
      const hs = allowedHours.map((on, i) => (on ? i : -1)).filter((x) => x >= 0);
      if (!hs.length) throw new Error($_("schedules.constraint_hours_empty"));
      out.hours = hs;
    }
    if (useWeekdays) {
      const ws = allowedWeekdays.map((on, i) => (on ? i : -1)).filter((x) => x >= 0);
      if (!ws.length) throw new Error($_("schedules.constraint_weekdays_empty"));
      out.weekdays = ws;
    }
    if (useDateRange) {
      const ds = localToMs(dateStart);
      const de = localToMs(dateEnd);
      if (ds !== null && de !== null && de < ds) throw new Error($_("schedules.constraint_daterange_invalid"));
      if (ds !== null) out.date_start_ms = ds;
      if (de !== null) out.date_end_ms = de;
    }
    return out;
  }

  async function save() {
    err = "";
    try {
      const resolvedInstruction = action === "visual_notify"
        ? "__visual_notify_tick__"
        : action === "scan_launcher_icons"
          ? "__scan_launcher_icons__"
          : action === "promote_tray_icons"
            ? "__promote_tray_icons__"
            : instruction.trim();
      if (!resolvedInstruction) { err = $_("schedules.instruction_required"); return; }
      const spec = buildSpec();
      const constraints = buildConstraints();
      // Strip the rule-override list down to only entries that actually
      // differ from the built-in defaults (saves payload + keeps the JSON
      // human-readable for users who never touch the safety policy). A rule
      // is "interesting" if it is disabled or its body has been edited.
      const rulesPayload = autoChatRules
        .filter((r) => r.enabled === false || (r.body && r.body.trim()))
        .map((r) => ({ id: r.id, enabled: r.enabled, body: r.body || "" }));
      // Drop empty-body customs and trim whitespace.
      const customsPayload = autoChatCustoms
        .filter((c) => c.body && c.body.trim())
        .map((c) => ({
          id: c.id,
          title: c.title || "",
          enabled: c.enabled !== false,
          body: c.body,
        }));
      // Migration: now that the structured editor owns the user's
      // preferences, blank out the legacy free-form field on save so it
      // doesn't get double-applied alongside the migrated custom rule.
      const legacyExtraPayload = customsPayload.length > 0 ? "" : autoChatExtra;
      if (editing) {
        await invoke("schedule_update", { id: editing.id, name, instruction: resolvedInstruction, action, spec, enabled, constraints, autoChatApps: action === "visual_notify" ? autoChatApps : null, autoChatExtra: action === "visual_notify" ? legacyExtraPayload : null, autoChatRules: action === "visual_notify" ? rulesPayload : null, autoChatCustoms: action === "visual_notify" ? customsPayload : null, taskbarAllowVisual: action === "visual_notify" ? taskbarAllowVisual : null, taskbarAllowUia: action === "visual_notify" ? taskbarAllowUia : null });
      } else {
        await invoke("schedule_add", { name, instruction: resolvedInstruction, action, spec, enabled, constraints, autoChatApps: action === "visual_notify" ? autoChatApps : null, autoChatExtra: action === "visual_notify" ? legacyExtraPayload : null, autoChatRules: action === "visual_notify" ? rulesPayload : null, autoChatCustoms: action === "visual_notify" ? customsPayload : null, taskbarAllowVisual: action === "visual_notify" ? taskbarAllowVisual : null, taskbarAllowUia: action === "visual_notify" ? taskbarAllowUia : null });
      }
      reset();
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  async function del(id: string) {
    if (!(await appConfirm($_("schedules.delete_confirm"), { danger: true }))) return;
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

  async function runNow(s: Sched) {
    try {
      await invoke("schedule_run_now", { id: s.id });
    } catch (e) {
      err = String(e);
    }
  }

  function fmtSpec(s: Spec): string {
    const tzValue = "tz" in s ? (s.tz ?? "") : "";
    const tzTag = tzValue ? $_("schedules.tz_suffix", { values: { tz: tzValue } }) : "";
    if (s.kind === "secondly") return $_("schedules.fmt_secondly", { values: { every: s.every } });
    if (s.kind === "minutely") return $_("schedules.fmt_minutely", { values: { every: s.every } });
    if (s.kind === "hourly") return $_("schedules.fmt_hourly", { values: { minute: String(s.minute).padStart(2, "0"), tz: tzTag } });
    if (s.kind === "daily") return $_("schedules.fmt_daily", { values: { time: s.time, tz: tzTag } });
    return $_("schedules.fmt_weekly", { values: { weekday: WD[s.weekday] ?? "?", time: s.time, tz: tzTag } });
  }

  function fmtConstraints(s: Sched): string {
    const c = s.constraints ?? {};
    const parts: string[] = [];
    if (Array.isArray(c.hours) && c.hours.length && c.hours.length < 24) {
      parts.push($_("schedules.fmt_hours", { values: { hours: c.hours.map((h) => String(h).padStart(2, "0")).join(",") } }));
    }
    if (Array.isArray(c.weekdays) && c.weekdays.length && c.weekdays.length < 7) {
      parts.push($_("schedules.fmt_weekdays", { values: { weekdays: c.weekdays.map((w) => WD[w] ?? "?").join(",") } }));
    }
    if (c.date_start_ms || c.date_end_ms) {
      const a = c.date_start_ms ? new Date(c.date_start_ms).toLocaleDateString() : "…";
      const b = c.date_end_ms ? new Date(c.date_end_ms).toLocaleDateString() : "…";
      parts.push($_("schedules.fmt_daterange", { values: { start: a, end: b } }));
    }
    if (!parts.length) return "";
    return " · " + parts.join(" · ");
  }

  function fmtTime(ms?: number): string {
    if (!ms) return $_("schedules.time_unknown");
    return new Date(ms).toLocaleString();
  }

  function isVisualNotify(s: Sched | null | undefined): boolean {
    return (s?.action ?? "task") === "visual_notify";
  }

  function isLauncherScan(s: Sched | null | undefined): boolean {
    return (s?.action ?? "task") === "scan_launcher_icons";
  }

  function isTrayPromote(s: Sched | null | undefined): boolean {
    return (s?.action ?? "task") === "promote_tray_icons";
  }

  function isInternal(s: Sched | null | undefined): boolean {
    return isVisualNotify(s) || isLauncherScan(s) || isTrayPromote(s);
  }

  function displayInstruction(s: Sched): string {
    if (isVisualNotify(s)) return $_("schedules.visual_notify_instruction");
    if (isLauncherScan(s)) return $_("schedules.launcher_scan_instruction");
    if (isTrayPromote(s)) return $_("schedules.tray_promote_instruction");
    return s.instruction;
  }

  function displayName(s: Sched): string {
    if (isVisualNotify(s)) return $_("schedules.visual_notify_name");
    if (isLauncherScan(s)) return $_("schedules.launcher_scan_name");
    if (isTrayPromote(s)) return $_("schedules.tray_promote_name");
    return s.name;
  }

  onMount(async () => {
    // Fetch built-in AUTO-REPLY SAFETY POLICY rule defaults so the rule
    // editor below can render one card per rule. Failure here just leaves
    // the editor empty (the sidecar would still apply its built-in
    // defaults to any visual_notify run).
    try {
      const d = await invoke<{ rules: DefaultRule[] }>("auto_chat_rules_defaults");
      defaultRules = Array.isArray(d?.rules) ? d.rules : [];
      // Seed the new-form rule overrides only after defaults are loaded.
      if (!editing && autoChatRules.length === 0) {
        autoChatRules = defaultsToOverrides();
      }
    } catch { /* defaults optional */ }
    try {
      const r = await invoke<{ items: InstalledApp[] }>("installed_apps_list");
      installedApps = r?.items ?? [];
      // Seed defaults for the (new) form. startEdit() overrides this when
      // editing an existing schedule.
      if (!editing && autoChatApps.length === 0) {
        autoChatApps = defaultAutoChatApps();
      }
    } catch { /* installed apps list optional */ }
    await load();
    if (typeof document !== "undefined") {
      document.addEventListener("click", onDocClick, true);
    }
  });
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
    <label>{$_("schedules.action_label")}
      <select bind:value={action} disabled={!!editing && isInternal(editing)}>
        <option value="task">{$_("schedules.action_task")}</option>
        <option value="visual_notify">{$_("schedules.action_visual_notify")}</option>
        <option value="scan_launcher_icons">{$_("schedules.action_launcher_scan")}</option>
        <option value="promote_tray_icons">{$_("schedules.action_tray_promote")}</option>
      </select>
    </label>
    {#if action === "visual_notify"}
      <p class="sub-hint visual-note">{$_("schedules.visual_notify_instruction")}</p>
      <fieldset class="trigger taskbar-channels">
        <legend>{$_("schedules.taskbar_channels_legend")}</legend>
        <p class="sub-hint">{$_("schedules.taskbar_channels_hint")}</p>
        <label><input type="checkbox" bind:checked={taskbarAllowUia} /> {$_("schedules.taskbar_channel_uia")}</label>
        <label><input type="checkbox" bind:checked={taskbarAllowVisual} /> {$_("schedules.taskbar_channel_visual")}</label>
      </fieldset>
      <fieldset class="trigger auto-chat-apps">
        <legend>{$_("schedules.auto_chat_apps_legend")}</legend>
        <p class="sub-hint">{$_("schedules.auto_chat_apps_hint")}</p>
        {#if installedApps.length === 0}
          <p class="sub-hint">{$_("schedules.auto_chat_apps_empty")}</p>
        {:else}
          <div class="apps-picker" bind:this={autoChatPickerEl}>
            <button
              type="button"
              class="apps-picker-trigger"
              onclick={openAutoChatPicker}
            >
              {#if autoChatApps.length === 0}
                <span class="apps-picker-placeholder">{$_("schedules.auto_chat_apps_pick_placeholder")}</span>
              {:else}
                <span class="apps-picker-chips">
                  {#each autoChatApps as nm (nm)}
                    <span class="apps-picker-chip">
                      {#if appIcon(nm)}<img src={appIcon(nm)} alt="" />{/if}
                      <span class="app-name">{nm}</span>
                      <button
                        type="button"
                        class="chip-x"
                        title={$_("schedules.select_none_button")}
                        onclick={(e) => { e.stopPropagation(); toggleAutoChatApp(nm); }}
                      >×</button>
                    </span>
                  {/each}
                </span>
              {/if}
              <span class="apps-picker-caret">{autoChatPickerOpen ? "▲" : "▼"}</span>
            </button>

            {#if autoChatPickerOpen}
              <div class="apps-picker-panel">
                <input
                  class="apps-picker-search"
                  type="text"
                  placeholder={$_("schedules.auto_chat_apps_search_placeholder")}
                  bind:value={autoChatSearch}
                />
                <div class="apps-picker-list">
                  {#each filteredInstalledApps as app (app.name)}
                    <label class="apps-picker-row" class:checked={autoChatApps.includes(app.name)}>
                      <input
                        type="checkbox"
                        checked={autoChatApps.includes(app.name)}
                        onchange={() => toggleAutoChatApp(app.name)}
                      />
                      {#if app.icon}<img src={app.icon} alt="" />{:else}<span class="app-icon-placeholder"></span>{/if}
                      <span class="app-name">{app.name}</span>
                    </label>
                  {:else}
                    <p class="sub-hint apps-picker-empty">{$_("schedules.auto_chat_apps_no_match")}</p>
                  {/each}
                </div>
                <div class="chip-actions apps-picker-actions">
                  <button type="button" class="ghost" onclick={() => (autoChatApps = installedApps.map((a) => a.name))}>{$_("schedules.select_all_button")}</button>
                  <button type="button" class="ghost" onclick={() => (autoChatApps = [])}>{$_("schedules.select_none_button")}</button>
                  <button type="button" class="ghost" onclick={() => (autoChatApps = defaultAutoChatApps())}>{$_("schedules.auto_chat_apps_defaults_button")}</button>
                  <button type="button" class="ghost" disabled={autoChatRefreshing} onclick={() => refreshInstalledApps(true)}>
                    {autoChatRefreshing ? $_("schedules.auto_chat_apps_refreshing") : $_("schedules.auto_chat_apps_refresh_button")}
                  </button>
                </div>
              </div>
            {/if}
          </div>
        {/if}
      </fieldset>
      <fieldset class="trigger safety-rules">
        <legend>{$_("schedules.safety_rules_legend")}</legend>
        <p class="sub-hint">{$_("schedules.safety_rules_hint")}</p>
        {#if defaultRules.length === 0}
          <p class="sub-hint">{$_("schedules.safety_rules_loading")}</p>
        {:else}
          <ul class="rule-list">
            {#each defaultRules as rule (rule.id)}
              {@const ovr = getRuleOverride(rule.id)}
              {@const customized = ruleIsCustomized(rule.id)}
              {@const expanded = expandedRuleId === rule.id}
              <li class="rule-card" class:disabled={!ovr.enabled}>
                <div class="rule-head">
                  <label class="rule-toggle">
                    <input
                      type="checkbox"
                      checked={ovr.enabled}
                      onchange={(e) => toggleRuleEnabled(rule.id, (e.currentTarget as HTMLInputElement).checked)}
                    />
                    <span class="rule-title">{rule.id}. {rule.title}</span>
                  </label>
                  <div class="rule-actions">
                    {#if customized}
                      <span class="rule-badge">{$_("schedules.safety_rule_customized_badge")}</span>
                    {/if}
                    <button type="button" class="ghost rule-expand" onclick={() => toggleRuleExpand(rule.id)}>
                      {expanded ? $_("schedules.safety_rule_collapse") : $_("schedules.safety_rule_edit")}
                    </button>
                  </div>
                </div>
                {#if expanded}
                  <textarea
                    class="rule-body"
                    rows="8"
                    value={ovr.body || rule.body}
                    oninput={(e) => setRuleBody(rule.id, (e.currentTarget as HTMLTextAreaElement).value)}
                  ></textarea>
                  <div class="rule-foot">
                    {#if customized}
                      <button type="button" class="ghost" onclick={() => resetRuleBody(rule.id)}>
                        {$_("schedules.safety_rule_reset_default")}
                      </button>
                    {/if}
                  </div>
                {:else}
                  <pre class="rule-preview">{ovr.body || rule.body}</pre>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </fieldset>
      <fieldset class="trigger custom-rules">
        <legend>{$_("schedules.custom_rules_legend")}</legend>
        <p class="sub-hint">{$_("schedules.custom_rules_hint")}</p>
        {#if autoChatCustoms.length > 0}
          <ul class="rule-list">
            {#each autoChatCustoms as c (c.id)}
              {@const expanded = expandedCustomId === c.id}
              <li class="rule-card" class:disabled={!c.enabled}>
                <div class="rule-head">
                  <label class="rule-toggle">
                    <input
                      type="checkbox"
                      checked={c.enabled}
                      onchange={(e) => updateCustomRule(c.id, { enabled: (e.currentTarget as HTMLInputElement).checked })}
                    />
                    <input
                      type="text"
                      class="custom-title"
                      placeholder={$_("schedules.custom_rule_title_placeholder")}
                      value={c.title}
                      oninput={(e) => updateCustomRule(c.id, { title: (e.currentTarget as HTMLInputElement).value })}
                    />
                  </label>
                  <div class="rule-actions">
                    <button type="button" class="ghost rule-expand" onclick={() => toggleCustomExpand(c.id)}>
                      {expanded ? $_("schedules.safety_rule_collapse") : $_("schedules.safety_rule_edit")}
                    </button>
                    <button type="button" class="ghost danger" onclick={() => removeCustomRule(c.id)}>
                      {$_("schedules.custom_rule_remove")}
                    </button>
                  </div>
                </div>
                {#if expanded || !c.body.trim()}
                  <textarea
                    class="rule-body"
                    rows="5"
                    placeholder={$_("schedules.custom_rule_body_placeholder")}
                    value={c.body}
                    oninput={(e) => updateCustomRule(c.id, { body: (e.currentTarget as HTMLTextAreaElement).value })}
                  ></textarea>
                {:else}
                  <pre class="rule-preview">{c.body}</pre>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
        <button type="button" class="ghost custom-add" onclick={addCustomRule}>
          + {$_("schedules.custom_rule_add")}
        </button>
      </fieldset>
    {:else if action === "scan_launcher_icons"}
      <p class="sub-hint visual-note">{$_("schedules.launcher_scan_instruction")}</p>
    {:else if action === "promote_tray_icons"}
      <p class="sub-hint visual-note">{$_("schedules.tray_promote_instruction")}</p>
    {:else}
      <label>{$_("schedules.instruction_label")}
        <textarea rows="3" bind:value={instruction} placeholder={$_("schedules.instruction_placeholder")}></textarea>
      </label>
    {/if}

    {#if action === "visual_notify" && !taskbarAllowVisual}
      <fieldset class="trigger no-poll">
        <legend>{$_("schedules.taskbar_no_poll_legend")}</legend>
        <p class="sub-hint">{$_("schedules.taskbar_no_poll_hint")}</p>
      </fieldset>
    {:else}
    <fieldset class="trigger">
      <legend>{$_("schedules.trigger_legend")}</legend>
      {#if action === "visual_notify"}
        <p class="sub-hint">{$_("schedules.taskbar_visual_poll_hint")}</p>
        <label><input type="radio" bind:group={kind} value="secondly" /> {$_("schedules.trigger_secondly")}</label>
        <label><input type="radio" bind:group={kind} value="minutely" /> {$_("schedules.trigger_minutely")}</label>
      {:else}
        <label><input type="radio" bind:group={kind} value="hourly" /> {$_("schedules.trigger_hourly")}</label>
        <label><input type="radio" bind:group={kind} value="daily" /> {$_("schedules.trigger_daily")}</label>
        <label><input type="radio" bind:group={kind} value="weekly" /> {$_("schedules.trigger_weekly")}</label>
        <label><input type="radio" bind:group={kind} value="secondly" /> {$_("schedules.trigger_secondly")}</label>
        <label><input type="radio" bind:group={kind} value="minutely" /> {$_("schedules.trigger_minutely")}</label>
      {/if}

      <div class="trigger-detail">
        {#if kind === "secondly"}
          <label>{$_("schedules.every_seconds_label")} <input type="number" min="1" max="3600" bind:value={everySeconds} /></label>
        {:else if kind === "minutely"}
          <label>{$_("schedules.every_minutes_label")} <input type="number" min="1" max="1440" bind:value={everyMinutes} /></label>
        {:else if kind === "hourly"}
          <label>{$_("schedules.minute_prefix")} <input type="number" min="0" max="59" bind:value={hourlyMinute} /> {$_("schedules.minute_suffix")}</label>
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
        {#if kind !== "secondly" && kind !== "minutely"}
          <label>{$_("schedules.tz_label")}
            <select bind:value={tz}>
              {#each TZS as t}<option value={t.value}>{t.label}</option>{/each}
            </select>
          </label>
        {/if}
      </div>
    </fieldset>
    {/if}

    <fieldset class="trigger constraints">
      <legend>{$_("schedules.constraints_legend")}</legend>
      <p class="sub-hint">{$_("schedules.constraints_hint")}</p>

      <div class="constraint-row">
        <label class="toggle"><input type="checkbox" bind:checked={useHours} /> {$_("schedules.constraint_hours_label")}</label>
        {#if useHours}
          <div class="hours-grid">
            {#each allowedHours as on, i}
              <label class="chip"><input type="checkbox" bind:checked={allowedHours[i]} /> {String(i).padStart(2, "0")}</label>
            {/each}
          </div>
          <div class="chip-actions">
            <button type="button" class="ghost" onclick={() => (allowedHours = Array(24).fill(true))}>{$_("schedules.select_all_button")}</button>
            <button type="button" class="ghost" onclick={() => (allowedHours = Array(24).fill(false))}>{$_("schedules.select_none_button")}</button>
            <button type="button" class="ghost" onclick={() => { const a = Array(24).fill(false); for (let i = 9; i < 17; i++) a[i] = true; allowedHours = a; }}>{$_("schedules.workhours_button")}</button>
          </div>
        {/if}
      </div>

      <div class="constraint-row">
        <label class="toggle"><input type="checkbox" bind:checked={useWeekdays} /> {$_("schedules.constraint_weekdays_label")}</label>
        {#if useWeekdays}
          <div class="wd-grid">
            {#each WD as w, i}
              <label class="chip"><input type="checkbox" bind:checked={allowedWeekdays[i]} /> {w}</label>
            {/each}
          </div>
          <div class="chip-actions">
            <button type="button" class="ghost" onclick={() => (allowedWeekdays = Array(7).fill(true))}>{$_("schedules.select_all_button")}</button>
            <button type="button" class="ghost" onclick={() => (allowedWeekdays = Array(7).fill(false))}>{$_("schedules.select_none_button")}</button>
            <button type="button" class="ghost" onclick={() => { const a = Array(7).fill(false); for (let i = 0; i < 5; i++) a[i] = true; allowedWeekdays = a; }}>{$_("schedules.weekdays_only_button")}</button>
          </div>
        {/if}
      </div>

      <div class="constraint-row">
        <label class="toggle"><input type="checkbox" bind:checked={useDateRange} /> {$_("schedules.constraint_daterange_label")}</label>
        {#if useDateRange}
          <div class="daterange">
            <label>{$_("schedules.window_start_label")} <input type="datetime-local" bind:value={dateStart} /></label>
            <label>{$_("schedules.window_end_label")} <input type="datetime-local" bind:value={dateEnd} /></label>
          </div>
        {/if}
      </div>
    </fieldset>

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
            {s.enabled ? "🟢" : "⚪"} {displayName(s)}
            {#if isVisualNotify(s)}<span class="type-tag">{$_("schedules.action_visual_notify")}</span>{/if}
            {#if isLauncherScan(s)}<span class="type-tag">{$_("schedules.action_launcher_scan")}</span>{/if}
            {#if isTrayPromote(s)}<span class="type-tag">{$_("schedules.action_tray_promote")}</span>{/if}
            <span class="trigger-tag">{fmtSpec(s.spec)}{fmtConstraints(s)}</span>
          </div>
          <div class="instr">{displayInstruction(s)}</div>
          <div class="meta">
            {$_("schedules.next_label")} {fmtTime(s.next_ms)} · {$_("schedules.last_label")} {fmtTime(s.last_run_ms)}{#if isVisualNotify(s)} · {$_("schedules.visual_notify_meta")}{:else if isLauncherScan(s)} · {$_("schedules.launcher_scan_meta")}{:else if isTrayPromote(s)} · {$_("schedules.tray_promote_meta")}{/if}
          </div>
        </div>
        <div class="ops">
          <button class="ghost" onclick={() => toggle(s)}>{s.enabled ? $_("schedules.pause_button") : $_("schedules.enable_button")}</button>
          <button class="ghost" onclick={() => startEdit(s)}>{$_("schedules.edit_button")}</button>
          <button class="ghost" onclick={() => runNow(s)} title={$_("schedules.test_button_hint")}>{$_("schedules.test_button")}</button>
          <button class="danger" onclick={() => del(s.id)}>{$_("schedules.delete_button")}</button>
        </div>
      </div>
    {/each}
    {#if !items.length}<p class="empty">{$_("schedules.empty")}</p>{/if}
  </section>
</div>

<style>
  /* AUTO-REPLY SAFETY POLICY rule-editor (5/21). */
  .safety-rules .sub-hint, .custom-rules .sub-hint { margin-bottom: 0.5rem; }
  .rule-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 0.35rem; }
  .rule-card { border: 1px solid #d1d5db; border-radius: 6px; padding: 0.45rem 0.55rem; background: #fafafa; }
  .rule-card.disabled { background: #f3f4f6; opacity: 0.65; }
  .rule-head { display: flex; align-items: center; gap: 0.5rem; }
  .rule-toggle { display: flex; align-items: center; gap: 0.4rem; margin: 0; flex: 1 1 auto; min-width: 0; }
  .rule-title { font-weight: 500; }
  .rule-actions { display: flex; align-items: center; gap: 0.35rem; flex: 0 0 auto; }
  .rule-badge { font-size: 0.72rem; padding: 0.05rem 0.4rem; border-radius: 10px; background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }
  .rule-body { display: block; width: 100%; box-sizing: border-box; margin-top: 0.4rem;
    padding: 0.4rem; border: 1px solid #d1d5db; border-radius: 4px; font-family: ui-monospace, "Cascadia Mono", monospace; font-size: 0.82rem; line-height: 1.35; }
  .rule-preview { margin: 0.4rem 0 0; padding: 0.4rem 0.5rem; border: 1px dashed #d1d5db; border-radius: 4px;
    background: #fff; color: #374151; font-family: ui-monospace, "Cascadia Mono", monospace; font-size: 0.78rem;
    line-height: 1.35; white-space: pre-wrap; word-break: break-word; max-height: 14rem; overflow: auto; }
  .rule-foot { margin-top: 0.3rem; display: flex; gap: 0.4rem; }
  .custom-title { flex: 1 1 auto; min-width: 0; padding: 0.2rem 0.4rem; border: 1px solid #d1d5db; border-radius: 4px; background: transparent; }
  .custom-add { margin-top: 0.5rem; }
  .ghost.danger { color: #b91c1c; }


  .page { max-width: 56rem; margin: 0; padding: 1rem 1.5rem; font: 14px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  header { display: flex; align-items: baseline; gap: 1rem; }
  .back { color: #2563eb; text-decoration: none; font-size: 0.9rem; }
  h1 { margin: 0.5rem 0; }
  h2 { margin: 1rem 0 0.4rem; font-size: 1.05rem; }
  .hint { font-size: 0.85rem; color: #4b5563; }
  .err { color: #b91c1c; }
  .editor label { display: block; margin: 0.4rem 0; }
  .editor input:not([type]), .editor input[type="number"],
  .editor input[type="time"], .editor input[type="datetime-local"], .editor select {
    margin-left: 0.4rem; padding: 0.25rem 0.4rem; border: 1px solid #d1d5db; border-radius: 4px;
  }
  .editor textarea { display: block; width: 100%; box-sizing: border-box; margin-top: 0.2rem;
                     padding: 0.4rem; border: 1px solid #d1d5db; border-radius: 4px; font: inherit; }
  fieldset.trigger { border: 1px solid #e5e7eb; padding: 0.5rem 0.8rem; border-radius: 4px; margin: 0.5rem 0; }
  fieldset.trigger label { display: inline-flex; align-items: center; gap: 0.2rem; margin-right: 0.8rem; }
  fieldset.trigger button.ghost { background: #fff; color: #2563eb; border: 1px solid #93c5fd;
                                   padding: 0.2rem 0.6rem; border-radius: 4px; cursor: pointer;
                                   margin-left: 0.4rem; font-size: 0.85rem; }
  .trigger-detail { margin-top: 0.4rem; }
  fieldset.constraints .sub-hint { font-size: 0.78rem; color: #6b7280; margin: 0 0 0.5rem; }
  .constraint-row { padding: 0.3rem 0; border-top: 1px dashed #e5e7eb; }
  .constraint-row:first-of-type { border-top: 0; }
  .constraint-row label.toggle { display: inline-flex; align-items: center; gap: 0.3rem;
                                  font-weight: 500; margin-right: 0; }
  .hours-grid { display: grid; grid-template-columns: repeat(8, minmax(0, 1fr));
                gap: 0.25rem 0.4rem; margin: 0.4rem 0 0.3rem 1.2rem; }
  .wd-grid { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0.4rem 0 0.3rem 1.2rem; }
  .chip { display: inline-flex; align-items: center; gap: 0.2rem; padding: 0.1rem 0.4rem;
          border: 1px solid #e5e7eb; border-radius: 3px; font-size: 0.78rem;
          background: #f9fafb; cursor: pointer; user-select: none; }
  .chip input { margin: 0; }
  .chip-actions { margin: 0.2rem 0 0.3rem 1.2rem; }
  .auto-chat-apps .apps-picker { position: relative; margin-top: 0.3rem; }
  .apps-picker-trigger { width: 100%; min-height: 2rem; padding: 0.25rem 1.6rem 0.25rem 0.4rem;
    border: 1px solid #d1d5db; border-radius: 4px; background: #fff;
    text-align: left; cursor: pointer; position: relative; font-size: 0.82rem; }
  .apps-picker-trigger:hover { border-color: #93c5fd; }
  .apps-picker-placeholder { color: #9ca3af; }
  .apps-picker-chips { display: flex; flex-wrap: wrap; gap: 0.25rem; }
  .apps-picker-chip { display: inline-flex; align-items: center; gap: 0.25rem;
    background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 3px;
    padding: 0.05rem 0.3rem; font-size: 0.78rem; max-width: 200px; }
  .apps-picker-chip img { width: 14px; height: 14px; object-fit: contain; flex-shrink: 0; }
  .apps-picker-chip .app-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .apps-picker-chip .chip-x { background: transparent; border: 0; color: #1d4ed8;
    cursor: pointer; padding: 0 0.1rem; font-size: 0.95rem; line-height: 1; }
  .apps-picker-chip .chip-x:hover { color: #b91c1c; }
  .apps-picker-caret { position: absolute; right: 0.5rem; top: 50%;
    transform: translateY(-50%); color: #6b7280; font-size: 0.7rem; pointer-events: none; }
  .apps-picker-panel { position: absolute; left: 0; right: 0; top: calc(100% + 4px);
    background: #fff; border: 1px solid #d1d5db; border-radius: 4px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.08); z-index: 20;
    max-height: 360px; display: flex; flex-direction: column; }
  .apps-picker-search { margin: 0.4rem; padding: 0.25rem 0.4rem;
    border: 1px solid #e5e7eb; border-radius: 3px; font-size: 0.82rem; }
  .apps-picker-list { overflow-y: auto; padding: 0 0.2rem 0.2rem; flex: 1; }
  .apps-picker-row { display: flex; align-items: center; gap: 0.4rem;
    padding: 0.2rem 0.35rem; border-radius: 3px; cursor: pointer; font-size: 0.82rem; }
  .apps-picker-row:hover { background: #f3f4f6; }
  .apps-picker-row.checked { background: #eff6ff; }
  .apps-picker-row input { margin: 0; flex-shrink: 0; }
  .apps-picker-row img { width: 18px; height: 18px; object-fit: contain; flex-shrink: 0; }
  .apps-picker-row .app-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .apps-picker-empty { margin: 0.5rem 0.6rem; }
  .apps-picker-actions { margin: 0.2rem 0.3rem 0.4rem; }
  .app-icon-placeholder { display: inline-block; width: 18px; height: 18px;
    background: #e5e7eb; border-radius: 3px; flex-shrink: 0; }
  .daterange { margin: 0.4rem 0 0.3rem 1.2rem; }
  .daterange label { display: inline-block; margin-right: 0.8rem; }
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
  .type-tag { background: #ecfeff; color: #0f766e; padding: 0 0.4rem; border-radius: 3px;
              font-size: 0.75rem; margin-left: 0.4rem; font-weight: 500; }
  .instr { font-size: 0.85rem; color: #374151; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .meta { font-size: 0.72rem; color: #6b7280; margin-top: 0.15rem; }
  .ops button { padding: 0.3rem 0.6rem; background: #2563eb; color: #fff; border: 0;
                border-radius: 4px; cursor: pointer; margin-left: 0.3rem; font-size: 0.85rem; }
  .ops button.ghost { background: #fff; color: #2563eb; border: 1px solid #93c5fd; }
  .ops button.danger { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
  .empty { color: #9ca3af; font-size: 0.85rem; }
</style>
