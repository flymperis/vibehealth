import { useEffect, useId, useState, type ReactNode } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  api,
  type AuthStatus,
  type DocumentKind,
  type MatchMode,
  type PaperlessDiscovery,
  type PaperlessPreview,
  type PaperlessSettingsResponse,
  type PaperlessTestResult,
  type PaperlessValues,
  type SettingSource,
  type SystemStatus,
} from "../api";
import { useI18n } from "../i18n";
import { Button, Card, Input, Select, goToPasswordCard } from "../ui";
import { formatWait } from "./ChangePasswordCard";
import { Combobox, TagPicker } from "./PaperlessPickers";

type Key = Parameters<ReturnType<typeof useI18n>["t"]>[0];
type Errors = Record<string, string>;
type ResetKey = "url" | "public_url" | "document_type" | "tags" | "sync_interval_minutes";
type TokenMode = "keep" | "replace" | "clear";

// --- constants ------------------------------------------------------------------

const KINDS: DocumentKind[] = ["blood_test", "report", "prescription", "imaging", "other"];
const KIND_LABEL: Record<DocumentKind, Key> = {
  blood_test: "kind.blood_test",
  report: "kind.report",
  prescription: "kind.prescription",
  imaging: "kind.imaging",
  other: "pl.kind.other",
};
/** What the backend maps by default (tag names are matched without regard to case). */
const DEFAULT_KINDS: Record<string, DocumentKind> = {
  "blood test": "blood_test",
  "medical report": "report",
  prescription: "prescription",
  imaging: "imaging",
};
const MATCHES: MatchMode[] = ["type_or_tags", "type_only", "tags_only"];
const MATCH_LABEL: Record<MatchMode, Key> = {
  type_or_tags: "pl.match.type_or_tags",
  type_only: "pl.match.type_only",
  tags_only: "pl.match.tags_only",
};
const SELECTION_TEXT: Record<MatchMode, Key> = {
  type_or_tags: "pl.sel.type_or_tags",
  type_only: "pl.sel.type_only",
  tags_only: "pl.sel.tags_only",
};
const INTERVALS: { minutes: number; label: Key }[] = [
  { minutes: 0, label: "pl.interval.0" },
  { minutes: 30, label: "pl.interval.30" },
  { minutes: 60, label: "pl.interval.60" },
  { minutes: 240, label: "pl.interval.240" },
  { minutes: 720, label: "pl.interval.720" },
  { minutes: 1440, label: "pl.interval.1440" },
];
const TEST_ERROR: Record<string, Key> = {
  connection: "pl.err.connection",
  unauthorized: "pl.err.unauthorized",
  not_found: "pl.err.not_found",
  tls: "pl.err.tls",
  timeout: "pl.err.timeout",
  other: "pl.err.other",
};
/** Where the first message of a failed save is shown, top to bottom. */
const FOCUS_ORDER: [string, string][] = [
  ["current_password", "password"],
  ["url", "url"],
  ["public_url", "publicUrl"],
  ["token", "token"],
  ["match", "match"],
  ["document_type", "docType"],
  ["tags", "tags"],
  ["sync_interval_minutes", "interval"],
];

// --- form model -----------------------------------------------------------------

interface Form {
  enabled: boolean;
  url: string;
  publicUrl: string;
  documentType: string;
  tags: string[];
  match: MatchMode;
  interval: number;
}

const fromValues = (v: PaperlessValues): Form => ({
  enabled: v.enabled,
  url: v.url,
  publicUrl: v.public_url,
  documentType: v.document_type,
  tags: v.tags,
  match: v.match,
  interval: v.sync_interval_minutes,
});

const lower = (s: string) => s.trim().toLowerCase();
const trimUrl = (s: string) => s.trim().replace(/\/+$/, "");
const sameList = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i]);

function findKey(map: Record<string, DocumentKind>, tag: string): string | undefined {
  return Object.keys(map).find((k) => lower(k) === lower(tag));
}

/** The kind a tag files under: the person's pick, else the saved mapping, else the default. */
function kindOf(saved: Record<string, DocumentKind>, edits: Record<string, DocumentKind>, tag: string): DocumentKind {
  const key = findKey(saved, tag);
  return edits[tag] ?? (key ? saved[key] : undefined) ?? DEFAULT_KINDS[lower(tag)] ?? "other";
}

/** The saved mapping with the chosen tags filled in. Entries for other tags stay as they are. */
function mergeKinds(
  saved: Record<string, DocumentKind>,
  edits: Record<string, DocumentKind>,
  tags: string[],
): Record<string, DocumentKind> {
  const out = { ...saved };
  for (const tag of tags) {
    const key = findKey(out, tag);
    const picked = edits[tag];
    if (key) {
      if (picked) out[key] = picked;
    } else {
      const kind = picked ?? DEFAULT_KINDS[lower(tag)];
      // "Other" is what an unmapped tag gets anyway: only store it to override a default
      if (kind && (kind !== "other" || DEFAULT_KINDS[lower(tag)])) out[tag] = kind;
    }
  }
  return out;
}

