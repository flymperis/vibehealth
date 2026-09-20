import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type DocumentKind, type ReadingSettingsResponse, type SystemStatus } from "../api";
import { useI18n } from "../i18n";
import {
  ACCEPT,
  ACCEPT_PHOTOS,
  precheck,
  useUploads,
  type Failure,
  type QueueItem,
} from "../upload";
import { Button, Dialog, Input, Select, Spinner, buttonClass, formatBytes } from "../ui";

const KINDS: DocumentKind[] = ["blood_test", "report", "prescription", "imaging", "other"];
const KIND_STORE = "vibehealth.upload.kind";
/** Long or odd file names (no spaces, very long words) must wrap instead of pushing the page wide. */
const WRAP = "[overflow-wrap:anywhere]";

interface Staged {
  key: number;
  file: File;
  problem: "type" | "size" | "empty" | null;
}

/** The "Add documents" button. The panel it opens is one for the whole app: see UploadDialog. */
export default function AddDocuments({
  label,
  variant = "primary",
  className = "",
}: {
  label?: string;
  variant?: "primary" | "ghost";
  className?: string;
}) {
  const { t } = useI18n();
  const { busy, panelOpen, setPanelOpen } = useUploads();
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button variant={variant} className={className} onClick={() => setPanelOpen(true)}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="size-4" aria-hidden="true">
          <path d="M12 5v14M5 12h14" />
        </svg>
        {label ?? t("up.add")}
      </Button>
      {busy > 0 && !panelOpen && (
        <button
          type="button"
          onClick={() => setPanelOpen(true)}
          className="text-xs font-medium text-emerald-700 underline underline-offset-2 dark:text-emerald-400"
          aria-live="polite"
        >
          {t("up.inProgress", { n: busy })}
        </button>
      )}
    </div>
  );
}

/** The panel itself, rendered once (in App): a page that swaps its "Add" buttons cannot close it. */
export function UploadDialog() {
  const { t } = useI18n();
  const { panelOpen, setPanelOpen } = useUploads();
  // Back / forward while it is open: the panel must not stay on top of another page.
  const { pathname } = useLocation();
  useEffect(() => setPanelOpen(false), [pathname, setPanelOpen]);
  return (
    <Dialog open={panelOpen} onClose={() => setPanelOpen(false)} title={t("up.title")} wide>
      <UploadPanel onClose={() => setPanelOpen(false)} />
    </Dialog>
  );
}

