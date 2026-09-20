/**
 * The upload queue. It lives above the pages (in App), so an upload keeps going when you open a
 * document or another tab, and the panel shows the same rows when you come back.
 * Two files are sent at a time; a failed one stays in the list until it is retried or removed.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { ApiError, uploadDocument, type UploadOptions, type UploadResult } from "./api";

const MAX_PARALLEL = 2;

export const ACCEPT = "application/pdf,image/jpeg,image/png,image/webp";
/** The camera only offers photos; iOS turns them into JPEG when the accepted list has no HEIC. */
export const ACCEPT_PHOTOS = "image/jpeg,image/png,image/webp";
const EXTENSIONS = [".pdf", ".jpg", ".jpeg", ".png", ".webp"];
const MIME_TYPES = ["application/pdf", "image/jpeg", "image/png", "image/webp"];

/** Everything that can be shown to explain a failed row. Localised where it is shown, not here. */
export type Failure =
  | { kind: "duplicate"; documentId: number | null }
  | { kind: "password" }
  | { kind: "disabled" }
  | { kind: "forbidden"; detail: string }
  | { kind: "tooLarge" }
  | { kind: "type" }
  | { kind: "invalid"; detail: string }
  | { kind: "session" }
  | { kind: "rate"; seconds: number | null }
  | { kind: "network" }
  | { kind: "server"; status: number };

export type UploadState = "waiting" | "uploading" | "done" | "error";

export interface QueueItem {
  id: number;
  file: File;
  options: UploadOptions;
  state: UploadState;
  /** 0..1 of the bytes sent; at 1 the server is checking and storing the file. */
  progress: number;
  failure?: Failure;
  result?: UploadResult;
}

/**
 * Cheap checks before anything is sent. They only save a pointless upload: the server checks the bytes
 * again and has the last word.
 */
export function precheck(file: File, maxMb: number): "type" | "size" | "empty" | null {
  const name = file.name.toLowerCase();
  const typeOk = MIME_TYPES.includes(file.type.toLowerCase()) || EXTENSIONS.some((e) => name.endsWith(e));
  if (!typeOk) return "type";
  if (file.size === 0) return "empty";
  if (file.size > maxMb * 1024 * 1024) return "size";
  return null;
}

function classify(error: unknown, file: File, maxMb: number | undefined): Failure {
  if (!(error instanceof ApiError)) return { kind: "network" };
  const { status, message } = error;
  if (status === 0) {
    // The server may answer 413 and hang up while the body is still being sent; the browser then only
    // sees a dropped connection. A file over the limit is by far the likeliest cause.
    if (maxMb && file.size > maxMb * 1024 * 1024) return { kind: "tooLarge" };
    return { kind: "network" };
  }
  if (status === 401) return { kind: "session" };
  if (status === 403) {
    if (/password/i.test(message)) return { kind: "password" };
    if (/turned off/i.test(message)) return { kind: "disabled" };
    return { kind: "forbidden", detail: message };
  }
  if (status === 409) return { kind: "duplicate", documentId: error.documentId };
  if (status === 413) return { kind: "tooLarge" };
  if (status === 415) return { kind: "type" };
  if (status === 422) return { kind: "invalid", detail: message };
  if (status === 429) return { kind: "rate", seconds: error.retryAfter };
  return { kind: "server", status };
}

export function refreshDocumentQueries(queryClient: QueryClient) {
  for (const key of ["documents", "dashboard", "status", "examinations", "values-by-test", "values"]) {
    queryClient.invalidateQueries({ queryKey: [key] });
  }
}

interface UploadContextValue {
  items: QueueItem[];
  add: (files: File[], options: UploadOptions, maxMb: number) => void;
  retry: (id: number) => void;
  remove: (id: number) => void;
  clearFinished: () => void;
  /** Files that are waiting or on their way. */
  busy: number;
  /** The "Add documents" panel. One panel for the whole app, so it survives the page swapping its buttons. */
  panelOpen: boolean;
  setPanelOpen: (open: boolean) => void;
}

const UploadContext = createContext<UploadContextValue>({
  items: [],
  add: () => {},
  retry: () => {},
  remove: () => {},
  clearFinished: () => {},
  busy: 0,
  panelOpen: false,
  setPanelOpen: () => {},
});

export const useUploads = () => useContext(UploadContext);

export function UploadProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [items, setItems] = useState<QueueItem[]>([]);
  const [panelOpen, setPanelOpen] = useState(false);
  const nextId = useRef(1);
  const aborts = useRef(new Map<number, () => void>());
  const maxMbRef = useRef<number | undefined>(undefined);

  const patch = useCallback((id: number, changes: Partial<QueueItem>) => {
    setItems((current) => current.map((item) => (item.id === id ? { ...item, ...changes } : item)));
  }, []);

  const start = useCallback(
    (item: QueueItem) => {
      const { promise, abort } = uploadDocument(item.file, item.options, (progress) =>
        patch(item.id, { progress }),
      );
      aborts.current.set(item.id, abort);
      patch(item.id, { state: "uploading", progress: 0, failure: undefined });
      promise
        .then((result) => {
          patch(item.id, { state: "done", progress: 1, result });
          refreshDocumentQueries(queryClient);
        })
        .catch((error: unknown) => {
          if (error instanceof ApiError && error.status === -1 && error.message === "aborted") return;
          const failure = classify(error, item.file, maxMbRef.current);
          patch(item.id, { state: "error", failure });
          // What the panel believed (limit, password, on/off) may be out of date: look again.
          if (["tooLarge", "password", "disabled", "network"].includes(failure.kind)) {
            queryClient.invalidateQueries({ queryKey: ["status"] });
          }
        })
        .finally(() => aborts.current.delete(item.id));
    },
    [patch, queryClient],
  );

  // The scheduler: whenever the list changes, fill the free slots with the oldest waiting files.
  useEffect(() => {
    let free = MAX_PARALLEL - items.filter((i) => i.state === "uploading").length;
    for (const item of items) {
      if (free <= 0) break;
      if (item.state === "waiting" && !aborts.current.has(item.id)) {
        start(item);
        free -= 1;
      }
    }
  }, [items, start]);

  const busy = items.filter((i) => i.state === "waiting" || i.state === "uploading").length;

  // Leaving the page would cut the transfer: ask first (the browser shows its own wording).
  useEffect(() => {
    if (!busy) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [busy]);

  const value = useMemo<UploadContextValue>(
    () => ({
      items,
      busy,
      panelOpen,
      setPanelOpen,
      add: (files, options, maxMb) => {
        maxMbRef.current = maxMb;
        setItems((current) => [
          ...current,
          ...files.map((file) => ({
            id: nextId.current++,
            file,
            options,
            state: "waiting" as const,
            progress: 0,
          })),
        ]);
      },
      retry: (id) => patch(id, { state: "waiting", progress: 0, failure: undefined }),
      remove: (id) => {
        aborts.current.get(id)?.();
        aborts.current.delete(id);
        setItems((current) => current.filter((i) => i.id !== id));
      },
      clearFinished: () => setItems((current) => current.filter((i) => i.state !== "done")),
    }),
    [items, busy, panelOpen, patch],
  );

  return <UploadContext.Provider value={value}>{children}</UploadContext.Provider>;
}
