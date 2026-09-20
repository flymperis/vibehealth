import { useEffect, useId, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  api,
  type ConnectionResult,
  type DetectResult,
  type PaperlessSettingsResponse,
  type ReadinessResult,
  type ReadingSettingsResponse,
  type SetupStatus,
  type SystemStatus,
  type UploadsSettingsResponse,
} from "../api";
import { useI18n, type Lang } from "../i18n";
import { useUploads } from "../upload";
import { Button, Card, Input } from "../ui";
import ChangePasswordCard, { formatWait } from "./ChangePasswordCard";
import PaperlessCard from "./PaperlessCard";

type Key = Parameters<ReturnType<typeof useI18n>["t"]>[0];
type StepId = "welcome" | "password" | "sources" | "ollama" | "done";

const STEP_LABEL: Record<StepId, Key> = {
  welcome: "wz.steps.welcome",
  password: "wz.steps.password",
  sources: "wz.steps.sources",
  ollama: "wz.steps.ollama",
  done: "wz.steps.done",
};

/** Set for the rest of the browser session when the guide is left early: it must not pull the person back in. */
export const SETUP_EXIT_KEY = "vibehealth-setup-exit";

const trimUrl = (s: string) => s.trim().replace(/\/+$/, "");

/**
 * The first-run guide. What is done is read from the server (a reload continues where it was); the
 * one thing kept only in memory is the password just chosen, which the address fields need once as
 * `current_password`. It is never written anywhere.
 */
