import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type MedicalDocument, type SystemStatus } from "../api";
import { useI18n } from "../i18n";
import { DocumentBadge, Pill, isActive, kindLine } from "../reading";
import { Button, Card, EmptyState, Spinner, formatDate } from "../ui";
import AddDocuments from "./AddDocuments";
import DocumentSearch from "./DocumentSearch";
import { DeleteDocumentDialog, EditDocumentDialog } from "./DocumentDialogs";

/** No Paperless to sync from: the list can only fill by uploading. */
export const useOwnDocumentsOnly = () => {
  const { data } = useQuery({
    queryKey: ["status"],
    queryFn: () => api.get<SystemStatus>("/status"),
  });
  return !!data && !(data.paperless.enabled && data.paperless.configured);
};

export default function Documents() {
  const { t } = useI18n();
  const [showHidden, setShowHidden] = useState(false);
  const [searching, setSearching] = useState(false);
  const ownOnly = useOwnDocumentsOnly();
  const { data, isLoading } = useQuery({
    queryKey: ["documents", showHidden],
    queryFn: () =>
      api.get<MedicalDocument[]>(`/documents${showHidden ? "?include_ignored=true" : ""}`),
    // Reading progress comes from the server: poll while something is being read.
    refetchInterval: (query) => (query.state.data?.some((d) => isActive(d.reading)) ? 3000 : false),
  });

  if (isLoading) return <Spinner label={t("common.loading")} />;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">{t("docs.title")}</h1>
        <label className="flex items-center gap-2 text-sm muted">
          <input
            type="checkbox"
            className="size-4"
            checked={showHidden}
            onChange={(e) => setShowHidden(e.target.checked)}
          />
          {t("docs.showHidden")}
        </label>
      </div>
      {!!data?.length && <AddDocuments />}
      {!!data?.length && <DocumentSearch onActive={setSearching} />}
      {searching ? null : !data?.length ? (
        <EmptyState>
          <div className="flex flex-col items-center gap-3">
            <span>{ownOnly ? t("docs.emptyOwn") : `${t("docs.empty")} ${t("docs.emptyOrAdd")}`}</span>
            <AddDocuments label={ownOnly ? t("up.addFirst") : t("up.add")} variant={ownOnly ? "primary" : "ghost"} />
          </div>
        </EmptyState>
      ) : (
        <div className="flex flex-col gap-2">
          {data.map((doc) => (
            <DocumentRow key={doc.id} doc={doc} />
          ))}
        </div>
      )}
    </div>
  );
}

function Thumbnail({ doc }: { doc: MedicalDocument }) {
  const [failed, setFailed] = useState(false);
  const box = "size-14 rounded-lg";
  return doc.has_file && !failed ? (
    <img
      src={`/api/documents/${doc.id}/thumbnail`}
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
      className={`${box} object-cover`}
      style={{ border: "1px solid var(--border)" }}
    />
  ) : (
    <span
      aria-hidden="true"
      className={`${box} flex items-center justify-center muted`}
      style={{ border: "1px solid var(--border)", background: "var(--bg)" }}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className="size-6">
        <path d="M7 4h7l5 5v11H7zM14 4v5h5" />
      </svg>
    </span>
  );
}

export function DocumentRow({ doc, compact }: { doc: MedicalDocument; compact?: boolean }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<"edit" | "delete" | null>(null);
  const toggle = useMutation({
    mutationFn: () => api.post(`/documents/${doc.id}/ignore`, { ignored: !doc.ignored }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      queryClient.invalidateQueries({ queryKey: ["status"] });
    },
  });
  const uploaded = doc.source === "upload";

  return (
    <Card
      className={`relative flex flex-wrap items-center gap-x-4 gap-y-2 p-3 ${doc.ignored ? "opacity-50" : ""} ${
        compact ? "hover:bg-black/5 dark:hover:bg-white/5" : ""
      }`}
    >
      {/* The thumbnail opens the document where its file lives: Paperless, or the review page for an upload.
          In the compact lists (Examinations, home) the whole card opens the document's page (the title link is
          stretched over it); Paperless is one click further, on that page. */}
      {doc.paperless_link && !compact ? (
        <a href={doc.paperless_link} target="_blank" rel="noreferrer" className="shrink-0">
          <Thumbnail doc={doc} />
        </a>
      ) : (
        <Link to={`/documents/${doc.id}`} className="shrink-0">
          <Thumbnail doc={doc} />
        </Link>
      )}
      <div className="min-w-0 flex-1 basis-40">
        <Link
          to={`/documents/${doc.id}`}
          className={`line-clamp-2 text-sm font-medium [overflow-wrap:anywhere] ${
            compact ? "after:absolute after:inset-0 after:content-['']" : ""
          }`}
        >
          {doc.title}
        </Link>
        <p className="text-xs muted">
          {doc.doc_date ? formatDate(doc.doc_date) : t("doc.notSet")} · {t(`kind.${doc.kind}` as "kind.other")}
        </p>
        {kindLine(t, doc) && <p className="text-xs [overflow-wrap:anywhere]">{kindLine(t, doc)}</p>}
        {compact && doc.report?.conclusion && (
          <p className="mt-0.5 line-clamp-2 text-xs [overflow-wrap:anywhere]">{doc.report.conclusion}</p>
        )}
        <Link to={`/documents/${doc.id}`} className="mt-1 flex flex-wrap items-center gap-1">
          {uploaded && <Pill tone="gray">{t("doc.uploaded")}</Pill>}
          <DocumentBadge doc={doc} />
        </Link>
      </div>
      {!compact && !uploaded && (
        <Button
          variant="ghost"
          className="shrink-0 px-3"
          onClick={() => toggle.mutate()}
          disabled={toggle.isPending}
        >
          {doc.ignored ? t("docs.unhide") : t("docs.hide")}
        </Button>
      )}
      {!compact && uploaded && (
        <div className="flex w-full shrink-0 justify-end gap-2 sm:w-auto">
          <Button variant="ghost" className="min-h-10 px-3" onClick={() => setDialog("edit")}>
            {t("doc.edit")}
          </Button>
          <Button variant="danger" className="min-h-10 px-3" onClick={() => setDialog("delete")}>
            {t("doc.delete")}
          </Button>
        </div>
      )}
      {!compact && uploaded && (
        <>
          <EditDocumentDialog doc={doc} open={dialog === "edit"} onClose={() => setDialog(null)} />
          <DeleteDocumentDialog doc={doc} open={dialog === "delete"} onClose={() => setDialog(null)} />
        </>
      )}
    </Card>
  );
}
