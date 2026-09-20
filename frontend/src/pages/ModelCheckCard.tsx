import { useEffect, useId, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  api,
  startModelCheck,
  type AuthStatus,
  type ModelCheckResult,
  type ModelCheckStatus,
  type ModelCheckSummary,
  type ModelInfo,
  type ModelStatus,
  type ModelsResponse,
  type ReadingSettingsResponse,
  type SystemStatus,
} from "../api";
import { useI18n } from "../i18n";
import { ACCEPT, ACCEPT_PHOTOS, precheck } from "../upload";
import { Button, Card, Select, formatBytes, formatDate, goToPasswordCard } from "../ui";

type T = ReturnType<typeof useI18n>["t"];
type Key = Parameters<T>[0];

const WRAP = "[overflow-wrap:anywhere]";

// --- shared with the Reading card: what is known about each installed model ----------------------------------

export function useModels() {
  return useQuery({
    queryKey: ["reading-models"],
    queryFn: () => api.get<ModelsResponse>("/reading/models"),
    staleTime: 30_000,
  });
}

/** "glm-ocr" and "glm-ocr:latest" are the same model to Ollama. */
export function findModel(models: ModelInfo[] | undefined, name: string): ModelInfo | undefined {
  return models?.find((m) => m.name === name) ?? models?.find((m) => m.name === `${name}:latest`);
}

const BADGE: Record<ModelStatus, string> = {
  tested: "bg-emerald-500/15 text-emerald-800 dark:text-emerald-300",
  untested: "bg-amber-500/15 text-amber-900 dark:text-amber-200",
  not_recommended: "bg-red-500/10 text-red-700 dark:text-red-300",
  no_vision: "bg-red-500/10 text-red-700 dark:text-red-300",
};

export function badgeText(t: T, status: ModelStatus) {
  return t(`mc.badge.${status}` as Key);
}

export function ModelBadge({ status }: { status: ModelStatus }) {
  const { t } = useI18n();
  return (
    <span className={`inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-xs font-medium ${BADGE[status]}`}>
      {badgeText(t, status)}
    </span>
  );
}

/** "3 of 4 known values right, 1 possibly invented, 12 s per page, GPU 100%": counts only. */
export function summaryLine(t: T, c: ModelCheckSummary) {
  const parts = [
    c.expected_count > 0
      ? t("mc.sum.known", { matched: c.matched, n: c.expected_count })
      : t("mc.sum.rows", { n: c.rows_found }),
  ];
  if (c.extra != null) parts.push(t("mc.sum.extra", { n: c.extra }));
  if (c.seconds_per_page != null) parts.push(t("mc.sum.secs", { n: c.seconds_per_page }));
  if (c.gpu_percent != null) parts.push(t("mc.sum.gpu", { n: c.gpu_percent }));
  return parts.join(", ");
}

/** The badge, the project's note, the warning that fits and the last check, for the model chosen in a dropdown. */
export function ModelNotes({
  name,
  reader,
  models,
  checks,
  readerBOn = true,
}: {
  name: string;
  reader: "A" | "B";
  models: ModelInfo[] | undefined;
  checks: Record<string, ModelCheckSummary> | undefined;
  readerBOn?: boolean;
}) {
  const { t } = useI18n();
  const info = findModel(models, name);
  if (!info) return null;
  const last = checks?.[info.name];
  const warnings: Key[] = [];
  if (info.status === "untested") warnings.push("mc.warn.untested");
  if (info.status === "no_vision") warnings.push("mc.warn.no_vision");
  if (reader === "B" && readerBOn && !info.glm_like) warnings.push("mc.warn.readerB");
  if (reader === "A" && info.glm_like) warnings.push("mc.warn.readerAOcr");
  return (
    <div className="flex min-w-0 flex-col gap-1 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <ModelBadge status={info.status} />
        {(info.status === "tested" || info.status === "not_recommended") && (
          <span className={`muted ${WRAP}`}>{t(`mc.note.${info.key}` as Key)}</span>
        )}
      </div>
      {warnings.map((w) => (
        <p key={w} role="note" className="text-amber-800 dark:text-amber-300">
          {t(w)}
        </p>
      ))}
      {last && (
        <p className="muted">
          {t("mc.lastCheck", { date: formatDate(last.date), summary: summaryLine(t, last) })}
        </p>
      )}
    </div>
  );
}

// --- the card -------------------------------------------------------------------------------------------------

const STAGES = ["receiving", "starting", "rendering", "reader_a", "reader_b", "verify"];

