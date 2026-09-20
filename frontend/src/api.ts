/** Thin fetch wrapper around the FastAPI backend (same origin, cookie session). */

export interface FieldError {
  field: string;
  message: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public fields: FieldError[] = [],
    /** Seconds to wait (Retry-After), set on 429. */
    public retryAfter: number | null = null,
    /** Classified cause from the Paperless helpers (409 / 502), e.g. "connection". */
    public errorKind: string | null = null,
    /** 409 from an upload: the document that already holds the same file. */
    public documentId: number | null = null,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    credentials: "same-origin",
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });
  if (!response.ok) {
    let detail: unknown = response.statusText;
    let documentId: number | null = null;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
      if (typeof body.document_id === "number") documentId = body.document_id;
    } catch {
      /* keep the status text */
    }
    if (Array.isArray(detail)) {
      // validation errors: [{field, message}] from our routes, [{loc, msg}] from FastAPI
      const fields = detail.map((d) => ({
        field: d.field ?? (Array.isArray(d.loc) ? d.loc.slice(1).join(".") : ""),
        message: d.message ?? d.msg ?? "",
      }));
      throw new ApiError(
        response.status,
        fields.map((f) => `${f.field}: ${f.message}`).join("; "),
        fields,
      );
    }
    const retry = Number(response.headers.get("Retry-After"));
    const retryAfter = Number.isFinite(retry) && retry > 0 ? retry : null;
    if (detail && typeof detail === "object") {
      // {error_kind, error} from the Paperless helpers
      const d = detail as { error?: unknown; error_kind?: unknown };
      throw new ApiError(
        response.status,
        typeof d.error === "string" ? d.error : String(response.status),
        [],
        retryAfter,
        typeof d.error_kind === "string" ? d.error_kind : null,
      );
    }
    throw new ApiError(response.status, String(detail || response.status), [], retryAfter, null, documentId);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

// --- shapes returned by the backend ---------------------------------------

export type DocumentKind = "blood_test" | "report" | "prescription" | "imaging" | "other";

export type DocumentSource = "paperless" | "upload";

export interface MedicalDocument {
  id: number;
  source: DocumentSource;
  /** null for an upload. */
  paperless_id: number | null;
  title: string;
  kind: DocumentKind;
  doc_date: string | null;
  /** Hidden by the user because it is not really medical. */
  ignored: boolean;
  /** Where the document opens in Paperless; null for an upload. */
  paperless_link: string | null;
  /** Uploads: the cleaned name of the file as it was sent (user-controlled text, show as text only). */
  original_filename: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  /** A preview / thumbnail can be shown (an upload whose file went missing: false). */
  has_file: boolean;
  /** "text": a narrative report (imaging, opinion, prescription): findings and a conclusion, no lab values to review. */
  read_mode: "lab" | "text";
  /** How it was last read: "<kind|auto|manual>:<lab|report>"; "" when never (or before migration 4). */
  read_route: string;
  /** Kind "other": the reading can be switched between lab and report by hand. */
  can_switch: boolean;
  reading: ReadingState | null;
  /** A text report that has been read: how its automatic summary went; null otherwise. */
  report: ReportBrief | null;
}

export interface ReportBrief {
  /** "ok", "failed" (the text is kept, no summary) or "empty". */
  status: "" | "ok" | "failed" | "empty";
  findings: number;
  /** The start of the automatic conclusion (list line). */
  conclusion: string;
  /** Kind-specific facts for the list line; empty/0 when the document has none (or was read before they existed). */
  modality: "" | Modality;
  regions: string[];
  diagnosis: string;
  medications: number;
}

export type Modality = "ultrasound" | "mri" | "ct" | "xray" | "other";

export interface Measurement {
  label: string;
  value: string;
  unit: string;
}

export interface Medication {
  name: string;
  active_substance: string;
  strength: string;
  dose_instruction: string;
  duration_or_quantity: string;
  /** Fields the model gave that the text did not confirm: they were left out. */
  unverified: string[];
}