const sameKinds = (a: Record<string, DocumentKind>, b: Record<string, DocumentKind>) => {
  const ka = Object.keys(a);
  return ka.length === Object.keys(b).length && ka.every((k) => b[k] === a[k]);
};

function selectionEmpty(f: Pick<Form, "match" | "documentType" | "tags">) {
  const hasType = !!f.documentType.trim();
  const hasTags = f.tags.some((x) => x.trim());
  if (f.match === "type_only") return !hasType;
  if (f.match === "tags_only") return !hasTags;
  return !hasType && !hasTags;
}

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const h = setTimeout(() => setV(value), ms);
    return () => clearTimeout(h);
  }, [value, ms]);
  return v;
}

function useDiscover(conn: string, q: string, enabled: boolean) {
  return useQuery({
    // `conn` names the saved connection, so a new address or token loads a fresh list
    queryKey: ["paperless-discover", conn, q],
    queryFn: () => api.post<PaperlessDiscovery>("/paperless/discover", { q, limit: 200 }),
    enabled,
    retry: false, // 409 / 429 / 502 are answers, not glitches; the calls share a small allowance
    staleTime: 60_000,
    gcTime: 120_000,
  });
}

const whenText = (iso: string | null | undefined) => (iso ? iso.replace("T", " ").slice(0, 16) : "—");

// --- small pieces ---------------------------------------------------------------

function TextButton({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      className="inline-flex min-h-8 items-center rounded px-1 text-xs font-medium text-emerald-700 underline
        underline-offset-2 hover:text-emerald-800 focus-visible:outline-none focus-visible:ring-2
        focus-visible:ring-emerald-500/60 disabled:opacity-50 dark:text-emerald-400 dark:hover:text-emerald-300"
      {...props}
    >
      {children}
    </button>
  );
}