export default function ModelCheckCard() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const ids = { model: useId(), expected: useId(), readerB: useId() };
  const picker = useRef<HTMLInputElement>(null);
  const camera = useRef<HTMLInputElement>(null);
  const { data: auth } = useQuery({ queryKey: ["auth"], queryFn: () => api.get<AuthStatus>("/auth/status") });
  const { data: status } = useQuery({ queryKey: ["status"], queryFn: () => api.get<SystemStatus>("/status") });
  const { data: settings } = useQuery({
    queryKey: ["reading-settings"],
    queryFn: () => api.get<ReadingSettingsResponse>("/reading/settings"),
  });
  const { data: known } = useModels();
  const needsPassword = !!auth && !auth.password_set;
  const maxMb = status?.uploads.max_mb ?? 50;

  const [model, setModel] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [expected, setExpected] = useState("");
  const [useReaderB, setUseReaderB] = useState(false);
  const [error, setError] = useState("");

  const usable = (known?.models ?? []).filter((m) => m.usable);
  useEffect(() => {
    if (model || !usable.length) return;
    const preferred = findModel(usable, settings?.settings.reader_a_model ?? "");
    setModel((preferred ?? usable[0]).name);
  }, [model, usable, settings]);

  const run = useMutation({
    mutationFn: () => startModelCheck(file as File, { model, useReaderB, expected }),
    onSuccess: () => {
      setError("");
      queryClient.invalidateQueries({ queryKey: ["model-check"] });
    },
    onError: (e: unknown) => {
      queryClient.invalidateQueries({ queryKey: ["model-check"] });
      if (!(e instanceof ApiError)) return setError(t("mc.err.other"));
      const byStatus: Record<number, Key> = {
        0: "mc.err.network",
        401: "mc.err.session",
        403: "mc.err.password",
        409: "mc.err.busy",
        413: "mc.err.tooLarge",
        415: "mc.err.type",
      };
      // 422 and 502 carry a reason that only the server knows (which model, what was wrong with it)
      setError(byStatus[e.status] ? t(byStatus[e.status]) : e.status === 422 || e.status === 502 ? e.message : t("mc.err.other"));
    },
  });

  const { data: check } = useQuery({
    queryKey: ["model-check"],
    queryFn: () => api.get<ModelCheckStatus>("/reading/model-check"),
    refetchInterval: (query) => (run.isPending || query.state.data?.current ? 2000 : false),
  });
  const running = run.isPending || !!check?.current;

  // A finished check changes what the Reading card shows next to the model: look again.
  const finished = check?.last?.finished_at;
  useEffect(() => {
    if (finished) queryClient.invalidateQueries({ queryKey: ["reading-models"] });
  }, [finished, queryClient]);

  const problem = file ? precheck(file, maxMb) : null;
  const problemText = problem
    ? problem === "type"
      ? t("up.problem.type")
      : problem === "size"
        ? t("up.problem.size", { max: maxMb })
        : t("up.problem.empty")
    : "";
  const canRun = !needsPassword && !running && !!file && !problem && !!model;
  const readerBModel = settings?.settings.reader_b_model ?? "glm-ocr";

  const pick = (files: FileList | null) => {
    if (files?.[0]) {
      setFile(files[0]);
      setError("");
    }
  };

  return (
    <Card id="model-check" className="p-4">
      <form
        noValidate
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (canRun) run.mutate();
        }}
      >
        <div className="flex flex-col gap-1">
          <h2 className="text-sm font-semibold">{t("mc.title")}</h2>
          <p className="text-xs muted">{t("mc.intro")}</p>
        </div>

        {needsPassword && (
          <div role="note" className="flex flex-col items-start gap-2 rounded-xl bg-amber-500/10 p-3 text-sm text-amber-900 dark:text-amber-200">
            <p>{t("mc.passwordWhy")}</p>
            <Button type="button" variant="ghost" onClick={goToPasswordCard}>
              {t("pw.titleFirst")}
            </Button>
          </div>
        )}

        <fieldset disabled={needsPassword} className="flex min-w-0 flex-col gap-4 border-0 p-0">
          <div className="flex flex-col gap-1">
            <label htmlFor={ids.model} className="text-sm font-medium">
              {t("mc.model")}
            </label>
            {usable.length ? (
              <Select id={ids.model} value={model} onChange={(e) => setModel(e.target.value)} disabled={running}>
                {usable.map((m) => (
                  <option key={m.name} value={m.name}>
                    {m.name} ({badgeText(t, m.status)})
                  </option>
                ))}
              </Select>
            ) : (
              <p className="text-xs muted">{known ? t("mc.noModels") : t("common.loading")}</p>
            )}
            <p className="text-xs muted">{t("mc.modelHelp")}</p>
            {model && <ModelNotes name={model} reader="A" models={known?.models} checks={known?.checks} />}
          </div>

          <div className="flex flex-col gap-2">
            <span className="text-sm font-medium">{t("mc.file")}</span>
            <input ref={picker} type="file" accept={ACCEPT} className="hidden" tabIndex={-1} aria-hidden="true"
              onChange={(e) => { pick(e.target.files); e.target.value = ""; }} />
            <input ref={camera} type="file" accept={ACCEPT_PHOTOS} capture="environment" className="hidden" tabIndex={-1}
              aria-hidden="true" onChange={(e) => { pick(e.target.files); e.target.value = ""; }} />
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" variant="ghost" disabled={running} onClick={() => picker.current?.click()}>
                {t("mc.choose")}
              </Button>
              <Button type="button" variant="ghost" className="sm:hidden" disabled={running} onClick={() => camera.current?.click()}>
                {t("mc.camera")}
              </Button>
              {file && (
                <span className={`min-w-0 text-sm ${WRAP}`}>
                  {file.name} <span className="text-xs muted">{formatBytes(file.size)}</span>
                </span>
              )}
            </div>
            {problemText && (
              <p role="alert" className="text-xs text-red-600 dark:text-red-400">
                {problemText}
              </p>
            )}
            <p className="text-xs muted">{t("mc.fileHelp", { max: maxMb })}</p>
          </div>

          <div className="flex flex-col gap-1">
            <label htmlFor={ids.expected} className="text-sm font-medium">
              {t("mc.expected")}
            </label>
            <textarea
              id={ids.expected}
              rows={4}
              value={expected}
              maxLength={12000}
              disabled={running}
              spellCheck={false}
              placeholder={t("mc.expectedPlaceholder")}
              onChange={(e) => setExpected(e.target.value)}
              className="surface min-h-24 w-full rounded-xl p-3 font-mono text-sm outline-none focus:ring-2 focus:ring-emerald-500/40"
            />
            <p className="text-xs muted">{t("mc.expectedHelp")}</p>
          </div>

          <div className="flex items-start gap-3">
            <input
              id={ids.readerB}
              type="checkbox"
              className="mt-1 size-5 shrink-0 accent-emerald-600"
              checked={useReaderB}
              disabled={running}
              onChange={(e) => setUseReaderB(e.target.checked)}
            />
            <div className="flex flex-col gap-0.5">
              <label htmlFor={ids.readerB} className="text-sm font-medium">
                {t("mc.readerB")}
              </label>
              <span className="text-xs muted">{t("mc.readerBHelp", { model: readerBModel })}</span>
            </div>
          </div>
        </fieldset>

        <p className="rounded-xl p-3 text-xs muted" style={{ background: "var(--bg)" }}>
          {t("mc.privacy")}
        </p>

        {error && (
          <p role="alert" className={`text-sm text-red-600 dark:text-red-400 ${WRAP}`}>
            {error}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <Button type="submit" disabled={!canRun}>
            {running ? t("mc.running") : t("mc.run")}
          </Button>
          {check?.current && <Progress current={check.current} />}
          {!check?.current && run.isPending && <span className="text-xs muted">{t("mc.stage.receiving")}</span>}
        </div>

        {!check?.current && check?.last?.status === "error" && (
          <p role="alert" className={`text-sm text-red-600 dark:text-red-400 ${WRAP}`}>
            {t("mc.failed", { error: check.last.error })}
          </p>
        )}
        {!check?.current && check?.last?.status === "done" && <Result result={check.last.result} />}

        <History models={known?.models} checks={known?.checks} />
      </form>
    </Card>
  );
}

