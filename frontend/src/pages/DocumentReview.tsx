import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type CatalogTest,
  type MedicalDocument,
  type DocumentValues,
  type ExtractedValue,
  type ReadingSettingsResponse,
  type ValueStatus,
} from "../api";
import { useI18n } from "../i18n";
import { Flag, Pill, ReadingBadge, isActive, reasonText, stageText, testName } from "../reading";
import { Button, Card, EmptyState, Input, Select, Spinner, buttonClass, formatBytes, formatDate } from "../ui";
import { refreshDocumentQueries } from "../upload";
import { DeleteDocumentDialog, EditDocumentDialog } from "./DocumentDialogs";

const GROUPS: ValueStatus[] = ["needs_review", "verified", "approved", "rejected"];

export default function DocumentReview() {
  const { id } = useParams();
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [confirmClear, setConfirmClear] = useState(false);
  const [notice, setNotice] = useState("");
  const [dialog, setDialog] = useState<"edit" | "delete" | null>(null);
  const navigate = useNavigate();

  const { data, isLoading, error } = useQuery({
    queryKey: ["values", id],
    queryFn: () => api.get<DocumentValues>(`/documents/${id}/values`),
    refetchInterval: (query) => (isActive(query.state.data?.document.reading) ? 2500 : false),
  });
  const { data: settings } = useQuery({
    queryKey: ["reading-settings"],
    queryFn: () => api.get<ReadingSettingsResponse>("/reading/settings"),
  });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["values", id] });
    queryClient.invalidateQueries({ queryKey: ["documents"] });
  };
  const onError = (e: Error) => setNotice(e.message || t("common.error"));

  const read = useMutation({
    mutationFn: () => api.post(`/documents/${id}/read`),
    onMutate: () => setNotice(""),
    onSuccess: refresh,
    onError,
  });
  const approveAll = useMutation({
    mutationFn: () => api.post<{ approved: number }>(`/documents/${id}/values/approve-verified`),
    onSuccess: (r) => {
      setNotice(t("review.approvedN", { n: r.approved }));
      refresh();
    },
    onError,
  });
  const clear = useMutation({
    mutationFn: () => api.del(`/documents/${id}/values`),
    onSuccess: () => {
      setConfirmClear(false);
      setNotice("");
      refresh();
    },
    onError,
  });

  if (isLoading) return <Spinner label={t("common.loading")} />;
  if (error || !data) return <EmptyState>{(error as Error)?.message ?? t("common.error")}</EmptyState>;

  const { document: doc, last_run: run, values } = data;
  const reading = doc.reading;
  const active = isActive(reading);
  const enabled = settings?.settings.enabled ?? true;
  const count = (s: ValueStatus) => values.filter((v) => v.status === s).length;
  const unapproved = values.length - count("approved");
  const uploaded = doc.source === "upload";

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <Link to="/" className="text-sm text-emerald-600">
          ‹ {t("review.back")}
        </Link>
        <h1 className="text-xl font-semibold tracking-tight [overflow-wrap:anywhere]">{doc.title}</h1>
        <div className="flex flex-wrap items-center gap-2 text-xs muted">
          {uploaded && <Pill tone="gray">{t("doc.uploaded")}</Pill>}
          <span>{doc.doc_date ? formatDate(doc.doc_date) : t("doc.notSet")}</span>
          <span>· {t(`kind.${doc.kind}` as "kind.other")}</span>
          <ReadingBadge reading={reading} />
        </div>
        {uploaded && doc.original_filename && (
          <p className="text-xs muted [overflow-wrap:anywhere]">
            {doc.original_filename}
            {doc.size_bytes != null && ` · ${formatBytes(doc.size_bytes)}`}
          </p>
        )}
      </div>

      {uploaded && !doc.doc_date && <SetDate doc={doc} onError={onError} />}

      <Card className="flex flex-col gap-3 p-4">
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => read.mutate()} disabled={active || !enabled || read.isPending}>
            {run ? t("review.readAgain") : t("review.read")}
          </Button>
          {doc.has_file && (
            <a
              href={`/api/documents/${doc.id}/preview`}
              target="_blank"
              rel="noreferrer"
              className={buttonClass("ghost")}
            >
              {t("review.preview")}
            </a>
          )}
          {doc.paperless_link && (
            <a href={doc.paperless_link} target="_blank" rel="noreferrer" className={buttonClass("ghost")}>
              {t("review.paperless")}
            </a>
          )}
          {uploaded && (
            <>
              <Button variant="ghost" onClick={() => setDialog("edit")}>
                {t("doc.edit")}
              </Button>
              <Button variant="danger" onClick={() => setDialog("delete")}>
                {t("doc.delete")}
              </Button>
            </>
          )}
        </div>
        {!doc.has_file && <p className="text-xs text-amber-600">{t("doc.fileMissing")}</p>}
        {!enabled ? (
          <p className="text-xs text-amber-600">{t("review.disabled")}</p>
        ) : (
          <p className="text-xs muted">{run ? t("review.readAgainHint") : t("review.readHint")}</p>
        )}
        {active && reading && <Progress reading={reading} />}
        {run && !active && (
          <div className="flex flex-col gap-1 text-xs muted">
            {run.status === "done" || run.status === "cleared" ? (
              <span>
                {t("review.lastRun", {
                  when: `${formatDate(run.started_at)} ${run.started_at.slice(11, 16)}`,
                  secs: Math.round(run.duration_s ?? 0),
                  pages: run.pages,
                })}
              </span>
            ) : (
              <span className="text-red-600">{t("review.lastRunError", { error: run.error || run.status })}</span>
            )}
            {run.page_errors.length > 0 && (
              <details>
                <summary className="cursor-pointer">
                  {t("review.pageErrors")} ({run.page_errors.length})
                </summary>
                <ul className="mt-1 list-disc pl-5">
                  {run.page_errors.map((e, i) => (
                    <li key={i}>
                      {t("review.page", { n: e.page })} · {e.reader}: {e.error}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
        {notice && <p className="text-sm">{notice}</p>}
      </Card>

      {values.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            onClick={() => approveAll.mutate()}
            disabled={!count("verified") || active || approveAll.isPending}
          >
            {t("review.approveAll")} ({count("verified")})
          </Button>
          {!confirmClear ? (
            <Button variant="danger" onClick={() => setConfirmClear(true)} disabled={!unapproved || active}>
              {t("review.clear")}
            </Button>
          ) : (
            <span className="flex flex-wrap items-center gap-2 text-sm">
              {t("review.clearConfirm")}
              <Button variant="danger" onClick={() => clear.mutate()} disabled={clear.isPending}>
                {t("review.clearYes")}
              </Button>
              <Button variant="ghost" onClick={() => setConfirmClear(false)}>
                {t("common.cancel")}
              </Button>
            </span>
          )}
        </div>
      )}

      {values.length === 0 ? (
        !active && <EmptyState>{t("review.none")}</EmptyState>
      ) : (
        GROUPS.map((status) => {
          const rows = values.filter((v) => v.status === status);
          if (!rows.length) return null;
          return (
            <section key={status} className="flex flex-col gap-2">
              <h2 className="text-sm font-semibold">
                {t(`review.group.${status}`)} <span className="muted">({rows.length})</span>
              </h2>
              <ValueList rows={rows} disabled={active} onChanged={refresh} onError={onError} />
            </section>
          );
        })
      )}

      {uploaded && (
        <>
          <EditDocumentDialog doc={doc} open={dialog === "edit"} onClose={() => setDialog(null)} />
          <DeleteDocumentDialog
            doc={doc}
            open={dialog === "delete"}
            onClose={() => setDialog(null)}
            onDeleted={() => navigate("/documents")}
          />
        </>
      )}
    </div>
  );
}

/** An upload with no date cannot be put in order in the history and charts: a quick way to add it. */
function SetDate({ doc, onError }: { doc: MedicalDocument; onError: (e: Error) => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [date, setDate] = useState("");
  const save = useMutation({
    mutationFn: () => api.patch(`/documents/${doc.id}`, { doc_date: date }),
    onSuccess: () => refreshDocumentQueries(queryClient),
    onError,
  });
  const today = new Date();
  const max = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  return (
    <form
      className="flex flex-col gap-2 rounded-xl bg-amber-500/10 p-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (date) save.mutate();
      }}
    >
      <p className="text-xs text-amber-900 dark:text-amber-200">
        {t("doc.notSet")}. {t("doc.dateWhy")}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          type="date"
          aria-label={t("doc.field.date")}
          min="1900-01-01"
          max={max}
          value={date}
          onChange={(e) => setDate(e.target.value)}
          className="w-auto min-w-40"
        />
        <Button type="submit" disabled={!date || save.isPending}>
          {t("doc.setDate")}
        </Button>
      </div>
    </form>
  );
}

function Progress({ reading }: { reading: NonNullable<DocumentValues["document"]["reading"]> }) {
  const { t } = useI18n();
  // reader A and reader B each pass over every page
  const steps = reading.pages * 2 || 1;
  const done =
    reading.stage === "reader_a"
      ? reading.page - 1
      : reading.stage === "reader_b"
        ? reading.pages + reading.page - 1
        : reading.stage === "verify"
          ? steps
          : 0;
  const pct = Math.max(3, Math.round((done / steps) * 100));
  return (
    <div className="flex flex-col gap-1">
      <div className="h-2 overflow-hidden rounded-full bg-slate-500/15">
        <div className="h-full rounded-full bg-emerald-500 transition-all" style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs muted">
        {reading.state === "queued"
          ? t("reading.queued")
          : `${stageText(t, reading.stage)}${reading.pages ? ` · ${reading.page}/${reading.pages}` : ""}`}
      </span>
    </div>
  );
}

function ValueList({
  rows,
  disabled,
  onChanged,
  onError,
}: {
  rows: ExtractedValue[];
  disabled: boolean;
  onChanged: () => void;
  onError: (e: Error) => void;
}) {
  const { t } = useI18n();
  const [editing, setEditing] = useState<number | null>(null);
  const headers = ["review.col.test", "review.col.value", "review.col.range", "review.col.flag"] as const;

  return (
    <Card className="overflow-hidden">
      {/* Wide screens (lg+, side nav leaves ~780px): a table. Phones: one block per value. */}
      <div className="hidden overflow-x-auto lg:block">
        <table className="w-full table-fixed text-left text-sm">
          <colgroup>
            <col />
            <col className="w-28" />
            <col className="w-36" />
            <col className="w-16" />
            <col className="w-64" />
          </colgroup>
          <thead className="text-xs muted">
            <tr>
              {headers.map((h) => (
                <th key={h} className="px-3 py-2 font-medium whitespace-nowrap">
                  {t(h)}
                </th>
              ))}
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {rows.map((v) =>
              editing === v.id ? (
                <tr key={v.id} style={{ borderTop: "1px solid var(--border)" }}>
                  <td colSpan={headers.length + 1} className="p-3">
                    <EditForm value={v} onDone={() => { setEditing(null); onChanged(); }} onCancel={() => setEditing(null)} onError={onError} />
                  </td>
                </tr>
              ) : (
                <DesktopRow key={v.id} v={v} disabled={disabled} onEdit={() => setEditing(v.id)} onChanged={onChanged} onError={onError} />
              ),
            )}
          </tbody>
        </table>
      </div>
      <div className="flex flex-col lg:hidden">
        {rows.map((v) => (
          <div key={v.id} className="flex min-w-0 flex-col gap-2 p-3" style={{ borderTop: "1px solid var(--border)" }}>
            {editing === v.id ? (
              <EditForm value={v} onDone={() => { setEditing(null); onChanged(); }} onCancel={() => setEditing(null)} onError={onError} />
            ) : (
              <MobileRow v={v} disabled={disabled} onEdit={() => setEditing(v.id)} onChanged={onChanged} onError={onError} />
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

type RowProps = {
  v: ExtractedValue;
  disabled: boolean;
  onEdit: () => void;
  onChanged: () => void;
  onError: (e: Error) => void;
};

function Name({ v, wrap }: { v: ExtractedValue; wrap?: boolean }) {
  const { t, lang } = useI18n();
  const cls = wrap ? "block" : "block truncate";
  return v.test_code ? (
    <span className={cls}>{testName(v, lang)}</span>
  ) : (
    <span className={`${cls} text-amber-600`}>{t("review.unknownTest")}</span>
  );
}

const same = (a: string, b: string) =>
  a.replace(",", ".").replace(/\s+/g, "").toLowerCase() === b.replace(",", ".").replace(/\s+/g, "").toLowerCase();

function Readers({ v }: { v: ExtractedValue }) {
  const differ = v.reader_a != null && v.reader_b != null && !same(v.reader_a, v.reader_b);
  return (
    <span className={differ ? "text-amber-600" : ""}>
      {v.reader_a ?? "—"} / {v.reader_b ?? "—"}
    </span>
  );
}

function DesktopRow({ v, ...props }: RowProps) {
  const { t } = useI18n();
  return (
    <tr style={{ borderTop: "1px solid var(--border)" }} className={v.status === "rejected" ? "opacity-60" : ""}>
      <td className="min-w-0 px-3 py-2 align-top">
        <div className="font-medium break-words">
          <Name v={v} wrap />
        </div>
        <div className="mt-0.5 flex flex-col gap-0.5 text-xs muted">
          <span className="break-words">{v.raw_name}</span>
          <span className="break-words">
            A / B: <Readers v={v} />
            {v.page != null && <> · {t("review.page", { n: v.page })}</>}
            {reasonText(t, v.reason) && <> · {reasonText(t, v.reason)}</>}
          </span>
        </div>
      </td>
      <td className="px-3 py-2 align-top whitespace-nowrap">
        <span className="font-semibold">{v.value}</span> <span className="muted">{v.unit}</span>
      </td>
      <td className="px-3 py-2 align-top break-words">{v.ref_range}</td>
      <td className="px-3 py-2 align-top"><Flag flag={v.flag} /></td>
      <td className="px-3 py-2 align-top"><Actions v={v} {...props} nowrap /></td>
    </tr>
  );
}

function MobileRow({ v, ...props }: RowProps) {
  const { t } = useI18n();
  return (
    <div className={`flex min-w-0 flex-col gap-1 ${v.status === "rejected" ? "opacity-60" : ""}`}>
      <div className="flex min-w-0 items-start justify-between gap-3">
        <span className="min-w-0 flex-1 truncate text-sm font-medium"><Name v={v} /></span>
        <span className="shrink-0 whitespace-nowrap text-right text-sm font-semibold">
          {v.value} <span className="font-normal muted">{v.unit}</span>
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs muted">
        <span>{v.raw_name}</span>
        {v.ref_range && <span>· {v.ref_range}</span>}
        {v.page != null && <span>· {t("review.page", { n: v.page })}</span>}
        <Flag flag={v.flag} />
      </div>
      <div className="text-xs muted">
        A / B: <Readers v={v} /> · {reasonText(t, v.reason)}
      </div>
      <Actions v={v} {...props} />
    </div>
  );
}

function Actions({ v, disabled, onEdit, onChanged, onError, nowrap }: RowProps & { nowrap?: boolean }) {
  const { t } = useI18n();
  const approve = useMutation({
    mutationFn: () => api.post(`/values/${v.id}/approve`),
    onSuccess: onChanged,
    onError,
  });
  const reject = useMutation({
    mutationFn: () => api.post(`/values/${v.id}/reject`),
    onSuccess: onChanged,
    onError,
  });
  const busy = disabled || approve.isPending || reject.isPending;
  return (
    <div className={`flex justify-end gap-1 ${nowrap ? "flex-nowrap whitespace-nowrap" : "flex-wrap"}`}>
      {v.status !== "approved" && v.test_code && (
        <Button className={nowrap ? "min-h-9 px-2.5" : "min-h-9 px-3"} onClick={() => approve.mutate()} disabled={busy}>
          {t("review.approve")}
        </Button>
      )}
      <Button variant="ghost" className={nowrap ? "min-h-9 px-2.5" : "min-h-9 px-3"} onClick={onEdit} disabled={busy}>
        {t("review.edit")}
      </Button>
      {v.status !== "rejected" && (
        <Button variant="danger" className={nowrap ? "min-h-9 px-2.5" : "min-h-9 px-3"} onClick={() => reject.mutate()} disabled={busy}>
          {t("review.reject")}
        </Button>
      )}
    </div>
  );
}

function EditForm({
  value,
  onDone,
  onCancel,
  onError,
}: {
  value: ExtractedValue;
  onDone: () => void;
  onCancel: () => void;
  onError: (e: Error) => void;
}) {
  const { t, lang } = useI18n();
  const { data: catalog } = useQuery({
    queryKey: ["catalog"],
    queryFn: () => api.get<CatalogTest[]>("/reading/catalog"),
    staleTime: Infinity,
  });
  // The catalogue is in report order; a picker is easier to scan alphabetically
  // (accents and case ignored, in the language shown).
  const sortedCatalog = useMemo(() => {
    const name = (c: CatalogTest) => (lang === "el" ? c.name_el : c.name_en);
    return [...(catalog ?? [])].sort((a, b) =>
      name(a).localeCompare(name(b), lang, { sensitivity: "base" }),
    );
  }, [catalog, lang]);
  const [form, setForm] = useState({
    test_code: value.test_code ?? "",
    value: value.value,
    unit: value.unit,
    ref_range: value.ref_range,
  });
  const save = useMutation({
    mutationFn: () => api.post(`/values/${value.id}/approve`, form),
    onSuccess: onDone,
    onError,
  });
  const set = (key: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm({ ...form, [key]: e.target.value });

  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <p className="text-xs muted">
        {value.raw_name} · A / B: <Readers v={value} />
      </p>
      <Select value={form.test_code} onChange={set("test_code")} required aria-label={t("review.col.test")}>
        <option value="">{t("review.chooseTest")}</option>
        {sortedCatalog.map((c) => (
          <option key={c.code} value={c.code}>
            {lang === "el" ? c.name_el : c.name_en} ({c.code})
          </option>
        ))}
      </Select>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        <label className="flex flex-col gap-1 text-xs muted">
          {t("review.col.value")}
          <Input value={form.value} onChange={set("value")} required maxLength={100} />
        </label>
        <label className="flex flex-col gap-1 text-xs muted">
          {t("review.col.unit")}
          <Input value={form.unit} onChange={set("unit")} maxLength={40} />
        </label>
        <label className="flex flex-col gap-1 text-xs muted">
          {t("review.col.range")}
          <Input value={form.ref_range} onChange={set("ref_range")} maxLength={80} />
        </label>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={save.isPending || !form.test_code || !form.value.trim()}>
          {t("review.saveApprove")}
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>
          {t("common.cancel")}
        </Button>
      </div>
    </form>
  );
}
