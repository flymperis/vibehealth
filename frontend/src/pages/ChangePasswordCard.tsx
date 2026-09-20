import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type AuthStatus } from "../api";
import { useI18n } from "../i18n";
import { Button, Card, Input } from "../ui";

const MIN_LENGTH = 10;

/**
 * Change the app password. With no password set yet it sets the first one.
 * `embedded` (the setup guide): just the form, without its own card and heading; `onSaved` gets
 * the new password so the guide can keep it in memory for the steps that ask for it.
 */
export default function ChangePasswordCard({
  embedded = false,
  onSaved,
}: {
  embedded?: boolean;
  onSaved?: (password: string) => void;
} = {}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const { data: auth } = useQuery({
    queryKey: ["auth"],
    queryFn: () => api.get<AuthStatus>("/auth/status"),
  });
  const hasPassword = !!auth?.password_set;

  const [current, setCurrent] = useState("");
  const [setupCode, setSetupCode] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const change = useMutation({
    mutationFn: () => api.post("/auth/change-password", hasPassword ? { current, new: next } : { setup_code: setupCode, new: next }),
    onSuccess: () => {
      onSaved?.(next);
      setCurrent("");
      setSetupCode("");
      setNext("");
      setConfirm("");
      setError("");
      setSaved(true);
      queryClient.invalidateQueries({ queryKey: ["auth"] });
    },
    onError: (e: unknown) => {
      setSaved(false);
      if (e instanceof ApiError && e.status === 403) setError(t(hasPassword ? "pw.wrongCurrent" : "pw.wrongCode"));
      else if (e instanceof ApiError && e.status === 429) {
        setError(t("login.locked", { wait: formatWait(e.retryAfter ?? 30, t) }));
      } else setError(e instanceof Error ? e.message : String(e));
    },
  });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    setSaved(false);
    if (next.length < MIN_LENGTH) return setError(t("pw.tooShort"));
    if (next !== confirm) return setError(t("pw.mismatch"));
    setError("");
    change.mutate();
  }

  const form = (
      <form onSubmit={submit} className="flex flex-col gap-3">
        {!embedded && <h2 className="text-sm font-semibold">{hasPassword ? t("pw.title") : t("pw.titleFirst")}</h2>}
        {!embedded && auth && !hasPassword && <p className="text-xs muted">{t("pw.firstHint")}</p>}
        {hasPassword ? (
          <Input
            type="password"
            autoComplete="current-password"
            placeholder={t("pw.current")}
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        ) : (
          auth && (
            <>
              <Input
                type="text"
                autoComplete="off"
                autoCapitalize="characters"
                spellCheck={false}
                maxLength={32}
                placeholder={t("pw.setupCode")}
                aria-label={t("pw.setupCode")}
                value={setupCode}
                onChange={(e) => setSetupCode(e.target.value)}
              />
              {!embedded && <p className="text-xs muted">{t("pw.setupCodeHelp")}</p>}
            </>
          )
        )}
        <Input
          type="password"
          autoComplete="new-password"
          placeholder={t("pw.new")}
          value={next}
          onChange={(e) => setNext(e.target.value)}
        />
        <Input
          type="password"
          autoComplete="new-password"
          placeholder={t("pw.confirm")}
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
        {error && <p className="text-sm text-red-600">{error}</p>}
        {saved && <p className="text-sm text-emerald-600">{t("pw.saved")}</p>}
        <Button
          type="submit"
          disabled={
            change.isPending || !next || !confirm || (hasPassword ? !current : !setupCode.trim())
          }
        >
          {hasPassword ? t("pw.submit") : t("pw.submitFirst")}
        </Button>
      </form>
  );
  if (embedded) return form;
  return (
    <Card id="set-password" className="p-4">
      {form}
    </Card>
  );
}

/** "45 s" or "3 min", for a wait in seconds. */
export function formatWait(
  seconds: number,
  t: (key: "wait.seconds" | "wait.minutes", vars: Record<string, number>) => string,
) {
  return seconds < 90 ? t("wait.seconds", { n: seconds }) : t("wait.minutes", { n: Math.ceil(seconds / 60) });
}