function Progress({ current }: { current: NonNullable<ModelCheckStatus["current"]> }) {
  const { t } = useI18n();
  const stage = STAGES.includes(current.stage) ? t(`mc.stage.${current.stage}` as Key) : current.stage;
  const text = current.pages
    ? t("mc.progress", { model: current.model, stage, page: current.page, pages: current.pages })
    : t("mc.progressStart", { model: current.model, stage });
  const done = current.pages ? Math.max(0, current.page - 1) / current.pages : 0;
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1" role="status" aria-live="polite">
      <span className={`text-xs muted ${WRAP}`}>{text}</span>
      <div className="h-1.5 w-full max-w-64 overflow-hidden rounded-full bg-black/10 dark:bg-white/10">
        <div className="h-full rounded-full bg-emerald-600 transition-all" style={{ width: `${Math.round(done * 100)}%` }} />
      </div>
    </div>
  );
}

function Result({ result }: { result: ModelCheckResult }) {
  const { t } = useI18n();
  const known = result.expected_count > 0;
  const perfect = known && result.matched === result.expected_count && (result.extra_not_in_expected ?? 0) === 0;
  const verdict = !known
    ? t("mc.verdict.none", { rows: result.rows_found, pages: result.pages })
    : perfect
      ? t("mc.verdict.perfect", { n: result.expected_count })
      : t("mc.verdict.partial", {
          matched: result.matched,
          n: result.expected_count,
          extra: result.extra_not_in_expected ?? 0,
        });
  const stats: [Key, number | string][] = [
    ["mc.stat.pages", result.pages],
    ["mc.stat.rows", result.rows_found],
  ];
  if (known) {
    stats.push(
      ["mc.stat.matched", result.matched],
      ["mc.stat.wrong", result.wrong_value],
      ["mc.stat.missing", result.missing],
      ["mc.stat.extra", result.extra_not_in_expected ?? 0],
    );
  }
  if (result.verified_by_two_readers != null) stats.push(["mc.stat.two", result.verified_by_two_readers]);
  stats.push(["mc.stat.speed", result.seconds_per_page]);
  const percent = result.gpu?.size_vram_percent;
  // The server also words a partial GPU load as a note; it is shown here in the user's language instead.
  const notes = result.warnings.filter((w) => !/GPU memory/.test(w));
  return (
    <section aria-labelledby="mc-result" className="flex flex-col gap-3 rounded-xl p-3" style={{ background: "var(--bg)" }}>
      <h3 id="mc-result" className={`text-sm font-semibold ${WRAP}`}>
        {t("mc.result", { model: result.model })}
      </h3>
      <p
        role="status"
        className={`text-sm font-medium ${perfect ? "text-emerald-700 dark:text-emerald-400" : known ? "text-amber-800 dark:text-amber-300" : ""}`}
      >
        {verdict}
      </p>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {stats.map(([label, value]) => (
          <div key={label} className="surface min-w-0 rounded-xl p-2">
            <dt className={`text-xs muted ${WRAP}`}>{t(label)}</dt>
            <dd className="text-base font-semibold">{value}</dd>
          </div>
        ))}
      </dl>
      <p className={`text-xs ${percent != null && percent < 100 ? "text-amber-800 dark:text-amber-300" : "muted"}`}>
        {percent == null ? t("mc.gpu.unknown") : percent >= 100 ? t("mc.gpu.full") : t("mc.gpu.partial", { n: percent })}{" "}
        {t("mc.speedNote")}
      </p>
      {known && <p className="text-xs muted">{t("mc.extraNote")}</p>}
      {notes.length > 0 && (
        <div className="flex flex-col gap-1 text-xs">
          <span className="font-medium">{t("mc.warnings")}</span>
          <ul className="list-disc pl-4">
            {notes.map((w, i) => (
              <li key={i} className={`text-amber-800 dark:text-amber-300 ${WRAP}`}>
                {w}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function History({ models, checks }: { models: ModelInfo[] | undefined; checks: Record<string, ModelCheckSummary> | undefined }) {
  const { t } = useI18n();
  const rows = Object.entries(checks ?? {}).reverse(); // the server keeps them oldest first
  return (
    <section aria-labelledby="mc-history" className="flex flex-col gap-2">
      <h3 id="mc-history" className="text-sm font-semibold">
        {t("mc.history")}
      </h3>
      {rows.length ? (
        <ul className="flex flex-col gap-2">
          {rows.map(([name, c]) => (
            <li key={name} className="flex min-w-0 flex-col gap-0.5 rounded-xl p-2 text-xs" style={{ background: "var(--bg)" }}>
              <span className="flex flex-wrap items-center gap-2">
                <span className={`text-sm font-medium ${WRAP}`}>{name}</span>
                {findModel(models, name) && <ModelBadge status={findModel(models, name)!.status} />}
                <span className="muted">{formatDate(c.date)}</span>
              </span>
              <span className="muted">{summaryLine(t, c)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs muted">{t("mc.historyEmpty")}</p>
      )}
    </section>
  );
}