export default function Setup() {
  const { t, lang } = useI18n();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { setPanelOpen } = useUploads();

  const { data: setup } = useQuery({ queryKey: ["setup"], queryFn: () => api.get<SetupStatus>("/setup/status") });
  const passwordSet = !!setup?.password_set;

  // The list of steps is fixed when the guide opens: with no password yet there is a step for it, and it
  // stays (showing "done") once it is set, so the progress does not jump under the person's finger.
  const [withPassword] = useState(() => !setup?.password_set);
  const steps: StepId[] = withPassword
    ? ["welcome", "password", "sources", "ollama", "done"]
    : ["welcome", "sources", "ollama", "done"];
  // Started before (a password exists, nothing else is set up): carry on from the sources.
  const [index, setIndex] = useState(() => (setup?.password_set && setup.wizard_pending ? 1 : 0));
  const step = steps[index];
  const [password, setPassword] = useState<string | undefined>(undefined);

  // The session exists once a password is set (the first-password call signs this browser in).
  const session = passwordSet;
  const paperless = useQuery({
    queryKey: ["paperless-settings"],
    queryFn: () => api.get<PaperlessSettingsResponse>("/settings/paperless"),
    enabled: session,
  });
  const uploads = useQuery({
    queryKey: ["uploads-settings"],
    queryFn: () => api.get<UploadsSettingsResponse>("/settings/uploads"),
    enabled: session,
  });
  const readiness = useQuery({
    queryKey: ["readiness"],
    queryFn: () => api.get<ReadinessResult>("/reading/readiness"),
    enabled: session && (step === "ollama" || step === "done"),
    retry: false,
    staleTime: 0,
  });

  const paperlessConnected = !!paperless.data?.values.url && !!paperless.data?.values.token_set;
  const uploadsOn = uploads.data?.values.enabled ?? false;
  const canNext =
    step === "password" ? passwordSet : step === "sources" ? uploadsOn || paperlessConnected : true;
  const nextLabel: Key = step === "ollama" && !readiness.data?.ready ? "wz.ol.skip" : "wz.next";

  const leave = () => {
    sessionStorage.setItem(SETUP_EXIT_KEY, "1");
    navigate("/");
  };

  return (
    <div className="mx-auto flex min-h-full w-full max-w-xl flex-col gap-4 px-4 py-6">
      <header className="flex items-center justify-between gap-3">
        <span className="text-lg font-semibold tracking-tight">{t("app.name")}</span>
        {setup?.state === "ready" && (
          <button
            type="button"
            onClick={leave}
            className="inline-flex min-h-9 items-center rounded px-1 text-sm muted underline underline-offset-2"
          >
            {t("wz.exit")}
          </button>
        )}
      </header>

      <Progress steps={steps} index={index} />

      <Card className="flex min-w-0 flex-col gap-4 p-4 sm:p-6">
        {step === "welcome" && <WelcomeStep passwordSet={passwordSet} />}
        {step === "password" && (
          <PasswordStep
            passwordSet={passwordSet}
            onSaved={(pw) => {
              setPassword(pw);
              queryClient.invalidateQueries({ queryKey: ["setup"] });
              queryClient.invalidateQueries({ queryKey: ["auth"] });
              // the server's language for a new device (best effort: the guide does not depend on it)
              api.put("/settings/general", { language: lang }).catch(() => {});
            }}
          />
        )}
        {step === "sources" && (
          <SourcesStep
            password={password}
            uploadsOn={uploadsOn}
            paperlessConnected={paperlessConnected}
          />
        )}
        {step === "ollama" && <OllamaStep password={password} readiness={readiness} />}
        {step === "done" && (
          <DoneStep
            paperlessConnected={paperlessConnected}
            uploadsOn={uploadsOn}
            readiness={readiness.data}
            onFinish={async (openUploads) => {
              await api.post("/setup/complete");
              setPassword(undefined);
              sessionStorage.setItem(SETUP_EXIT_KEY, "1");
              // the flag must be known before the dashboard asks, or it would send us back here
              await queryClient.invalidateQueries({ queryKey: ["setup"] });
              await queryClient.invalidateQueries({ queryKey: ["status"] });
              navigate("/");
              if (openUploads) setPanelOpen(true);
            }}
          />
        )}
      </Card>

      {step !== "done" && (
        <div className="flex items-center justify-between gap-3">
          <Button variant="ghost" onClick={() => setIndex(index - 1)} disabled={index === 0}>
            {t("wz.back")}
          </Button>
          <Button onClick={() => setIndex(index + 1)} disabled={!canNext}>
            {t(nextLabel)}
          </Button>
        </div>
      )}
      {step === "done" && (
        <div className="flex">
          <Button variant="ghost" onClick={() => setIndex(index - 1)}>
            {t("wz.back")}
          </Button>
        </div>
      )}
    </div>
  );
}

// --- progress ----------------------------------------------------------------------

function Progress({ steps, index }: { steps: StepId[]; index: number }) {
  const { t } = useI18n();
  return (
    <nav aria-label={t("wz.title")} className="flex flex-col gap-2">
      <p className="text-xs muted" aria-live="polite">
        {t("wz.step", { n: index + 1, total: steps.length })} · {t(STEP_LABEL[steps[index]])}
      </p>
      <ol className="flex gap-1.5">
        {steps.map((s, i) => (
          <li
            key={s}
            aria-current={i === index ? "step" : undefined}
            className={`h-1.5 flex-1 rounded-full ${i <= index ? "bg-emerald-600" : "bg-black/10 dark:bg-white/10"}`}
          >
            <span className="sr-only">
              {t(STEP_LABEL[s])}
              {i < index ? ` (${t("wz.stepDone")})` : ""}
            </span>
          </li>
        ))}
      </ol>
    </nav>
  );
}

function Heading({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
      {children && <p className="text-sm muted">{children}</p>}
    </div>
  );
}

function Tick({ ok, pending }: { ok: boolean; pending?: boolean }) {
  if (pending) {
    return <span className="mt-1 size-4 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent muted" aria-hidden="true" />;
  }
  return (
    <span
      aria-hidden="true"
      className={`mt-0.5 inline-flex size-5 shrink-0 items-center justify-center rounded-full text-xs font-bold ${
        ok ? "bg-emerald-600/15 text-emerald-700 dark:text-emerald-400" : "bg-amber-500/20 text-amber-800 dark:text-amber-300"
      }`}
    >
      {ok ? "✓" : "!"}
    </span>
  );
}