/** What the kind-specific extraction found. Every key is optional: {} for a summary written before it existed. */
export interface ReportFields {
  modality?: "" | Modality;
  regions?: string[];
  measurements?: Measurement[];
  doctor?: string;
  specialty?: string;
  diagnoses?: string[];
  recommendations?: string[];
  follow_up?: { text?: string; date?: string };
  prescriber?: string;
  date?: string;
  medications?: Medication[];
  /** Medication entries that could not be confirmed in the text at all. */
  dropped_medications?: number;
  /** "prescriber" / "date" when given but not confirmed. */
  unverified?: string[];
}

export interface DocumentPatch {
  title?: string;
  kind?: DocumentKind;
  /** null clears the date. */
  doc_date?: string | null;
}

export interface UploadOptions {
  kind: DocumentKind;
  title: string;
  /** YYYY-MM-DD, empty when not known. */
  docDate: string;
  readNow: boolean;
}

export interface UploadResult {
  document: MedicalDocument;
  read_queued: boolean;
}

export interface UploadsSettings {
  enabled: boolean;
  max_file_mb: number;
}

export interface UploadsSettingsResponse {
  values: UploadsSettings;
  sources: Record<keyof UploadsSettings, SettingSource>;
}

/**
 * POST /api/documents/upload with progress. XMLHttpRequest, because fetch cannot report upload progress.
 * The browser sets the multipart Content-Type and the Origin header itself, and sends the session cookie.
 * `onProgress` gets 0..1 of the bytes handed to the network; 1 means the server is now checking the file.
 */
export function uploadDocument(
  file: File,
  options: UploadOptions,
  onProgress: (fraction: number) => void,
): { promise: Promise<UploadResult>; abort: () => void } {
  const xhr = new XMLHttpRequest();
  let aborted = false;
  const promise = new Promise<UploadResult>((resolve, reject) => {
    const form = new FormData();
    form.append("kind", options.kind);
    if (options.title.trim()) form.append("title", options.title.trim());
    if (options.docDate) form.append("doc_date", options.docDate);
    form.append("read_now", options.readNow ? "true" : "false");
    form.append("file", file, file.name);

    xhr.open("POST", "/api/documents/upload");
    xhr.withCredentials = true;
    xhr.responseType = "text";
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) onProgress(Math.min(1, e.loaded / e.total));
    };
    xhr.upload.onload = () => onProgress(1);
    xhr.onload = () => {
      let body: { detail?: unknown; document_id?: unknown } | null = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON: keep the status */
      }
      if (xhr.status >= 200 && xhr.status < 300 && body) return resolve(body as unknown as UploadResult);
      const retry = Number(xhr.getResponseHeader("Retry-After"));
      const detail = body?.detail;
      const message = Array.isArray(detail)
        ? detail.map((d) => d?.message ?? d?.msg ?? "").filter(Boolean).join("; ")
        : typeof detail === "string"
          ? detail
          : xhr.statusText || String(xhr.status);
      reject(
        new ApiError(
          xhr.status,
          message,
          [],
          Number.isFinite(retry) && retry > 0 ? retry : null,
          null,
          typeof body?.document_id === "number" ? body.document_id : null,
        ),
      );
    };
    // status 0: no answer at all (offline, reset while sending). The caller decides what it likely was.
    xhr.onerror = () => reject(new ApiError(0, "network"));
    xhr.ontimeout = () => reject(new ApiError(0, "timeout"));
    xhr.onabort = () => reject(new ApiError(-1, aborted ? "aborted" : "network"));
    xhr.send(form);
  });
  return {
    promise,
    abort: () => {
      aborted = true;
      xhr.abort();
    },
  };
}

export type ReadingStage = "" | "queued" | "download" | "reader_a" | "reader_b" | "verify" | "transcribe" | "summary";

