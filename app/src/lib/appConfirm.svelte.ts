// Tiny replacement for window.confirm() that respects the app's own i18n /
// styling instead of falling through to the OS-locale native dialog (which
// shows "tauri.localhost 显示 / 确定 / 取消" on a Chinese-locale Windows even
// when the app is set to English).
//
// Usage: `if (!(await appConfirm("Delete this?"))) return;`
// The Confirm modal is mounted once in +layout.svelte.

type Pending = {
  message: string;
  title?: string;
  okLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  resolve: (v: boolean) => void;
};

export const confirmState = $state<{ pending: Pending | null }>({ pending: null });

export function appConfirm(
  message: string,
  opts: { title?: string; okLabel?: string; cancelLabel?: string; danger?: boolean } = {}
): Promise<boolean> {
  // If something is already pending, resolve it false (shouldn't normally happen).
  if (confirmState.pending) confirmState.pending.resolve(false);
  return new Promise<boolean>((resolve) => {
    confirmState.pending = {
      message,
      title: opts.title,
      okLabel: opts.okLabel,
      cancelLabel: opts.cancelLabel,
      danger: opts.danger,
      resolve,
    };
  });
}

export function resolveConfirm(v: boolean): void {
  const p = confirmState.pending;
  confirmState.pending = null;
  if (p) p.resolve(v);
}
