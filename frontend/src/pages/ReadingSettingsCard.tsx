import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  api,
  type AuthStatus,
  type ConnectionResult,
  type ModelInfo,
  type ReadingSettings,
  type ReadingSettingsResponse,
  type ReadingWorker,
} from "../api";
import { useI18n } from "../i18n";
import { stageText } from "../reading";
import { Button, Card, Input, Select } from "../ui";
import { ModelNotes, badgeText, findModel, useModels } from "./ModelCheckCard";

type Key = Parameters<ReturnType<typeof useI18n>["t"]>[0];

function Field({ label, help, error, children }: { label: Key; help: Key; error?: string; children: ReactNode }) {
  const { t } = useI18n();
  return (
    <label className="flex flex-col gap-1">
      <span className="text-sm font-medium">{t(label)}</span>
      {children}
      <span className="text-xs muted">{t(help)}</span>
      {error && <span className="text-xs text-red-600">{error}</span>}
    </label>
  );
}

function Toggle({
  label,
  help,
  checked,
  onChange,
}: {
  label: Key;
  help: Key;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  const { t } = useI18n();
  return (
    <label className="flex items-start gap-3">
      <input
        type="checkbox"
        className="mt-1 size-5 shrink-0 accent-emerald-600"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="flex flex-col gap-0.5">
        <span className="text-sm font-medium">{t(label)}</span>
        <span className="text-xs muted">{t(help)}</span>
      </span>
    </label>
  );
}

function ModelSelect({
  value,
  models,
  known,
  onChange,
  disabled,
}: {
  value: string;
  models: string[] | undefined;
  /** What the project knows about each installed model: shown as a word after its name. */
  known: ModelInfo[] | undefined;
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  const { t } = useI18n();
  if (!models?.length) {
    // No list (Ollama not reached): still allow typing a name.
    return <Input value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled} />;
  }
  // "glm-ocr" and "glm-ocr:latest" are the same model to Ollama.
  const shown = models.includes(value) ? value : models.includes(`${value}:latest`) ? `${value}:latest` : value;
  const installed = models.includes(shown);
  return (
    <Select value={shown} onChange={(e) => onChange(e.target.value)} disabled={disabled}>
      {!installed && (
        <option value={value}>
          {value} ({t("rs.notInstalled")})
        </option>
      )}
      {models.map((m) => {
        const info = findModel(known, m);
        return (
          <option key={m} value={m}>
            {info ? `${m} (${badgeText(t, info.status)})` : m}
          </option>
        );
      })}
    </Select>
  );
}

export default function ReadingSettingsCard({ worker }: { worker: ReadingWorker | undefined }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ["reading-settings"],
    queryFn: () => api.get<ReadingSettingsResponse>("/reading/settings"),
  });
  const [form, setForm] = useState<ReadingSettings | null>(null);
  const [dpis, setDpis] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  const [connection, setConnection] = useState<ConnectionResult | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const { data: auth } = useQuery({
    queryKey: ["auth"],
    queryFn: () => api.get<AuthStatus>("/auth/status"),
  });
  const hasPassword = !!auth?.password_set;
  const { data: known } = useModels();

  const test = useMutation({
    mutationFn: (url: string) =>
      api.post<ConnectionResult>("/reading/test-connection", {
        ollama_url: url,
        // trying another address needs the password, like saving it
        ...(hasPassword && data && url !== data.settings.ollama_url ? { current_password: confirmation } : {}),
      }),
    onSuccess: setConnection,
    onError: (e: Error) => setConnection({ ok: false, url: "", models: [], error: e.message }),
  });

  const load = (s: ReadingSettings) => {
    setForm(s);
    setDpis(s.fallback_dpis.join(", "));
  };

  useEffect(() => {
    if (data && !form) {
      load(data.settings);
      test.mutate(data.settings.ollama_url); // fills the model lists
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  const save = useMutation({
    mutationFn: (body: ReadingSettings & { current_password?: string }) =>
      api.put<ReadingSettingsResponse>("/reading/settings", body),
    onSuccess: (r) => {
      load(r.settings);
      setConfirmation("");
      setErrors({});
      setSaved(true);
      queryClient.setQueryData(["reading-settings"], r);
      queryClient.invalidateQueries({ queryKey: ["reading-models"] }); // (the saved address may have changed)
      queryClient.invalidateQueries({ queryKey: ["documents"] });
    },
    onError: (e: Error) => {
      const fields: Record<string, string> = {};
      if (e instanceof ApiError && e.fields.length) {
        for (const f of e.fields) fields[f.field.split(".")[0] || "_"] = f.message;
      } else {
        fields._ = e.message || t("common.error");
      }
      setErrors(fields);
    },
  });

  if (!form || !data) return null;

  const update = <K extends keyof ReadingSettings>(key: K, value: ReadingSettings[K]) => {
    setForm({ ...form, [key]: value });
    setSaved(false);
  };
  const submit = () => {
    const parsed = dpis.split(",").map((x) => x.trim()).filter(Boolean).map(Number);
    if (parsed.some((n) => !Number.isInteger(n))) {
      setErrors({ fallback_dpis: t("rs.fallbackInvalid") });
      return;
    }
    // A new Ollama address is a security-relevant change: the server asks for the password.
    save.mutate({ ...form, fallback_dpis: parsed, ...(urlChanged ? { current_password: confirmation } : {}) });
  };
  const models = connection?.ok ? connection.models : undefined;
  const urlChanged = form.ollama_url !== data.settings.ollama_url;
  const number = (key: "dpi" | "num_ctx" | "timeout_seconds") => (
    <Input
      type="number"
      inputMode="numeric"
      value={Number.isNaN(form[key]) ? "" : form[key]}
      onChange={(e) => update(key, e.target.valueAsNumber)}
    />
  );

  return (
    <Card className="flex flex-col gap-4 p-4">
      <h2 className="text-sm font-semibold">{t("rs.title")}</h2>

      <WorkerStatus worker={worker} />

      <Toggle label="rs.enabled" help="rs.enabledHelp" checked={form.enabled} onChange={(v) => update("enabled", v)} />

      <Field label="rs.url" help="rs.urlHelp" error={errors.ollama_url}>
        <div className="flex flex-col gap-2 sm:flex-row">
          <Input
            value={form.ollama_url}
            onChange={(e) => update("ollama_url", e.target.value)}
            disabled={!hasPassword}
          />
          <Button
            type="button"
            variant="ghost"
            className="shrink-0"
            onClick={(e) => {
              e.preventDefault();
              test.mutate(form.ollama_url);
            }}
            disabled={test.isPending}
          >
            {t("rs.test")}
          </Button>
        </div>
      </Field>
      {auth && !hasPassword && <p className="-mt-2 text-xs muted">{t("rs.urlLocked")}</p>}
      {hasPassword && urlChanged && (
        <Field label="rs.confirmPassword" help="rs.urlHelp" error={errors.current_password}>
          <Input
            type="password"
            autoComplete="current-password"
            value={confirmation}
            onChange={(e) => setConfirmation(e.target.value)}
          />
        </Field>
      )}
      {connection && (
        <p className={`-mt-2 text-xs ${connection.ok ? "text-emerald-600" : "text-red-600"}`}>
          {connection.ok ? t("rs.testOk", { n: connection.models.length }) : connection.error}
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-2">
          <Field label="rs.readerA" help="rs.readerAHelp" error={errors.reader_a_model}>
            <ModelSelect
              value={form.reader_a_model}
              models={models}
              known={known?.models}
              onChange={(v) => update("reader_a_model", v)}
            />
          </Field>
          <ModelNotes name={form.reader_a_model} reader="A" models={known?.models} checks={known?.checks} />
        </div>
        <div className="flex min-w-0 flex-col gap-2">
          <Field label="rs.readerB" help="rs.readerBHelp" error={errors.reader_b_model}>
            <ModelSelect
              value={form.reader_b_model}
              models={models}
              known={known?.models}
              onChange={(v) => update("reader_b_model", v)}
              disabled={!form.reader_b_enabled}
            />
          </Field>
          {form.reader_b_enabled && (
            <ModelNotes
              name={form.reader_b_model}
              reader="B"
              models={known?.models}
              checks={known?.checks}
            />
          )}
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              className="size-4 accent-emerald-600"
              checked={form.reader_b_enabled}
              onChange={(e) => update("reader_b_enabled", e.target.checked)}
            />
            {t("rs.readerBEnabled")}
          </label>
        </div>
        <Field label="rs.dpi" help="rs.dpiHelp" error={errors.dpi}>
          {number("dpi")}
        </Field>
        <Field label="rs.fallback" help="rs.fallbackHelp" error={errors.fallback_dpis}>
          <Input
            value={dpis}
            inputMode="numeric"
            onChange={(e) => {
              setDpis(e.target.value);
              setSaved(false);
            }}
          />
        </Field>
        <Field label="rs.numCtx" help="rs.numCtxHelp" error={errors.num_ctx}>
          {number("num_ctx")}
        </Field>
        <Field label="rs.timeout" help="rs.timeoutHelp" error={errors.timeout_seconds}>
          {number("timeout_seconds")}
        </Field>
        <Field label="rs.keepAlive" help="rs.keepAliveHelp" error={errors.keep_alive}>
          <Input value={form.keep_alive} onChange={(e) => update("keep_alive", e.target.value)} />
        </Field>
      </div>

      <Toggle
        label="rs.paperlessText"
        help="rs.paperlessTextHelp"
        checked={form.use_paperless_text}
        onChange={(v) => update("use_paperless_text", v)}
      />
      <Toggle
        label="rs.autoRead"
        help="rs.autoReadHelp"
        checked={form.auto_read_after_sync}
        onChange={(v) => update("auto_read_after_sync", v)}
      />

      {errors._ && <p className="text-xs text-red-600">{errors._}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={submit} disabled={save.isPending}>
          {t("common.save")}
        </Button>
        <Button variant="ghost" onClick={() => { load(data.defaults); setSaved(false); }}>
          {t("rs.defaults")}
        </Button>
        {saved && <span className="text-xs text-emerald-600">{t("rs.saved")}</span>}
      </div>
    </Card>
  );
}

function WorkerStatus({ worker }: { worker: ReadingWorker | undefined }) {
  const { t } = useI18n();
  if (!worker) return null;
  const { current, queue, last_run: last } = worker;
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-xl p-3 text-xs" style={{ background: "var(--bg)" }}>
      <span className="font-medium">{t("rs.queue")}</span>
      {current ? (
        <Link to={`/documents/${current.document_id}`} className="text-emerald-600 [overflow-wrap:anywhere]">
          {t("rs.current", {
            title: current.title || `#${current.document_id}`,
            stage: stageText(t, current.stage),
            page: current.page,
            pages: current.pages,
          })}
        </Link>
      ) : (
        <span className="muted">{t("rs.idle")}</span>
      )}
      {queue.length > 0 && <span className="muted">{t("rs.waiting", { n: queue.length })}</span>}
      {last && (
        <Link
          to={`/documents/${last.document_id}`}
          className={`[overflow-wrap:anywhere] ${last.status === "error" ? "text-red-600" : "muted"}`}
        >
          {t("rs.last", {
            title: last.title || `#${last.document_id}`,
            result:
              last.status === "error"
                ? last.error || t("reading.error")
                : t("rs.lastDone", {
                    verified: last.verified ?? 0,
                    review: last.needs_review ?? 0,
                    secs: Math.round(last.duration_s ?? 0),
                  }),
          })}
        </Link>
      )}
    </div>
  );
}
