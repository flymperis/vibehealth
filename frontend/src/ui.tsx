/** Small shared UI pieces, sized for thumbs first. */
import { createPortal } from "react-dom";
import { useEffect, useRef, type ButtonHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from "react";
import { Link } from "react-router-dom";
import { useI18n } from "./i18n";

export function Card({
  children,
  className = "",
  id,
}: {
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <div id={id} className={`surface rounded-2xl shadow-sm ${className}`}>
      {children}
    </div>
  );
}

type Variant = "primary" | "ghost" | "danger";

/** Button look, also for links that act as buttons. */
export function buttonClass(variant: Variant = "primary", className = "") {
  const styles = {
    primary: "bg-emerald-600 text-white hover:bg-emerald-700 active:bg-emerald-800",
    ghost: "surface hover:bg-black/5 dark:hover:bg-white/5",
    danger: "bg-red-600/10 text-red-600 hover:bg-red-600/20",
  }[variant];
  return `inline-flex min-h-11 items-center justify-center gap-2 rounded-xl px-4 text-sm
        font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${styles} ${className}`;
}

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return <button className={buttonClass(variant, className)} {...props} />;
}

export function Select({ className = "", ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={`surface min-h-11 rounded-xl px-3 text-sm outline-none
        focus:ring-2 focus:ring-emerald-500/40 ${className}`}
      {...props}
    />
  );
}

export function Input({
  className = "",
  ...props
}: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={`surface min-h-11 w-full rounded-xl px-3 text-sm outline-none
        focus:ring-2 focus:ring-emerald-500/40 ${className}`}
      {...props}
    />
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 py-10 muted text-sm">
      <span className="size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
      {label}
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <Card className="p-8 text-center text-sm muted">{children}</Card>;
}

/** Small linkable stat card: label, count and an optional last-date line. */
export function StatCard({
  to,
  label,
  count,
  sub,
}: {
  to: string;
  label: string;
  count: number;
  sub?: string;
}) {
  return (
    <Link to={to} className="block">
      <Card className="flex flex-col gap-1 p-3 transition hover:bg-black/5 dark:hover:bg-white/5">
        <span className="text-xs muted">{label}</span>
        <span className="text-2xl font-semibold tracking-tight">{count}</span>
        {sub && <span className="text-xs muted">{sub}</span>}
      </Card>
    </Link>
  );
}

/** Pill-shaped toggle, used for the kind picker. */
export function Chip({
  active,
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  return (
    <button
      className={`inline-flex min-h-9 shrink-0 items-center rounded-full px-3 text-sm font-medium
        transition ${active ? "bg-emerald-600 text-white" : "surface hover:bg-black/5 dark:hover:bg-white/5"} ${className}`}
      {...props}
    />
  );
}

export function formatDate(value: string | null | undefined) {
  if (!value) return "—";
  const [year, month, day] = value.slice(0, 10).split("-");
  return `${day}/${month}/${year}`;
}

/**
 * A modal dialog on the native <dialog>: the page behind is inert, Tab stays inside, Escape closes it and
 * focus goes back to what opened it. On phones it is a sheet at the bottom. Children are only mounted while
 * it is open, so a form starts fresh every time.
 */
export function Dialog({
  open,
  onClose,
  title,
  children,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  wide?: boolean;
}) {
  const { t } = useI18n();
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useRef(`dialog-title-${Math.random().toString(36).slice(2, 8)}`).current;

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      // the first thing worth doing in the dialog, instead of its close button
      dialog.querySelector<HTMLElement>("[data-autofocus]")?.focus({ preventScroll: true });
    }
    if (!open && dialog.open) dialog.close();
  }, [open]);

  // Portalled to <body>: a dialog opened from inside a card must not inherit its text alignment, colour or opacity.
  return createPortal(
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onClose={onClose}
      // a click on the dimmed area (the dialog element itself, not its content) closes it
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) e.currentTarget.close();
      }}
      className={`surface fixed inset-0 m-auto max-h-[calc(100dvh-1.5rem)] w-[calc(100%-1.5rem)] overflow-y-auto
        overscroll-contain rounded-2xl p-0 text-left text-sm text-[var(--text)] shadow-xl backdrop:bg-black/50
        max-sm:mb-0 max-sm:w-full max-sm:max-w-none max-sm:rounded-b-none ${wide ? "max-w-xl" : "max-w-md"}`}
    >
      {open && (
        <div className="flex flex-col gap-4 p-4 sm:p-5">
          <div className="flex items-start justify-between gap-3">
            <h2 id={titleId} className="text-base font-semibold tracking-tight break-words">
              {title}
            </h2>
            <button
              type="button"
              onClick={onClose}
              aria-label={t("common.close")}
              className="-mr-2 -mt-1 inline-flex size-11 shrink-0 items-center justify-center rounded-xl text-xl
                leading-none muted hover:bg-black/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60 dark:hover:bg-white/5"
            >
              <span aria-hidden="true">×</span>
            </button>
          </div>
          {children}
        </div>
      )}
    </dialog>,
    document.body,
  );
}

/** Scroll to the "Set a password" card on the Settings page and put the cursor in it. */
export function goToPasswordCard() {
  const el = document.getElementById("set-password");
  if (!el) return;
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "center" });
  el.querySelector<HTMLInputElement>("input")?.focus({ preventScroll: true });
}

/** A small "label: value"-free file size, 1.4 MB / 320 KB. */
export function formatBytes(bytes: number | null | undefined) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}