function Field({
  id,
  label,
  help,
  error,
  note,
  children,
}: {
  id: string;
  label: Key;
  help?: Key;
  error?: string;
  note?: ReactNode;
  children: ReactNode;
}) {
  const { t } = useI18n();
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {t(label)}
      </label>
      {children}
      {help && (
        <p id={`${id}-help`} className="text-xs muted">
          {t(help)}
        </p>
      )}
      {note}
      {error && (
        <p id={`${id}-err`} role="alert" className="text-xs text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
    </div>
  );
}

const describe = (id: string, help: boolean, error?: string) =>
  [help ? `${id}-help` : "", error ? `${id}-err` : ""].filter(Boolean).join(" ") || undefined;

function SourceNote({
  source,
  resettable,
  pending,
  onReset,
  onUndo,
}: {
  source: SettingSource;
  resettable: boolean;
  pending: boolean;
  onReset: () => void;
  onUndo: () => void;
}) {
  const { t } = useI18n();
  if (pending) {
    return (
      <p className="text-xs muted">
        {t("pl.src.pending")} <TextButton onClick={onUndo}>{t("pl.undo")}</TextButton>
      </p>
    );
  }
  if (source === "env") return <p className="text-xs muted">{t("pl.src.env")}</p>;
  if (source === "default") return <p className="text-xs muted">{t("pl.src.default")}</p>;
  if (!resettable) return null;
  return (
    <p className="text-xs muted">
      {t("pl.src.app")} · <TextButton onClick={onReset}>{t("pl.src.reset")}</TextButton>
    </p>
  );
}

function Toggle({
  id,
  label,
  help,
  checked,
  onChange,
}: {
  id: string;
  label: Key;
  help: Key;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  const { t } = useI18n();
  return (
    <div className="flex items-start gap-3">
      <input
        id={id}
        type="checkbox"
        className="mt-0.5 size-5 shrink-0 accent-emerald-600"
        aria-describedby={`${id}-help`}
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <div className="flex flex-col gap-0.5">
        <label htmlFor={id} className="text-sm font-medium">
          {t(label)}
        </label>
        <p id={`${id}-help`} className="text-xs muted">
          {t(help)}
        </p>
      </div>
    </div>
  );
}

function SubHeading({ children }: { children: ReactNode }) {
  return <h3 className="border-t pt-4 text-sm font-semibold" style={{ borderColor: "var(--border)" }}>{children}</h3>;
}

// --- the card -------------------------------------------------------------------

/**
 * `embedded` (the setup guide): no card of its own, no sync block. `currentPassword`: the password the
 * person just set, kept in memory by the guide, so the address fields need no password box. It is only
 * ever sent along with the request, never stored.
 */
export default function PaperlessCard({
  status,
  embedded = false,
  currentPassword,
}: {
  status: SystemStatus | undefined;
  embedded?: boolean;
  currentPassword?: string;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const uid = useId();
  const id = (name: string) => `${uid}-${name}`;

  const { data } = useQuery({
    queryKey: ["paperless-settings"],
    queryFn: () => api.get<PaperlessSettingsResponse>("/settings/paperless"),
  });
  const { data: auth } = useQuery({
    queryKey: ["auth"],
    queryFn: () => api.get<AuthStatus>("/auth/status"),
  });
  const hasPassword = !!auth?.password_set;
  const urlLocked = !!auth && !hasPassword; // open mode: addresses come from the environment only

  const [form, setForm] = useState<Form | null>(null);
  const [resets, setResets] = useState<Set<ResetKey>>(new Set());
  const [tokenMode, setTokenMode] = useState<TokenMode>("keep");
  const [tokenValue, setTokenValue] = useState("");
  const [kindEdits, setKindEdits] = useState<Record<string, DocumentKind>>({});
  const [typedConfirmation, setConfirmation] = useState("");
  const confirmation = currentPassword ?? typedConfirmation;
  const [errors, setErrors] = useState<Errors>({});
  const [saved, setSaved] = useState(false);
  const [testNote, setTestNote] = useState("");
  const [requested, setRequested] = useState(false);
  const [typeQuery, setTypeQuery] = useState("");
  const [tagQuery, setTagQuery] = useState("");

  const v = data?.values;
  // Both must agree: the saved settings change first, the status a moment later.
  const configured = !!status?.paperless.configured && !!v?.url && !!v?.token_set;
  const conn = v ? `${v.url}|${v.token_set ? v.token_last4 : ""}` : "";

  // --- what the user changed -----------------------------------------------------
  const tokenTyped = tokenValue.trim();
  const changes = (() => {
    const body: Record<string, unknown> = {};
    let tokenRequired = false;
    if (!form || !v) return { body, tokenRequired };
    if (form.enabled !== v.enabled) body.enabled = form.enabled;
    if (resets.has("url")) body.url = null;
    else if (!urlLocked && trimUrl(form.url) !== trimUrl(v.url)) body.url = form.url.trim();
    if (resets.has("public_url")) body.public_url = null;
    else if (!urlLocked && trimUrl(form.publicUrl) !== trimUrl(v.public_url)) body.public_url = form.publicUrl.trim();
    if (resets.has("document_type")) body.document_type = null;
    else if (form.documentType.trim() !== v.document_type) body.document_type = form.documentType.trim();
    if (resets.has("tags")) body.tags = null;
    else if (!sameList(form.tags, v.tags)) body.tags = form.tags;
    if (form.match !== v.match) body.match = form.match;
    if (resets.has("sync_interval_minutes")) body.sync_interval_minutes = null;
    else if (form.interval !== v.sync_interval_minutes) body.sync_interval_minutes = form.interval;
    const kinds = mergeKinds(v.kind_map, kindEdits, form.tags);
    if (!sameKinds(kinds, v.kind_map)) body.kind_map = kinds;
    // A saved token is only ever sent to the address it was saved for.
    tokenRequired = typeof body.url === "string" && v.token_set && !(tokenMode === "replace" && tokenTyped);
    const tokenInputShown = !v.token_set || tokenMode === "replace" || tokenRequired;
    if (tokenMode === "clear") body.token = "";
    else if (tokenInputShown && tokenTyped) body.token = tokenTyped;
    return { body, tokenRequired };
  })();
  const dirty = Object.keys(changes.body).length > 0;
  const addressTouched = "url" in changes.body || "public_url" in changes.body;
  const showTokenInput = !!v && (!v.token_set || tokenMode === "replace" || changes.tokenRequired);

  // --- Paperless lookups (a small shared allowance: few, debounced, no retries) ---------
  const base = useDiscover(conn, "", configured);
  const typeSearch = useDiscover(
    conn,
    useDebounced(typeQuery.trim().slice(0, 100), 400),
    configured && !!base.data?.truncated.document_types && !!typeQuery.trim(),
  );
  const tagSearch = useDiscover(
    conn,
    useDebounced(tagQuery.trim().slice(0, 100), 400),
    configured && !!base.data?.truncated.tags && !!tagQuery.trim(),
  );

  const selectionPending = resets.has("document_type") || resets.has("tags");
  const empty = form ? selectionEmpty(form) : true;
  const wantedKey = JSON.stringify(form ? { m: form.match, d: form.documentType.trim(), t: form.tags } : null);
  const previewKey = useDebounced(wantedKey, 700);
  const preview = useQuery({
    queryKey: ["paperless-count", conn, previewKey],
    queryFn: () => {
      const p = JSON.parse(previewKey) as { m: MatchMode; d: string; t: string[] };
      return api.post<PaperlessPreview>("/paperless/preview-count", { match: p.m, document_type: p.d, tags: p.t });
    },
    enabled: configured && !!form && !empty && !selectionPending && previewKey !== "null",
    retry: false,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  });

  // --- mutations -------------------------------------------------------------------
  const load = (r: PaperlessSettingsResponse) => {
    setForm(fromValues(r.values));
    setResets(new Set());
    setTokenMode("keep");
    setTokenValue("");
    setKindEdits({});
    setConfirmation("");
    setErrors({});
    setTestNote("");
  };

  useEffect(() => {
    if (data && !form) load(data);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  const test = useMutation({
    mutationFn: (body?: { url?: string; token?: string; current_password?: string }) => api.post<PaperlessTestResult>("/paperless/test", body),
  });

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.put<PaperlessSettingsResponse>("/settings/paperless", body),
    onSuccess: (r) => {
      queryClient.setQueryData(["paperless-settings"], r);
      load(r);
      setSaved(true);
      test.reset();
      // The lookups (types, tags, count) are keyed by the saved connection: a change to it
      // loads them afresh once the status says Paperless is connected.
      queryClient.invalidateQueries({ queryKey: ["status"] });
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      // the sync loop picks the change up a moment later
      setTimeout(() => queryClient.invalidateQueries({ queryKey: ["status"] }), 1500);
    },
    onError: (e: Error) => {
      const errs: Errors = {};
      if (e instanceof ApiError && e.fields.length) {
        for (const f of e.fields) {
          const key = f.field.split(".")[0] || "_";
          if (errs[key]) continue;
          if (key === "current_password") errs[key] = /wrong/i.test(f.message) ? t("pw.wrongCurrent") : t("pl.needPassword");
          else if (key === "selection" && form) errs[key] = t(SELECTION_TEXT[form.match]);
          else errs[key] = f.message;
        }
      } else if (e instanceof ApiError && e.status === 403) {
        errs._ = t("pl.openRefused");
      } else if (e instanceof ApiError && e.status === 429) {
        errs._ = t("login.locked", { wait: formatWait(e.retryAfter ?? 30, t) });
      } else {
        errs._ = e.message || t("common.error");
      }
      setSaved(false);
      setErrors(errs);
      if (errs.token && v?.token_set) setTokenMode("replace");
      const first = FOCUS_ORDER.find(([field]) => errs[field]);
      if (first) setTimeout(() => document.getElementById(id(first[1]))?.focus(), 0);
    },
  });

  const sync = useMutation({
    mutationFn: () => api.post<{ ok: boolean; queued: boolean; reason: string | null }>("/sync"),
    onSuccess: (r) => {
      queryClient.invalidateQueries({ queryKey: ["status"] });
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      if (r.queued) {
        setRequested(true);
        setTimeout(() => setRequested(false), 4000);
      }
    },
  });

  if (!form || !v || !data || !auth) return null;

  // --- helpers that need the form ---------------------------------------------------
  const edit = (patch: Partial<Form>, clear: string[] = []) => {
    setForm({ ...form, ...patch });
    setSaved(false);
    setErrors((cur) => {
      const next = { ...cur };
      delete next._;
      delete next.selection;
      for (const key of clear) delete next[key];
      return next;
    });
  };
  const setReset = (key: ResetKey, on: boolean) => {
    setResets((cur) => {
      const next = new Set(cur);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
    setSaved(false);
  };
  const discard = () => {
    load(data);
    setSaved(false);
  };
  const noteFor = (field: ResetKey, source: SettingSource, locked = false) =>
    locked && source === "env" ? (
      <p className="text-xs muted">{t("pl.tokenEnv")}</p> // it cannot be overridden here
    ) : (
    <SourceNote
      source={source}
      resettable={!locked && source === "app"}
      pending={resets.has(field)}
      onReset={() => setReset(field, true)}
      onUndo={() => setReset(field, false)}
    />
    );
  const invalid = (key: string) => (errors[key] ? "border-red-500!" : "");

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const problems: Errors = {};
    if (changes.tokenRequired) problems.token = t("pl.tokenRequired");
    if (addressTouched && hasPassword && !confirmation) problems.current_password = t("pl.needPassword");
    if (Object.keys(problems).length) {
      setErrors(problems);
      const first = FOCUS_ORDER.find(([field]) => problems[field]);
      if (first) document.getElementById(id(first[1]))?.focus();
      return;
    }
    setErrors({});
    save.mutate({ ...changes.body, ...(addressTouched && hasPassword ? { current_password: confirmation } : {}) });
  };

  const runTest = () => {
    setTestNote("");
    const typedToken = showTokenInput && tokenMode !== "clear" ? tokenTyped : "";
    const typedUrl = typeof changes.body.url === "string" ? changes.body.url : "";
    if (!hasPassword && typedToken) return setTestNote(t("pl.test.needSave")); // open mode: only the saved settings
    const body: { url?: string; token?: string; current_password?: string } = {};
    if (hasPassword) {
      if (typedUrl) body.url = typedUrl;
      if (typedToken) body.token = typedToken;
      if (body.url && !body.token) return setTestNote(t("pl.test.needToken"));
      // trying another address needs the password, like saving it
      if (body.url) {
        if (!confirmation) return setTestNote(t("pl.needPassword"));
        body.current_password = confirmation;
      }
    }
    test.mutate(Object.keys(body).length ? body : undefined);
  };
  const testsTyped = hasPassword && (typeof changes.body.url === "string" || (showTokenInput && !!tokenTyped));

  // --- pickers ------------------------------------------------------------------------
  const rateNote = (e: unknown) => t("pl.err.rateLimited", { wait: formatWait((e as ApiError).retryAfter ?? 30, t) });
  const listNote = (): ReactNode => {
    if (!configured || (base.error instanceof ApiError && base.error.status === 409)) return t("pl.picker.connectFirst");
    if (base.isPending) return t("pl.picker.loading");
    if (base.error) {
      return base.error instanceof ApiError && base.error.status === 429 ? (
        rateNote(base.error)
      ) : (
        <>
          {t("pl.picker.unavailable")} <TextButton onClick={() => base.refetch()}>{t("pl.picker.retry")}</TextButton>
        </>
      );
    }
    return null;
  };
  const typeData = typeSearch.data ?? base.data;
  const typeOptions = (typeSearch.data ?? base.data)?.document_types.map((o) => ({ name: o.name, count: o.count })) ?? [];
  const typeTruncated = typeData?.truncated.document_types
    ? t("pl.picker.truncated", { shown: typeOptions.length, total: typeData.total.document_types })
    : null;
  const typeNoMatch =
    !!form.documentType.trim() &&
    typeOptions.length > 0 &&
    !typeOptions.some((o) => lower(o.name).includes(lower(form.documentType))) &&
    (!base.data?.truncated.document_types || !!typeSearch.data);
  const tagData = tagSearch.data ?? base.data;
  const tagOptions = tagData?.tags.map((o) => ({ name: o.name, count: o.count })) ?? [];
  const tagTruncated = tagData?.truncated.tags
    ? t("pl.picker.truncated", { shown: tagOptions.length, total: tagData.total.tags })
    : null;

  // --- the count line ---------------------------------------------------------------------
  let countLine: string;
  if (!configured) countLine = t("pl.count.needConnection");
  else if (selectionPending) countLine = t("pl.count.pending");
  else if (empty) countLine = t(SELECTION_TEXT[form.match]);
  else if (preview.error) {
    const e = preview.error;
    countLine =
      e instanceof ApiError && e.status === 429
        ? rateNote(e)
        : e instanceof ApiError && e.status === 409
          ? t("pl.count.needConnection")
          : t("pl.count.failed");
  } else if (!preview.data) countLine = t("pl.count.loading");
  else if (preview.data.empty_selection) countLine = t(SELECTION_TEXT[form.match]);
  else if (preview.data.count === 0) countLine = t("pl.count.none");
  else if (preview.data.count === 1) countLine = t("pl.count.one");
  else countLine = t("pl.count.some", { n: preview.data.count });
  const countIsNumber = configured && !selectionPending && !empty && !preview.error && !!preview.data && !preview.data.empty_selection;

  // --- interval options ---------------------------------------------------------------------
  const knownInterval = INTERVALS.some((o) => o.minutes === form.interval);

  // --- test result --------------------------------------------------------------------------
  const result = test.data;
  let testLine: { ok: boolean; text: string; detail?: string } | null = null;
  if (testNote) testLine = { ok: false, text: testNote };
  else if (result?.ok) {
    testLine = { ok: true, text: result.version ? t("pl.test.ok", { version: result.version }) : t("pl.test.okNoVersion") };
  } else if (result) {
    const kind = result.error_kind ?? "other";
    testLine = {
      ok: false,
      text: t(TEST_ERROR[kind] ?? "pl.err.other"),
      detail: kind === "other" ? (result.error ?? undefined) : undefined,
    };
  } else if (test.error) {
    const e = test.error;
    testLine = {
      ok: false,
      text:
        e instanceof ApiError && e.status === 429
          ? rateNote(e)
          : e instanceof ApiError && e.status === 403 && e.fields.some((f) => f.field === "current_password")
            ? t("pw.wrongCurrent")
            : e instanceof ApiError && e.status === 403
              ? t("pl.openRefused")
              : e instanceof ApiError && e.fields.length
                ? e.fields.map((f) => f.message).join(" ")
                : e.message || t("common.error"),
    };
  }

  const kindTags = form.tags;
  const keptMappings = Object.keys(v.kind_map).filter((k) => !kindTags.some((tag) => lower(tag) === lower(k))).length;

  const formView = (
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        {!embedded && (
          <>
            <h2 className="text-sm font-semibold">{t("pl.title")}</h2>

            <SyncStatus
              status={status}
              onSync={() => sync.mutate()}
              syncing={sync.isPending}
              requested={requested}
            />
          </>
        )}

        <Toggle
          id={id("enabled")}
          label="pl.enabled"
          help="pl.enabledHelp"
          checked={form.enabled}
          onChange={(on) => edit({ enabled: on })}
        />

        {/* ---- connection ---- */}
        <SubHeading>{t("pl.connection")}</SubHeading>

        <Field
          id={id("url")}
          label="pl.url"
          help="pl.urlHelp"
          error={errors.url}
          note={noteFor("url", data.sources.url, urlLocked)}
        >
          {resets.has("url") ? (
            <Input id={id("url")} disabled value="" placeholder={t("pl.src.pending")} />
          ) : (
            <Input
              id={id("url")}
              type="url"
              inputMode="url"
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              placeholder="http://paperless.example.com:8000"
              className={`${invalid("url")} ${urlLocked ? "cursor-not-allowed opacity-70" : ""}`}
              readOnly={urlLocked}
              aria-readonly={urlLocked || undefined}
              aria-invalid={errors.url ? true : undefined}
              aria-describedby={describe(id("url"), true, errors.url)}
              value={form.url}
              onChange={(e) => {
                edit({ url: e.target.value }, ["url", "token"]);
                test.reset();
                setTestNote("");
              }}
            />
          )}
        </Field>

        <Field
          id={id("publicUrl")}
          label="pl.publicUrl"
          help="pl.publicUrlHelp"
          error={errors.public_url}
          note={noteFor("public_url", data.sources.public_url, urlLocked)}
        >
          {resets.has("public_url") ? (
            <Input id={id("publicUrl")} disabled value="" placeholder={t("pl.src.pending")} />
          ) : (
            <Input
              id={id("publicUrl")}
              type="url"
              inputMode="url"
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              className={`${invalid("public_url")} ${urlLocked ? "cursor-not-allowed opacity-70" : ""}`}
              readOnly={urlLocked}
              aria-readonly={urlLocked || undefined}
              aria-invalid={errors.public_url ? true : undefined}
              aria-describedby={describe(id("publicUrl"), true, errors.public_url)}
              value={form.publicUrl}
              onChange={(e) => edit({ publicUrl: e.target.value }, ["public_url"])}
            />
          )}
        </Field>

        {urlLocked && (
          <div className="-mt-2 flex flex-col items-start gap-1">
            <p className="text-xs muted">{t("pl.urlLocked")}</p>
            <Button type="button" variant="ghost" className="min-h-10 text-xs" onClick={goToPasswordCard}>
              {t("pw.titleFirst")}
            </Button>
          </div>
        )}

        {hasPassword && addressTouched && currentPassword === undefined && (
          <Field id={id("password")} label="rs.confirmPassword" error={errors.current_password}>
            <Input
              id={id("password")}
              type="password"
              autoComplete="current-password"
              className={invalid("current_password")}
              aria-invalid={errors.current_password ? true : undefined}
              aria-describedby={describe(id("password"), false, errors.current_password)}
              value={confirmation}
              onChange={(e) => {
                setConfirmation(e.target.value);
                setErrors((cur) => ({ ...cur, current_password: "" }));
              }}
            />
          </Field>
        )}

        {/* the token: write-only */}
        <div className="flex min-w-0 flex-col gap-1">
          {showTokenInput ? (
            <label htmlFor={id("token")} className="text-sm font-medium">
              {t("pl.token")}
            </label>
          ) : (
            <span className="text-sm font-medium">{t("pl.token")}</span>
          )}
          {showTokenInput ? (
            <div className="flex gap-2">
              <Input
                id={id("token")}
                type="password"
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
                placeholder={t("pl.tokenPlaceholder")}
                className={invalid("token")}
                aria-invalid={errors.token ? true : undefined}
                aria-describedby={describe(id("token"), true, errors.token)}
                value={tokenValue}
                onChange={(e) => {
                  setTokenValue(e.target.value);
                  setSaved(false);
                  setErrors((cur) => ({ ...cur, token: "" }));
                  test.reset();
                  setTestNote("");
                }}
              />
              {v.token_set && tokenMode === "replace" && !changes.tokenRequired && (
                <Button
                  type="button"
                  variant="ghost"
                  className="shrink-0"
                  onClick={() => {
                    setTokenMode("keep");
                    setTokenValue("");
                  }}
                >
                  {t("common.cancel")}
                </Button>
              )}
            </div>
          ) : tokenMode === "clear" ? (
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span id={id("token")} tabIndex={-1} className="muted">{t("pl.tokenWillClear")}</span>
              <TextButton onClick={() => setTokenMode("keep")}>{t("pl.undo")}</TextButton>
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <span
                id={id("token")}
                tabIndex={-1}
                className="surface inline-flex min-h-11 min-w-0 flex-1 items-center rounded-xl px-3 font-mono text-sm"
              >
                <span aria-hidden="true">••••{v.token_last4}</span>
                <span className="sr-only">{t("pl.token")} …{v.token_last4}</span>
              </span>
              <Button
                type="button"
                variant="ghost"
                onClick={() => {
                  setTokenMode("replace");
                  setTimeout(() => document.getElementById(id("token"))?.focus(), 0);
                }}
              >
                {t("pl.tokenReplace")}
              </Button>
              {v.token_source === "app" && (
                <Button
                  type="button"
                  variant="danger"
                  onClick={() => {
                    setTokenMode("clear");
                    setSaved(false);
                    test.reset();
                  }}
                >
                  {t("pl.tokenClear")}
                </Button>
              )}
            </div>
          )}
          <p id={`${id("token")}-help`} className="text-xs muted">
            {t("pl.tokenHelp")}
          </p>
          {v.token_set && v.token_source === "env" && <p className="text-xs muted">{t("pl.tokenEnv")}</p>}
          {changes.tokenRequired && !errors.token && (
            <p className="text-xs text-amber-700 dark:text-amber-400">{t("pl.tokenRequired")}</p>
          )}
          {errors.token && (
            <p id={`${id("token")}-err`} role="alert" className="text-xs text-red-600 dark:text-red-400">
              {errors.token}
            </p>
          )}
        </div>

        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" variant="ghost" onClick={runTest} disabled={test.isPending}>
              {test.isPending ? t("pl.testing") : t("rs.test")}
            </Button>
            <span className="text-xs muted">{testsTyped ? t("pl.test.typed") : t("pl.test.saved")}</span>
          </div>
          <div aria-live="polite" className="min-h-5 text-sm">
            {testLine && (
              <p className={testLine.ok ? "text-emerald-700 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}>
                {testLine.text}
                {testLine.detail && <span className="mt-0.5 block text-xs muted">{testLine.detail}</span>}
              </p>
            )}
          </div>
        </div>

        {/* ---- which documents ---- */}
        <SubHeading>{t("pl.docs")}</SubHeading>
        <p className="-mt-2 text-xs muted">{t("pl.docsHelp")}</p>

        <Field id={id("match")} label="pl.match" error={errors.match}>
          <Select
            id={id("match")}
            value={form.match}
            aria-describedby={describe(id("match"), false, errors.match)}
            onChange={(e) => edit({ match: e.target.value as MatchMode })}
          >
            {MATCHES.map((m) => (
              <option key={m} value={m}>
                {t(MATCH_LABEL[m])}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          id={id("docType")}
          label="pl.docType"
          help="pl.docTypeHelp"
          error={errors.document_type}
          note={
            <>
              {form.match === "tags_only" && <p className="text-xs muted">{t("pl.docTypeUnused")}</p>}
              {!resets.has("document_type") && (() => {
                const note = listNote();
                return note && <p className="text-xs muted">{note}</p>;
              })()}
              {noteFor("document_type", data.sources.document_type)}
            </>
          }
        >
          {resets.has("document_type") ? (
            <Input id={id("docType")} disabled value="" placeholder={t("pl.src.pending")} />
          ) : (
            <Combobox
              id={id("docType")}
              value={form.documentType}
              onChange={(text) => {
                edit({ documentType: text }, ["document_type"]);
                setTypeQuery(text);
              }}
              onPick={(name) => {
                edit({ documentType: name }, ["document_type"]);
                setTypeQuery("");
              }}
              options={typeOptions}
              footer={typeNoMatch ? t("pl.picker.noMatch") : typeTruncated}
              describedBy={describe(id("docType"), true, errors.document_type)}
              invalid={!!errors.document_type}
            />
          )}
        </Field>

        <Field
          id={id("tags")}
          label="pl.tags"
          help="pl.tagsHelp"
          error={errors.tags}
          note={
            <>
              {form.match === "type_only" && <p className="text-xs muted">{t("pl.tagsUnused")}</p>}
              {noteFor("tags", data.sources.tags)}
            </>
          }
        >
          {resets.has("tags") ? (
            <Input id={id("tags")} disabled value="" placeholder={t("pl.src.pending")} />
          ) : (
            <TagPicker
              id={id("tags")}
              tags={form.tags}
              onChange={(tags) => edit({ tags }, ["tags"])}
              options={tagOptions}
              onQuery={setTagQuery}
              footer={tagTruncated}
              placeholder={t("pl.tagsPlaceholder")}
              describedBy={describe(id("tags"), true, errors.tags)}
              invalid={!!errors.tags}
            />
          )}
        </Field>

        <div aria-live="polite" className="min-h-6">
          <p
            className={`text-sm ${countIsNumber ? "font-medium" : "muted"} ${preview.isFetching || previewKey !== wantedKey ? "opacity-60" : ""}`}
          >
            {countLine}
          </p>
          {errors.selection && (
            <p role="alert" className="mt-1 text-xs text-red-600 dark:text-red-400">
              {errors.selection}
            </p>
          )}
        </div>

        {/* ---- kind per tag ---- */}
        <SubHeading>{t("pl.kinds")}</SubHeading>
        <p className="-mt-2 text-xs muted">{t("pl.kindsHelp")}</p>
        {kindTags.length === 0 ? (
          <p className="text-xs muted">{t("pl.kindsNone")}</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {kindTags.map((tag, i) => (
              <li key={tag} className="grid grid-cols-1 items-center gap-1 sm:grid-cols-2 sm:gap-3">
                <label htmlFor={id(`kind-${i}`)} className="min-w-0 break-words text-sm">
                  {tag}
                </label>
                <Select
                  id={id(`kind-${i}`)}
                  value={kindOf(v.kind_map, kindEdits, tag)}
                  onChange={(e) => {
                    setKindEdits({ ...kindEdits, [tag]: e.target.value as DocumentKind });
                    setSaved(false);
                  }}
                >
                  {KINDS.map((k) => (
                    <option key={k} value={k}>
                      {t(KIND_LABEL[k])}
                    </option>
                  ))}
                </Select>
              </li>
            ))}
          </ul>
        )}
        {keptMappings > 0 && <p className="text-xs muted">{t("pl.kindsKept", { n: keptMappings })}</p>}
        {errors.kind_map && (
          <p role="alert" className="text-xs text-red-600 dark:text-red-400">
            {errors.kind_map}
          </p>
        )}

        {/* ---- interval ---- */}
        <div className="border-t" style={{ borderColor: "var(--border)" }} aria-hidden="true" />
        <Field
          id={id("interval")}
          label="pl.interval"
          help="pl.intervalHelp"
          error={errors.sync_interval_minutes}
          note={noteFor("sync_interval_minutes", data.sources.sync_interval_minutes)}
        >
          <Select
            id={id("interval")}
            value={resets.has("sync_interval_minutes") ? "" : form.interval}
            disabled={resets.has("sync_interval_minutes")}
            className={invalid("sync_interval_minutes")}
            aria-describedby={describe(id("interval"), true, errors.sync_interval_minutes)}
            onChange={(e) => edit({ interval: Number(e.target.value) }, ["sync_interval_minutes"])}
          >
            {resets.has("sync_interval_minutes") && <option value="">{t("pl.src.pending")}</option>}
            {!knownInterval && !resets.has("sync_interval_minutes") && (
              <option value={form.interval}>{t("pl.interval.custom", { n: form.interval })}</option>
            )}
            {INTERVALS.map((o) => (
              <option key={o.minutes} value={o.minutes}>
                {t(o.label)}
              </option>
            ))}
          </Select>
        </Field>

        {/* ---- save ---- */}
        <div className="flex flex-col gap-2 border-t pt-4" style={{ borderColor: "var(--border)" }}>
          {(errors._ || Object.values(errors).some(Boolean)) && (
            <p role="alert" className="text-sm text-red-600 dark:text-red-400">
              {errors._ || t("pl.fixFields")}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" disabled={!dirty || save.isPending}>
              {save.isPending ? t("pl.saving") : t("common.save")}
            </Button>
            <Button type="button" variant="ghost" onClick={discard} disabled={!dirty || save.isPending}>
              {t("pl.discard")}
            </Button>
            <span aria-live="polite" className="min-h-5 text-xs">
              {dirty ? (
                <span className="muted">{t("pl.unsaved")}</span>
              ) : saved ? (
                <span className="text-emerald-700 dark:text-emerald-400">{t("rs.saved")}</span>
              ) : null}
            </span>
          </div>
        </div>
      </form>
  );
  return embedded ? formView : <Card className="p-4">{formView}</Card>;
}

// --- status -----------------------------------------------------------------------

function SyncStatus({
  status,
  onSync,
  syncing,
  requested,
}: {
  status: SystemStatus | undefined;
  onSync: () => void;
  syncing: boolean;
  requested: boolean;
}) {
  const { t } = useI18n();
  const p = status?.paperless;
  const worker = status?.worker;
  const busy = !!worker?.running;
  const skipped = p?.skipped;
  const result = worker?.last_sync_result;

  return (
    <div className="flex flex-col gap-2 rounded-xl p-3 text-xs" style={{ background: "var(--bg)" }}>
      <div className="flex flex-col items-start gap-3 sm:flex-row sm:items-center">
        <Button type="button" onClick={onSync} disabled={busy || syncing || !p || !!skipped}>
          {busy ? t("settings.syncing") : t("settings.syncNow")}
        </Button>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5" aria-live="polite">
          {skipped === "disabled" && <span className="font-medium">{t("pl.status.off")}</span>}
          {skipped === "not_configured" && <span className="font-medium">{t("pl.status.notConfigured")}</span>}
          {busy ? (
            <span className="muted">{t("pl.status.working")}</span>
          ) : (
            <span className="muted">
              {worker?.last_sync ? t("pl.status.last", { when: whenText(worker.last_sync) }) : t("pl.status.never")}
              {result && ` · ${t("settings.lastResult", { seen: result.seen, created: result.created })}`}
            </span>
          )}
          {!skipped && p && p.sync_interval_minutes === 0 && <span className="muted">{t("pl.status.manual")}</span>}
          {!skipped && p && p.sync_interval_minutes > 0 && worker?.next_sync && (
            <span className="muted">{t("pl.status.next", { when: whenText(worker.next_sync) })}</span>
          )}
          {requested && <span className="text-emerald-700 dark:text-emerald-400">{t("pl.status.requested")}</span>}
        </div>
      </div>
      <span className="muted">
        {t("settings.documents", { n: status?.documents.total ?? 0, hidden: status?.documents.ignored ?? 0 })}
      </span>
      {worker?.last_error && (
        <span className="break-words text-red-600 dark:text-red-400">
          {t("pl.status.failed", { error: worker.last_error })}
        </span>
      )}
    </div>
  );
}