// --- 1. welcome ---------------------------------------------------------------------

function WelcomeStep({ passwordSet }: { passwordSet: boolean }) {
  const { t, lang, setLang } = useI18n();
  const options: { code: Lang; label: string }[] = [
    { code: "en", label: "English" },
    { code: "el", label: "Ελληνικά" },
  ];
  return (
    <>
      <Heading title={t("wz.welcome.title")}>{t("wz.welcome.intro")}</Heading>
      <p className="text-sm">{t("wz.welcome.private")}</p>
      <div role="group" aria-label={t("wz.welcome.lang")} className="flex flex-col gap-2">
        <span className="text-sm font-medium">{t("wz.welcome.lang")}</span>
        <div className="grid grid-cols-2 gap-2">
          {options.map((o) => (
            <button
              key={o.code}
              type="button"
              lang={o.code}
              aria-pressed={lang === o.code}
              onClick={() => setLang(o.code)}
              className={`min-h-11 rounded-xl px-3 text-sm font-medium transition ${
                lang === o.code ? "bg-emerald-600 text-white" : "surface hover:bg-black/5 dark:hover:bg-white/5"
              }`}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>
      {passwordSet && <p className="text-xs muted">{t("wz.welcome.hasPassword")}</p>}
    </>
  );
}

// --- 2. password ---------------------------------------------------------------------

function PasswordStep({ passwordSet, onSaved }: { passwordSet: boolean; onSaved: (password: string) => void }) {
  const { t } = useI18n();
  if (passwordSet) {
    return (
      <>
        <Heading title={t("wz.pw.title")} />
        <div className="flex items-start gap-3 text-sm" role="status">
          <Tick ok />
          <span>{t("wz.pw.done")}</span>
        </div>
      </>
    );
  }
  return (
    <>
      <Heading title={t("wz.pw.title")}>{t("wz.pw.intro")}</Heading>
      <p className="rounded-xl p-3 text-xs muted" style={{ background: "var(--bg)" }}>
        {t("wz.pw.codeHint")}
      </p>
      <ChangePasswordCard embedded onSaved={onSaved} />
    </>
  );
}

// --- 3. sources ----------------------------------------------------------------------

function SourceChoice({
  id,
  checked,
  onChange,
  label,
  help,
  disabled,
}: {
  id: string;
  checked: boolean;
  onChange: (on: boolean) => void;
  label: Key;
  help: Key;
  disabled?: boolean;
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
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <div className="flex min-w-0 flex-col gap-0.5">
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

function SourcesStep({
  password,
  uploadsOn,
  paperlessConnected,
}: {
  password: string | undefined;
  uploadsOn: boolean;
  paperlessConnected: boolean;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const uid = useId();
  const [wantPaperless, setWantPaperless] = useState(paperlessConnected);
  // the saved settings arrive after a session exists: show the Paperless form once they say it is set up
  useEffect(() => {
    if (paperlessConnected) setWantPaperless(true);
  }, [paperlessConnected]);
  const { data: status } = useQuery({ queryKey: ["status"], queryFn: () => api.get<SystemStatus>("/status") });

  const setUploads = useMutation({
    mutationFn: (enabled: boolean) => api.put<UploadsSettingsResponse>("/settings/uploads", { enabled }),
    onSuccess: (r) => {
      queryClient.setQueryData(["uploads-settings"], r);
      queryClient.invalidateQueries({ queryKey: ["status"] });
      queryClient.invalidateQueries({ queryKey: ["setup"] });
    },
  });

  return (
    <>
      <Heading title={t("wz.src.title")}>{t("wz.src.intro")}</Heading>

      <SourceChoice
        id={`${uid}-uploads`}
        checked={uploadsOn}
        onChange={(on) => setUploads.mutate(on)}
        disabled={setUploads.isPending}
        label="wz.src.uploads"
        help="wz.src.uploadsHelp"
      />
      {setUploads.isError && (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          {t("wz.src.uploadsError")}
        </p>
      )}

      <SourceChoice
        id={`${uid}-paperless`}
        checked={wantPaperless}
        onChange={setWantPaperless}
        label="wz.src.paperless"
        help="wz.src.paperlessHelp"
      />
      {wantPaperless && (
        <div className="flex min-w-0 flex-col gap-3 border-t pt-4" style={{ borderColor: "var(--border)" }}>
          {!paperlessConnected && <p className="text-xs muted">{t("wz.src.paperlessPending")}</p>}
          <PaperlessCard status={status} embedded currentPassword={password} />
        </div>
      )}

      {!uploadsOn && !paperlessConnected && (
        <p role="status" className="text-sm text-amber-800 dark:text-amber-300">
          {t("wz.src.needOne")}
        </p>
      )}
    </>
  );
}

// --- 4. Ollama -----------------------------------------------------------------------

function legacyCopy(text: string): boolean {
  // navigator.clipboard only exists on https and localhost; a phone on the home network is neither
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  document.body.removeChild(area);
  return ok;
}

function CopyCommand({ command }: { command: string }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const h = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(h);
  }, [copied]);
  async function copy() {
    let ok = false;
    try {
      await navigator.clipboard.writeText(command);
      ok = true;
    } catch {
      ok = legacyCopy(command);
    }
    setCopied(ok);
  }
  return (
    <div className="flex items-center gap-2">
      <code className="surface min-w-0 flex-1 select-all break-all rounded-lg px-2.5 py-2 font-mono text-xs">{command}</code>
      <Button type="button" variant="ghost" className="min-h-10 shrink-0 px-3 text-xs" onClick={copy}>
        <span aria-live="polite">{copied ? t("wz.ol.copied") : t("wz.ol.copy")}</span>
      </Button>
    </div>
  );
}

function Check({ ok, pending, children }: { ok: boolean; pending?: boolean; children: ReactNode }) {
  return (
    <li className="flex min-w-0 items-start gap-3 text-sm">
      <Tick ok={ok} pending={pending} />
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">{children}</div>
    </li>
  );
}

function OllamaStep({
  password,
  readiness,
}: {
  password: string | undefined;
  readiness: { data: ReadinessResult | undefined; isFetching: boolean; isError: boolean; refetch: () => unknown };
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const uid = useId();
  const { data: reading } = useQuery({
    queryKey: ["reading-settings"],
    queryFn: () => api.get<ReadingSettingsResponse>("/reading/settings"),
  });
  const saved = reading?.settings.ollama_url ?? "";
  const [url, setUrl] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [typedPassword, setTypedPassword] = useState("");
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => {
    if (reading && !loaded) {
      setUrl(reading.settings.ollama_url);
      setLoaded(true);
    }
  }, [reading, loaded]);

  const changed = loaded && trimUrl(url) !== trimUrl(saved);
  const confirmation = password ?? typedPassword;
  const errorText = (e: unknown): string => {
    if (e instanceof ApiError && e.status === 429) return t("pl.err.rateLimited", { wait: formatWait(e.retryAfter ?? 30, t) });
    if (e instanceof ApiError && e.fields.some((f) => f.field === "current_password")) return t("pw.wrongCurrent");
    return e instanceof Error && e.message ? e.message : t("common.error");
  };

  const detect = useMutation({
    mutationFn: () => api.post<DetectResult>("/reading/detect-ollama"),
  });

  const test = useMutation({
    mutationFn: async () => {
      const r = await api.post<ConnectionResult>("/reading/test-connection", {
        ollama_url: url,
        ...(changed && confirmation ? { current_password: confirmation } : {}),
      });
      if (!r.ok) return r;
      if (reading && trimUrl(r.url) !== trimUrl(saved)) {
        if (!confirmation) throw new Error(t("pl.needPassword"));
        // the whole settings object goes back: a partial one would reset the rest to the defaults
        const s = await api.put<ReadingSettingsResponse>("/reading/settings", {
          ...reading.settings,
          ollama_url: r.url,
          current_password: confirmation,
        });
        queryClient.setQueryData(["reading-settings"], s);
        setUrl(s.settings.ollama_url);
      }
      return r;
    },
    onSuccess: (r) => {
      setNote(r.ok ? { ok: true, text: t("wz.ol.saved") } : { ok: false, text: r.error || t("common.error") });
      readiness.refetch();
    },
    onError: (e) => setNote({ ok: false, text: errorText(e) }),
  });

  const data = readiness.data;
  const checking = readiness.isFetching && !data;

  return (
    <>
      <Heading title={t("wz.ol.title")}>{t("wz.ol.intro")}</Heading>
      <p className="rounded-xl p-3 text-xs muted" style={{ background: "var(--bg)" }}>
        {t("wz.ol.hardware")}
      </p>

      <div className="flex min-w-0 flex-col gap-2">
        <label htmlFor={`${uid}-url`} className="text-sm font-medium">
          {t("wz.ol.url")}
        </label>
        <Input
          id={`${uid}-url`}
          type="url"
          inputMode="url"
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          value={url}
          onChange={(e) => {
            setUrl(e.target.value);
            setNote(null);
          }}
        />
        {changed && !password && (
          <>
            <label htmlFor={`${uid}-pw`} className="text-sm font-medium">
              {t("rs.confirmPassword")}
            </label>
            <Input
              id={`${uid}-pw`}
              type="password"
              autoComplete="current-password"
              value={typedPassword}
              onChange={(e) => setTypedPassword(e.target.value)}
            />
          </>
        )}
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="ghost" onClick={() => detect.mutate()} disabled={detect.isPending}>
            {detect.isPending ? t("wz.ol.detecting") : t("wz.ol.detect")}
          </Button>
          <Button type="button" onClick={() => test.mutate()} disabled={test.isPending || !url.trim()}>
            {test.isPending ? t("wz.ol.testing") : t("wz.ol.test")}
          </Button>
        </div>
        <p className="text-xs muted">{t("wz.ol.testHelp")}</p>

        <div aria-live="polite" className="flex flex-col gap-1.5 text-sm">
          {detect.data &&
            (detect.data.found.length === 0 ? (
              <p className="muted">{t("wz.ol.detectNone")}</p>
            ) : (
              detect.data.found.map((f) => (
                <div key={f.url} className="flex flex-wrap items-center gap-2">
                  <span className="min-w-0 break-all">{t("wz.ol.detectFound", { version: f.version, url: f.url })}</span>
                  <Button
                    type="button"
                    variant="ghost"
                    className="min-h-9 px-3 text-xs"
                    onClick={() => {
                      setUrl(f.url);
                      setNote(null);
                    }}
                  >
                    {t("wz.ol.use")}
                  </Button>
                </div>
              ))
            ))}
          {detect.isError && <p className="text-red-600 dark:text-red-400">{errorText(detect.error)}</p>}
          {note && (
            <p className={note.ok ? "text-emerald-700 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}>
              {note.text}
            </p>
          )}
        </div>
      </div>

      <div className="flex flex-col gap-3 border-t pt-4" style={{ borderColor: "var(--border)" }}>
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-sm font-semibold">{t("wz.ol.check")}</h2>
          <Button
            type="button"
            variant="ghost"
            className="min-h-9 px-3 text-xs"
            onClick={() => readiness.refetch()}
            disabled={readiness.isFetching}
          >
            {readiness.isFetching ? t("wz.ol.checking") : t("wz.ol.recheck")}
          </Button>
        </div>
        <ul className="flex flex-col gap-3" aria-live="polite">
          <Check ok={!!data?.ollama.reachable} pending={checking}>
            <span>
              {data?.ollama.reachable
                ? data.ollama.version
                  ? t("wz.ol.reachableVersion", { version: data.ollama.version })
                  : t("wz.ol.reachable")
                : t("wz.ol.unreachable")}
            </span>
          </Check>
          {data?.models.map((m) => (
            <Check key={m.role} ok={m.installed}>
              <span className="break-words">
                {t(m.role === "reader_a" ? "wz.ol.readerA" : "wz.ol.readerB", { name: m.name })} ·{" "}
                <span className="muted">{m.installed ? t("wz.ol.installed") : t("wz.ol.missing")}</span>
              </span>
              {!m.installed && (
                <>
                  <span className="text-xs muted">{t("wz.ol.pull")}</span>
                  <CopyCommand command={m.pull_command} />
                </>
              )}
            </Check>
          ))}
        </ul>
        <p className="text-xs muted">{t("wz.ol.skipHelp")}</p>
      </div>
    </>
  );
}

// --- 5. done -------------------------------------------------------------------------

function SummaryRow({ ok, label, value }: { ok: boolean; label: string; value: string }) {
  return (
    <li className="flex items-start gap-3 text-sm">
      <Tick ok={ok} />
      <span className="min-w-0 break-words">
        <span className="font-medium">{label}</span> · <span className="muted">{value}</span>
      </span>
    </li>
  );
}

function DoneStep({
  paperlessConnected,
  uploadsOn,
  readiness,
  onFinish,
}: {
  paperlessConnected: boolean;
  uploadsOn: boolean;
  readiness: ReadinessResult | undefined;
  onFinish: (openUploads: boolean) => Promise<void>;
}) {
  const { t } = useI18n();
  const finish = useMutation({ mutationFn: onFinish });
  const readingValue = !readiness
    ? "…"
    : !readiness.ollama.reachable
      ? t("wz.done.ollamaDown")
      : readiness.ready
        ? t("wz.done.ollamaReady")
        : t("wz.done.ollamaModels");
  return (
    <>
      <Heading title={t("wz.done.title")}>{t("wz.done.intro")}</Heading>
      <ul className="flex flex-col gap-3">
        <SummaryRow ok label={t("wz.done.password")} value={t("wz.done.passwordOk")} />
        <SummaryRow
          ok={paperlessConnected}
          label={t("wz.done.paperless")}
          value={paperlessConnected ? t("wz.done.connected") : t("wz.done.notConnected")}
        />
        <SummaryRow ok={uploadsOn} label={t("wz.done.uploads")} value={uploadsOn ? t("wz.done.on") : t("wz.done.off")} />
        <SummaryRow ok={!!readiness?.ready} label={t("wz.done.ollama")} value={readingValue} />
      </ul>
      {readiness && readiness.missing.length > 0 && (
        <div className="flex flex-col gap-2">
          <span className="text-sm font-medium">{t("wz.done.todo")}</span>
          {readiness.missing.map((command) => (
            <CopyCommand key={command} command={command} />
          ))}
        </div>
      )}
      <p className="text-xs muted">{t("wz.done.settings")}</p>
      {finish.isError && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {t("wz.done.error")}
        </p>
      )}
      <div className="flex flex-col gap-2 sm:flex-row">
        <Button onClick={() => finish.mutate(false)} disabled={finish.isPending}>
          {finish.isPending ? t("wz.done.saving") : t("wz.done.dashboard")}
        </Button>
        {uploadsOn && (
          <Button variant="ghost" onClick={() => finish.mutate(true)} disabled={finish.isPending}>
            {t("wz.done.first")}
          </Button>
        )}
      </div>
    </>
  );
}