/** Where a document stands with reading its lab values (computed on the server). */
export interface ReadingState {
  state: "not_read" | "queued" | "reading" | "done" | "error";
  verified: number;
  needs_review: number;
  approved: number;
  error: string;
  page: number;
  pages: number;
  stage: ReadingStage;
}

export interface AuthStatus {
  password_required: boolean;
  authenticated: boolean;
  password_set: boolean;
  /** No password yet: setting the first one needs the code from the server log / data folder. */
  setup_code_required: boolean;
  /** "open_legacy": no password, kept open by VIBEHEALTH_LEGACY_OPEN=1. "needs_setup": no password, nothing works yet. */
  mode: "open_legacy" | "password" | "needs_setup";
  language: "en" | "el";
  default_language: "en" | "el";
}

/** GET /api/setup/status: public, so only what the guide and the login page need. */
export interface SetupStatus {
  /** "needs_setup": a fresh install, the API answers 403 "setup_required" until a password is set. */
  state: "needs_setup" | "ready";
  password_set: boolean;
  needs_setup_code: boolean;
  /** The guide opens by itself: no password yet, or a password and the guide not finished once. */
  wizard_pending: boolean;
}

export interface ReadinessModel {
  role: "reader_a" | "reader_b";
  name: string;
  installed: boolean;
  pull_command: string;
}

/** GET /api/reading/readiness */
export interface ReadinessResult {
  ollama: { reachable: boolean; version: string; url: string };
  models: ReadinessModel[];
  ready: boolean;
  missing: string[];
}

/** POST /api/reading/detect-ollama: which of a fixed list of usual addresses answer like Ollama. */
export interface DetectResult {
  found: { url: string; version: string }[];
}

export interface SystemStatus {
  documents: { total: number; ignored: number };
  worker: {
    running: boolean;
    last_sync: string | null;
    last_sync_result: { seen: number; created: number; updated: number } | null;
    /** When the next automatic sync is due (server local time), null when there is none. */
    next_sync: string | null;
    last_error: string | null;
    reading: ReadingWorker;
  };
  paperless_url: string;
  paperless: {
    enabled: boolean;
    configured: boolean;
    sync_active: boolean;
    skipped: null | "disabled" | "not_configured";
    /** 0: only "Sync now". */
    sync_interval_minutes: number;
  };
  uploads: {
    enabled: boolean;
    max_mb: number;
    /** No password set yet: uploads are refused until the app is protected. */
    password_required: boolean;
  };
}

// --- Paperless settings ------------------------------------------------------

export type SettingSource = "app" | "env" | "default";
export type MatchMode = "type_or_tags" | "type_only" | "tags_only";

export interface PaperlessValues {
  enabled: boolean;
  url: string;
  public_url: string;
  token_set: boolean;
  token_last4: string;
  token_source: SettingSource;
  document_type: string;
  tags: string[];
  match: MatchMode;
  kind_map: Record<string, DocumentKind>;
  sync_interval_minutes: number;
}

export interface PaperlessSettingsResponse {
  values: PaperlessValues;
  sources: Record<
    "enabled" | "url" | "public_url" | "document_type" | "tags" | "match" | "kind_map" | "sync_interval_minutes",
    SettingSource
  >;
}

export interface PaperlessTestResult {
  ok: boolean;
  version: string | null;
  error_kind: "connection" | "unauthorized" | "not_found" | "tls" | "timeout" | "other" | null;
  error: string | null;
}

export interface PaperlessName {
  id: number;
  name: string;
  count: number;
}

export interface PaperlessDiscovery {
  document_types: PaperlessName[];
  tags: PaperlessName[];
  total: { document_types: number; tags: number };
  truncated: { document_types: boolean; tags: boolean };
}

export interface PaperlessPreview {
  count: number;
  empty_selection: boolean;
}

export type ValueStatus = "verified" | "needs_review" | "approved" | "rejected";

