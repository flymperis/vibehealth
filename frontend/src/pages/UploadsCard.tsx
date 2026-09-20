import { useEffect, useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, type AuthStatus, type UploadsSettings, type UploadsSettingsResponse } from "../api";
import { useI18n } from "../i18n";
import { Button, Card, Input, goToPasswordCard } from "../ui";

// The server's range for the size limit (settings_store.UploadsSection): it checks again.
const MIN_MB = 1;
const MAX_MB = 200;

/** Settings for adding documents by upload: on/off, the size limit, and what is kept where. */
export default function UploadsCard() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const ids = { enabled: useId(), max: useId(), maxHelp: useId(), maxErr: useId(), enabledHelp: useId() };
  const { data } = useQuery({
    queryKey: ["uploads-settings"],
    queryFn: () => api.get<UploadsSettingsResponse>("/settings/uploads"),
  });
  const { data: auth } = useQuery({
    queryKey: ["auth"],
    queryFn: () => api.get<AuthStatus>("/auth/status"),
  });
  const needsPassword = !!auth && !auth.password_set;

  const [enabled, setEnabled] = useState(true);
  const [maxMb, setMaxMb] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [saved, setSaved] = useState(false);
  const [errors, setErrors] = useState<{ max?: string; _?: string }>({});

  const load = (values: UploadsSettings) => {
    setEnabled(values.enabled);
    setMaxMb(String(values.max_file_mb));
  };
  useEffect(() => {
    if (data && !loaded) {
      load(data.values);
      setLoaded(true);
    }
  }, [data, loaded]);

  const parsed = Number(maxMb);
  const validMax = maxMb.trim() !== "" && Number.isInteger(parsed) && parsed >= MIN_MB && parsed <= MAX_MB;
  const dirty = !!data && (enabled !== data.values.enabled || (validMax ? parsed !== data.values.max_file_mb : maxMb !== String(data.values.max_file_mb)));

  const save = useMutation({
    mutationFn: (body: UploadsSettings) => api.put<UploadsSettingsResponse>("/settings/uploads", body),
    onSuccess: (r) => {
      queryClient.setQueryData(["uploads-settings"], r);
      load(r.values);
      setErrors({});
      setSaved(true);
      queryClient.invalidateQueries({ queryKey: ["status"] }); // the upload panel reads its limit from there
    },
    onError: (e: unknown) => {
      setSaved(false);
      if (e instanceof ApiError && e.fields.length) {
        const max = e.fields.find((f) => f.field.split(".")[0] === "max_file_mb");
        setErrors({ max: max?.message, _: max ? undefined : e.message });
      } else setErrors({ _: e instanceof Error ? e.message : t("common.error") });
    },
  });

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    setSaved(false);
    if (!validMax) return setErrors({ max: t("ul.maxInvalid", { min: MIN_MB, max: MAX_MB }) });
    setErrors({});
    save.mutate({ enabled, max_file_mb: parsed });
  };

  return (
    <Card id="uploads" className="p-4">
      <form onSubmit={submit} noValidate className="flex flex-col gap-4" aria-labelledby={`${ids.enabled}-title`}>
        <div className="flex flex-col gap-1">
          <h2 id={`${ids.enabled}-title`} className="text-sm font-semibold">
            {t("ul.title")}
          </h2>
          <p className="text-xs muted">{t("ul.intro")}</p>
        </div>

        {needsPassword && (
          <div role="note" className="flex flex-col items-start gap-2 rounded-xl bg-amber-500/10 p-3 text-sm text-amber-900 dark:text-amber-200">
            <p>{t("ul.password")} {t("up.off.password")}</p>
            <Button type="button" variant="ghost" onClick={goToPasswordCard}>
              {t("pw.titleFirst")}
            </Button>
          </div>
        )}

        {data ? (
          <>
            <div className="flex items-start gap-3">
              <input
                id={ids.enabled}
                type="checkbox"
                className="mt-1 size-5 shrink-0 accent-emerald-600"
                aria-describedby={ids.enabledHelp}
                checked={enabled}
                onChange={(e) => {
                  setEnabled(e.target.checked);
                  setSaved(false);
                }}
              />
              <div className="flex flex-col gap-0.5">
                <label htmlFor={ids.enabled} className="text-sm font-medium">
                  {t("ul.enabled")}
                </label>
                <span id={ids.enabledHelp} className="text-xs muted">
                  {t("ul.enabledHelp")}
                </span>
              </div>
            </div>

            <div className="flex flex-col gap-1">
              <label htmlFor={ids.max} className="text-sm font-medium">
                {t("ul.maxMb")}
              </label>
              <Input
                id={ids.max}
                type="number"
                inputMode="numeric"
                min={MIN_MB}
                max={MAX_MB}
                step={1}
                value={maxMb}
                aria-invalid={errors.max ? true : undefined}
                aria-describedby={`${ids.maxHelp}${errors.max ? ` ${ids.maxErr}` : ""}`}
                onChange={(e) => {
                  setMaxMb(e.target.value);
                  setSaved(false);
                  setErrors({});
                }}
                className="sm:max-w-40"
              />
              <p id={ids.maxHelp} className="text-xs muted">
                {t("ul.maxHelp", { min: MIN_MB, max: MAX_MB })}
              </p>
              {errors.max && (
                <p id={ids.maxErr} role="alert" className="text-xs text-red-600 dark:text-red-400">
                  {errors.max}
                </p>
              )}
            </div>

            <p className="rounded-xl p-3 text-xs muted" style={{ background: "var(--bg)" }}>
              {t("ul.stored")}
            </p>

            {errors._ && (
              <p role="alert" className="text-sm text-red-600 dark:text-red-400">
                {errors._}
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <Button type="submit" disabled={!dirty || save.isPending}>
                {save.isPending ? t("pl.saving") : t("common.save")}
              </Button>
              <Button
                type="button"
                variant="ghost"
                disabled={!dirty || save.isPending}
                onClick={() => {
                  if (data) load(data.values);
                  setErrors({});
                  setSaved(false);
                }}
              >
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
          </>
        ) : (
          <p className="text-xs muted">{t("common.loading")}</p>
        )}
      </form>
    </Card>
  );
}