function Field({
  label,
  help,
  children,
}: {
  label: string;
  help?: string;
  children: (ids: { id: string; describedBy?: string }) => ReactNode;
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {children({ id, describedBy: help ? `${id}-help` : undefined })}
      {help && (
        <p id={`${id}-help`} className="text-xs muted">
          {help}
        </p>
      )}
    </div>
  );
}

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function UploadPanel({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  const { items, add, clearFinished } = useUploads();
  const { data: status, isError } = useQuery({
    queryKey: ["status"],
    queryFn: () => api.get<SystemStatus>("/status"),
  });
  const { data: reading } = useQuery({
    queryKey: ["reading-settings"],
    queryFn: () => api.get<ReadingSettingsResponse>("/reading/settings"),
  });
  const readingOn = reading?.settings.enabled ?? true;

  const [staged, setStaged] = useState<Staged[]>([]);
  const [kind, setKind] = useState<DocumentKind>(() => {
    const saved = localStorage.getItem(KIND_STORE) as DocumentKind | null;
    // Most people add lab results, which is what the reading is for.
    return saved && KINDS.includes(saved) ? saved : "blood_test";
  });
  const [date, setDate] = useState("");
  const [title, setTitle] = useState("");
  const [readNow, setReadNow] = useState(true);
  const [dragging, setDragging] = useState(false);
  const nextKey = useRef(1);
  const picker = useRef<HTMLInputElement>(null);
  const camera = useRef<HTMLInputElement>(null);

  if (isError || !status) {
    return isError ? <p className="text-sm text-red-600 dark:text-red-400">{t("up.off.unknown")}</p> : <Spinner label={t("common.loading")} />;
  }

  const { enabled, max_mb: maxMb, password_required: passwordRequired } = status.uploads;
  const available = enabled && !passwordRequired;
  const valid = staged.filter((s) => !s.problem);

  const stage = (list: FileList | File[] | null) => {
    if (!list || !available) return;
    const files = Array.from(list);
    setStaged((current) => {
      const known = new Set(current.map((s) => `${s.file.name}|${s.file.size}|${s.file.lastModified}`));
      const fresh: Staged[] = [];
      for (const file of files) {
        const id = `${file.name}|${file.size}|${file.lastModified}`;
        if (known.has(id)) continue;
        known.add(id);
        fresh.push({ key: nextKey.current++, file, problem: precheck(file, maxMb) });
      }
      return [...current, ...fresh];
    });
  };

  const send = () => {
    if (!valid.length) return;
    localStorage.setItem(KIND_STORE, kind);
    const single = staged.length === 1;
    add(
      valid.map((s) => s.file),
      { kind, title: single ? title : "", docDate: date, readNow: readNow && readingOn },
      maxMb,
    );
    setStaged((current) => current.filter((s) => s.problem));
    setTitle("");
  };

  const problemText = (s: Staged) =>
    s.problem === "type"
      ? t("up.problem.type")
      : s.problem === "size"
        ? t("up.problem.size", { max: maxMb })
        : t("up.problem.empty");

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm muted">{t("up.intro")}</p>

      {passwordRequired && (
        <Unavailable text={t("up.off.password")}>
          <Link to="/settings#set-password" data-autofocus onClick={onClose} className={buttonClass("primary")}>
            {t("pw.titleFirst")}
          </Link>
        </Unavailable>
      )}
      {!enabled && (
        <Unavailable text={t("up.off.disabled")}>
          <Link to="/settings#uploads" data-autofocus={!passwordRequired || undefined} onClick={onClose} className={buttonClass(passwordRequired ? "ghost" : "primary")}>
            {t("up.off.openSettings")}
          </Link>
        </Unavailable>
      )}

      {available && (
        <>
          {/* Two hidden inputs behind the visible buttons: any files, and the phone's camera. */}
          <input
            ref={picker}
            type="file"
            multiple
            accept={ACCEPT}
            className="hidden"
            tabIndex={-1}
            aria-hidden="true"
            onChange={(e) => {
              stage(e.target.files);
              e.target.value = ""; // the same file can be picked again
            }}
          />
          <input
            ref={camera}
            type="file"
            accept={ACCEPT_PHOTOS}
            capture="environment"
            className="hidden"
            tabIndex={-1}
            aria-hidden="true"
            onChange={(e) => {
              stage(e.target.files);
              e.target.value = "";
            }}
          />
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              stage(e.dataTransfer.files);
            }}
            className={`flex flex-col items-center gap-3 rounded-2xl border-2 border-dashed p-4 text-center transition ${
              dragging ? "border-emerald-500 bg-emerald-500/10" : ""
            }`}
            style={dragging ? undefined : { borderColor: "var(--border)" }}
          >
            <p className="hidden text-sm font-medium sm:block">{t("up.dropTitle")}</p>
            <div className="flex flex-wrap items-center justify-center gap-2">
              <Button type="button" data-autofocus onClick={() => picker.current?.click()}>
                {t("up.choose")}
              </Button>
              <Button type="button" variant="ghost" className="sm:hidden" onClick={() => camera.current?.click()}>
                {t("up.camera")}
              </Button>
            </div>
            <p className="text-xs muted">{t("up.formats", { max: maxMb })}</p>
          </div>

          {staged.length > 0 && (
            <section className="flex flex-col gap-3" aria-labelledby="staged-heading">
              <h3 id="staged-heading" className="text-sm font-semibold">
                {t("up.selected")}
              </h3>
              <ul className="flex flex-col gap-2">
                {staged.map((s) => (
                  <li key={s.key} className="surface flex items-start gap-2 rounded-xl p-2 pl-3">
                    <div className="min-w-0 flex-1 py-1">
                      <p className={`text-sm font-medium ${WRAP}`}>{s.file.name}</p>
                      <p className="text-xs muted">{formatBytes(s.file.size)}</p>
                      {s.problem && (
                        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
                          {problemText(s)}
                        </p>
                      )}
                    </div>
                    <button
                      type="button"
                      aria-label={t("up.removeFile", { name: s.file.name })}
                      onClick={() => setStaged((current) => current.filter((x) => x.key !== s.key))}
                      className="inline-flex size-11 shrink-0 items-center justify-center rounded-xl text-lg muted hover:bg-black/5 dark:hover:bg-white/5"
                    >
                      <span aria-hidden="true">×</span>
                    </button>
                  </li>
                ))}
              </ul>

              {valid.length > 0 && (
                <>
                  <Field label={t("up.kind")} help={t("up.kindHelp")}>
                    {({ id, describedBy }) => (
                      <Select id={id} aria-describedby={describedBy} value={kind} onChange={(e) => setKind(e.target.value as DocumentKind)}>
                        {KINDS.map((k) => (
                          <option key={k} value={k}>
                            {t(`kind.${k}` as "kind.other")}
                          </option>
                        ))}
                      </Select>
                    )}
                  </Field>
                  <Field label={t("up.date")} help={t("up.dateHelp")}>
                    {({ id, describedBy }) => (
                      <Input
                        id={id}
                        aria-describedby={describedBy}
                        type="date"
                        min="1900-01-01"
                        max={todayIso()}
                        value={date}
                        onChange={(e) => setDate(e.target.value)}
                        className="sm:max-w-56"
                      />
                    )}
                  </Field>
                  {staged.length === 1 && (
                    <Field label={t("up.titleField")} help={t("up.titleHelp")}>
                      {({ id, describedBy }) => (
                        <Input
                          id={id}
                          aria-describedby={describedBy}
                          value={title}
                          maxLength={200}
                          onChange={(e) => setTitle(e.target.value)}
                        />
                      )}
                    </Field>
                  )}
                  <label className="flex items-start gap-3">
                    <input
                      type="checkbox"
                      className="mt-1 size-5 shrink-0 accent-emerald-600"
                      checked={readNow && readingOn}
                      disabled={!readingOn}
                      onChange={(e) => setReadNow(e.target.checked)}
                    />
                    <span className="flex flex-col gap-0.5">
                      <span className="text-sm font-medium">{t("up.readNow")}</span>
                      <span className="text-xs muted">{readingOn ? t("up.readNowHelp") : t("up.readOff")}</span>
                    </span>
                  </label>
                  <div>
                    <Button type="button" disabled={!valid.length} onClick={send} className="w-full sm:w-auto">
                      {valid.length === 1 ? t("up.start.one") : t("up.start.many", { n: valid.length })}
                    </Button>
                  </div>
                </>
              )}
            </section>
          )}
        </>
      )}

      {items.length > 0 && (
        <section className="flex flex-col gap-2" aria-labelledby="queue-heading">
          <div className="flex items-center justify-between gap-2">
            <h3 id="queue-heading" className="text-sm font-semibold">
              {t("up.queue")}
            </h3>
            {items.some((i) => i.state === "done") && (
              <button type="button" onClick={clearFinished} className="min-h-9 px-1 text-xs font-medium text-emerald-700 underline underline-offset-2 dark:text-emerald-400">
                {t("up.clearDone")}
              </button>
            )}
          </div>
          <ul className="flex flex-col gap-2">
            {items.map((item) => (
              <UploadRow key={item.id} item={item} maxMb={maxMb} onNavigate={onClose} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function Unavailable({ text, children }: { text: string; children: ReactNode }) {
  return (
    <div
      role="note"
      className="flex flex-col items-start gap-3 rounded-xl bg-amber-500/10 p-3 text-sm text-amber-900 dark:text-amber-200"
    >
      <p>{text}</p>
      {children}
    </div>
  );
}

function UploadRow({ item, maxMb, onNavigate }: { item: QueueItem; maxMb: number; onNavigate: () => void }) {
  const { t } = useI18n();
  const { retry, remove } = useUploads();
  const { state, progress, failure, file, result } = item;
  const sent = state === "uploading" && progress >= 1;
  const pct = Math.round(progress * 100);

  const linkClass = buttonClass("ghost", "min-h-9 px-3");
  const small = "min-h-9 px-3";
  const canRetry = failure && failure.kind !== "duplicate" && failure.kind !== "type" && failure.kind !== "invalid";

  return (
    <li className="surface flex flex-col gap-2 rounded-xl p-3">
      <div className="flex min-w-0 flex-col gap-0.5">
        <p className={`text-sm font-medium ${WRAP}`}>{file.name}</p>
        <p className="text-xs muted">{formatBytes(file.size)}</p>
      </div>

      {(state === "waiting" || state === "uploading") && (
        <div className="flex flex-col gap-1">
          <div
            role="progressbar"
            aria-label={file.name}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={state === "waiting" || sent ? undefined : pct}
            className="h-2 overflow-hidden rounded-full bg-slate-500/15"
          >
            <div
              className={`h-full rounded-full bg-emerald-500 transition-all ${sent ? "animate-pulse" : ""}`}
              style={{ width: `${state === "waiting" ? 0 : Math.max(3, pct)}%` }}
            />
          </div>
          <p className="text-xs muted" aria-live="polite">
            {state === "waiting" ? t("up.state.waiting") : sent ? t("up.state.processing") : t("up.state.uploading", { pct })}
          </p>
        </div>
      )}

      {state === "done" && result && (
        <p className="text-xs text-emerald-700 dark:text-emerald-400" aria-live="polite">
          {t("up.state.done")} · {result.read_queued ? t("up.readStarted") : t("up.notRead")}
        </p>
      )}

      {state === "error" && failure && (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          {failureText(t, failure, maxMb)}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        {state === "done" && result && (
          <Link to={`/documents/${result.document.id}`} onClick={onNavigate} className={linkClass}>
            {t("up.open")}
          </Link>
        )}
        {failure?.kind === "duplicate" && failure.documentId != null && (
          <Link to={`/documents/${failure.documentId}`} onClick={onNavigate} className={linkClass}>
            {t("up.err.viewExisting")}
          </Link>
        )}
        {failure?.kind === "password" && (
          <Link to="/settings#set-password" onClick={onNavigate} className={buttonClass("primary", small)}>
            {t("pw.titleFirst")}
          </Link>
        )}
        {failure?.kind === "disabled" && (
          <Link to="/settings#uploads" onClick={onNavigate} className={buttonClass("primary", small)}>
            {t("up.off.openSettings")}
          </Link>
        )}
        {state === "error" && canRetry && (
          <Button type="button" variant="ghost" className={small} onClick={() => retry(item.id)}>
            {t("up.retry")}
          </Button>
        )}
        {state !== "done" && (
          <Button type="button" variant="ghost" className={small} onClick={() => remove(item.id)}>
            {state === "error" ? t("up.remove") : t("common.cancel")}
          </Button>
        )}
      </div>
    </li>
  );
}

function failureText(t: ReturnType<typeof useI18n>["t"], failure: Failure, maxMb: number): string {
  switch (failure.kind) {
    case "duplicate":
      return t("up.err.duplicate");
    case "password":
      return t("up.err.password");
    case "disabled":
      return t("up.err.disabled");
    case "forbidden":
      return t("up.err.forbidden", { detail: failure.detail });
    case "tooLarge":
      return t("up.err.tooLarge", { max: maxMb });
    case "type":
      return t("up.err.type");
    case "invalid":
      return t("up.err.invalid", { detail: failure.detail });
    case "session":
      return t("up.err.session");
    case "rate":
      return t("up.err.rate");
    case "network":
      return t("up.err.network");
    case "server":
      return t("up.err.server", { status: failure.status });
  }
}
