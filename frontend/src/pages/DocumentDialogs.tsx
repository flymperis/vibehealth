import { useEffect, useId, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, type DocumentKind, type DocumentPatch, type MedicalDocument } from "../api";
import { useI18n } from "../i18n";
import { refreshDocumentQueries } from "../upload";
import { Button, Dialog, Input, Select } from "../ui";

const KINDS: DocumentKind[] = ["blood_test", "report", "prescription", "imaging", "other"];

/** The server's text for a refused change: the messages of a validation error, or the plain detail. */
function errorText(e: unknown, fallback: string) {
  if (e instanceof ApiError) return e.message || fallback;
  return e instanceof Error ? e.message : fallback;
}

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Title, type and date of an uploaded document. Paperless documents are edited in Paperless. */
export function EditDocumentDialog({
  doc,
  open,
  onClose,
}: {
  doc: MedicalDocument;
  open: boolean;
  onClose: () => void;
}) {
  const { t } = useI18n();
  return (
    <Dialog open={open} onClose={onClose} title={t("doc.editTitle")}>
      <EditForm doc={doc} onClose={onClose} />
    </Dialog>
  );
}

function EditForm({ doc, onClose }: { doc: MedicalDocument; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const ids = { title: useId(), kind: useId(), date: useId(), dateHelp: useId() };
  const [title, setTitle] = useState(doc.title);
  const [kind, setKind] = useState<DocumentKind>(doc.kind);
  const [date, setDate] = useState(doc.doc_date ?? "");
  const [error, setError] = useState("");

  const save = useMutation({
    mutationFn: (body: DocumentPatch) => api.patch<MedicalDocument>(`/documents/${doc.id}`, body),
    onSuccess: () => {
      refreshDocumentQueries(queryClient);
      onClose();
    },
    onError: (e) => setError(errorText(e, t("common.error"))),
  });

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    const body: DocumentPatch = {};
    if (title.trim() !== doc.title) body.title = title.trim();
    if (kind !== doc.kind) body.kind = kind;
    if (date !== (doc.doc_date ?? "")) body.doc_date = date || null;
    if (!Object.keys(body).length) return onClose();
    save.mutate(body);
  };

  return (
    <form onSubmit={submit} className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <label htmlFor={ids.title} className="text-sm font-medium">
          {t("doc.field.title")}
        </label>
        <Input id={ids.title} data-autofocus value={title} maxLength={200} required onChange={(e) => setTitle(e.target.value)} />
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={ids.kind} className="text-sm font-medium">
          {t("up.kind")}
        </label>
        <Select id={ids.kind} value={kind} onChange={(e) => setKind(e.target.value as DocumentKind)}>
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {t(`kind.${k}` as "kind.other")}
            </option>
          ))}
        </Select>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={ids.date} className="text-sm font-medium">
          {t("doc.field.date")}
        </label>
        <Input
          id={ids.date}
          type="date"
          aria-describedby={ids.dateHelp}
          min="1900-01-01"
          max={todayIso()}
          value={date}
          onChange={(e) => setDate(e.target.value)}
          className="sm:max-w-56"
        />
        <p id={ids.dateHelp} className="text-xs muted">
          {t("doc.dateHelp")} {t("doc.dateWhy")}
        </p>
      </div>
      {error && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={save.isPending || !title.trim()}>
          {save.isPending ? t("doc.saving") : t("common.save")}
        </Button>
        <Button type="button" variant="ghost" onClick={onClose}>
          {t("common.cancel")}
        </Button>
      </div>
    </form>
  );
}

/**
 * Delete an uploaded document with its file and values. `onDeleted` runs once the dialog is done
 * (the review page uses it to leave a page whose document no longer exists).
 */
export function DeleteDocumentDialog({
  doc,
  open,
  onClose,
  onDeleted,
}: {
  doc: MedicalDocument;
  open: boolean;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const { t } = useI18n();
  return (
    <Dialog open={open} onClose={onClose} title={t("doc.deleteTitle")}>
      <DeleteBody doc={doc} onClose={onClose} onDeleted={onDeleted} />
    </Dialog>
  );
}

function DeleteBody({
  doc,
  onClose,
  onDeleted,
}: {
  doc: MedicalDocument;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [error, setError] = useState("");
  const [fileLeft, setFileLeft] = useState(false);
  const deleted = useRef(false);
  const finished = useRef(false);

  const finish = () => {
    if (finished.current) return;
    finished.current = true;
    onClose();
    onDeleted?.();
    // After the page has moved on: refetching the values of a document that no longer exists would only 404.
    setTimeout(() => {
      queryClient.removeQueries({ queryKey: ["values", String(doc.id)] });
      refreshDocumentQueries(queryClient);
    }, 0);
  };
  // A delete that happened must reach the lists however the dialog is left (Escape, backdrop).
  const finishRef = useRef(finish);
  finishRef.current = finish;
  useEffect(
    () => () => {
      if (deleted.current) finishRef.current();
    },
    [],
  );

  const remove = useMutation({
    mutationFn: () => api.del<{ deleted: boolean; file_removed: boolean }>(`/documents/${doc.id}`),
    onSuccess: (result) => {
      deleted.current = true;
      if (result.file_removed) finish();
      else setFileLeft(true); // say so before the row disappears
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) setError(t("doc.deleteBusy"));
      else setError(errorText(e, t("common.error")));
    },
  });

  if (fileLeft) {
    return (
      <>
        <p role="status" className="text-sm">
          {t("doc.deleteFileLeft")}
        </p>
        <div>
          <Button type="button" data-autofocus onClick={finish}>
            {t("common.close")}
          </Button>
        </div>
      </>
    );
  }
  return (
    <>
      <p className="text-sm [overflow-wrap:anywhere]">{t("doc.deleteBody", { title: doc.title })}</p>
      {error && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="danger" onClick={() => remove.mutate()} disabled={remove.isPending}>
          {t("doc.deleteYes")}
        </Button>
        <Button type="button" variant="ghost" data-autofocus onClick={onClose}>
          {t("common.cancel")}
        </Button>
      </div>
    </>
  );
}