export interface ExtractedValue {
  id: number;
  test_code: string | null;
  name_en: string;
  name_el: string;
  raw_name: string;
  value: string;
  value_num: number | null;
  unit: string;
  ref_range: string;
  flag: "" | "H" | "L";
  status: ValueStatus;
  reason: string;
  page: number | null;
  /** What each reader read, for comparison. */
  reader_a: string | null;
  reader_b: string | null;
}

export interface RunSummary {
  id: number;
  status: "running" | "done" | "error" | "interrupted" | "cleared";
  started_at: string;
  duration_s: number | null;
  pages: number;
  page_errors: { page: number; reader: "A" | "B"; error: string }[];
  error: string;
  verified: number;
  needs_review: number;
  kept_approved: number;
  route: string;
}

export interface DocumentValues {
  document: MedicalDocument;
  last_run: RunSummary | null;
  values: ExtractedValue[];
}

export interface CatalogTest {
  code: string;
  name_en: string;
  name_el: string;
  specimen: "blood" | "urine";
  category: string;
  unit: string;
}

export interface ReadingSettings {
  enabled: boolean;
  ollama_url: string;
  reader_a_model: string;
  reader_b_enabled: boolean;
  reader_b_model: string;
  dpi: number;
  fallback_dpis: number[];
  num_ctx: number;
  timeout_seconds: number;
  keep_alive: string;
  use_paperless_text: boolean;
  auto_read_after_sync: boolean;
}

export interface ReadingSettingsResponse {
  settings: ReadingSettings;
  defaults: ReadingSettings;
}

export interface ConnectionResult {
  ok: boolean;
  url: string;
  models: string[];
  error: string;
}

// --- models and the model check -----------------------------------------------

export type ModelStatus = "tested" | "untested" | "not_recommended" | "no_vision";

export interface ModelInfo {
  name: string;
  /** The project measured it (also true for the ones it does not recommend). */
  tested: boolean;
  status: ModelStatus;
  role: "reader_a" | "reader_b" | null;
  /** English; the UI shows its own translation, chosen by `key`. */
  note: string;
  key: string;
  /** What Ollama says about images; null when it does not say. */
  vision: boolean | null;
  usable: boolean;
  /** Looks like glm-ocr: the only kind of model reader B's parser can read. */
  glm_like: boolean;
}

/** The counts kept from a model's last check: never values, names or the file. */
export interface ModelCheckSummary {
  date: string;
  pages: number;
  seconds_per_page: number | null;
  rows_found: number;
  expected_count: number;
  matched: number;
  wrong_value: number;
  extra: number | null;
  gpu_percent: number | null;
}

export interface ModelsResponse {
  ok: boolean;
  error: string;
  models: ModelInfo[];
  checks: Record<string, ModelCheckSummary>;
}

export interface ModelCheckResult {
  model: string;
  pages: number;
  seconds_per_page: number;
  rows_found: number;
  used_reader_b: boolean;
  verified_by_two_readers: number | null;
  expected_count: number;
  matched: number;
  wrong_value: number;
  missing: number;
  extra_not_in_expected: number | null;
  gpu: { size_vram_percent: number } | null;
  warnings: string[];
}

export interface ModelCheckStatus {
  current: { model: string; stage: string; page: number; pages: number; started_at: string } | null;
  last:
    | ({ model: string; finished_at: string } & (
        | { status: "done"; result: ModelCheckResult }
        | { status: "error"; error: string }
      ))
    | null;
}

/** POST /api/reading/model-check: the file and the options go as multipart/form-data. 202 when it has started. */
export async function startModelCheck(
  file: File,
  options: { model: string; useReaderB: boolean; expected: string },
): Promise<void> {
  const form = new FormData();
  form.append("model", options.model);
  form.append("use_reader_b", options.useReaderB ? "true" : "false");
  if (options.expected.trim()) form.append("expected", options.expected);
  form.append("file", file, file.name);
  let response: Response;
  try {
    // no Content-Type: the browser writes the multipart one, with its boundary
    response = await fetch("/api/reading/model-check", { method: "POST", body: form, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "network");
  }
  if (response.ok) return;
  let detail = response.statusText;
  try {
    const body = await response.json();
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    /* keep the status text */
  }
  throw new ApiError(response.status, detail || String(response.status));
}

export interface FlaggedValue {
  id: number;
  document_id: number;
  document_title: string;
  doc_date: string | null;
  test_code: string;
  name_en: string;
  name_el: string;
  value: string;
  unit: string;
  ref_range: string;
  flag: "" | "H" | "L";
}

export interface CategoryCount {
  kind: DocumentKind;
  count: number;
  last_date: string | null;
}

export interface DashboardSummary {
  recent_documents: MedicalDocument[];
  flagged_values: FlaggedValue[];
  category_counts: CategoryCount[];
  has_any_approved: boolean;
}

/** A single historical reading of a test (used for `latest`, `history`, and paired `secondary` values). */
export interface TestHistoryPoint {
  document_id: number;
  document_title: string;
  doc_date: string | null;
  value: string;
  value_num: number | null;
  unit: string;
  ref_range: string;
  flag: "" | "H" | "L";
}

export interface ExamTest {
  test_code: string;
  name_en: string;
  name_el: string;
  value: string;
  value_num: number | null;
  unit: string;
  ref_range: string;
  flag: "" | "H" | "L";
  doc_date: string | null;
  document_id: number;
  /** Paired value shown alongside this one, e.g. Neutrophils % row also carries the absolute count. */
  secondary?: TestHistoryPoint | null;
}

export interface ExamCategory {
  category: string;
  tests: ExamTest[];
}

export interface ExaminationsResponse {
  kind: DocumentKind;
  categories: ExamCategory[] | null;
  documents: MedicalDocument[] | null;
}

export type TestValuePoint = TestHistoryPoint;

export interface TestByCode {
  test_code: string;
  name_en: string;
  name_el: string;
  category: string;
  latest: TestValuePoint;
  history: TestValuePoint[];
  /** Paired value for the latest reading, e.g. the absolute count next to a % test. */
  secondary?: TestHistoryPoint | null;
  /** History of the paired value, parallel to `history`. */
  secondary_history?: TestHistoryPoint[] | null;
}

export interface ReadingWorker {
  current: {
    document_id: number;
    title: string;
    stage: ReadingStage;
    page: number;
    pages: number;
    started_at: string;
  } | null;
  queue: number[];
  last_run:
    | ({ document_id: number; title: string } & Partial<RunSummary> & { status: string })
    | null;
  last_error: string | null;
}

/** GET /api/documents/{id}/report: what was read from a text report. */
export interface DocumentReport {
  document: MedicalDocument;
  /** null until the document has been read as a text report. */
  report: ReportDetail | null;
}

export interface ReportDetail {
  /** Always true: written by a local model, not by the report's author. The original prevails. */
  auto: boolean;
  summary_status: "" | "ok" | "failed" | "empty";
  summary_error: string;
  summary_model: string;
  conclusion: string;
  key_findings: string[];
  details: ReportFields;
  /** Pages whose text looks like a table of lab results. */
  lab_pages: number[];
  updated_at: string;
  pages: { page: number; text: string }[];
}

/** GET /api/dashboard/medications: an automatic list from recent prescriptions. */
export interface CurrentMedication extends Omit<Medication, "unverified"> {
  unverified: string[];
  /** The prescription's date (YYYY-MM-DD). */
  date: string;
  document_id: number;
  document_title: string;
  earlier_dates: string[];
}

export interface CurrentMedications {
  days: number;
  medications: CurrentMedication[];
  /** Prescriptions with medications but no date, and ones older than `days`: not in the list. */
  undated: number;
  older: number;
}

/** GET /api/search: `before` + `match` + `after` are cut from the original text. */
export interface SearchSnippet {
  field: string;
  page: number | null;
  before: string;
  match: string;
  after: string;
}

export interface SearchResults {
  query: string;
  results: { document: MedicalDocument; snippets: SearchSnippet[] }[];
}
