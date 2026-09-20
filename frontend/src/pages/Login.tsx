import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api";
import { useI18n } from "../i18n";
import { Button, Card, Input } from "../ui";
import { formatWait } from "./ChangePasswordCard";

export default function Login() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [password, setPassword] = useState("");
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  // Seconds left of a lockout (429 + Retry-After); counts down to zero.
  const [wait, setWait] = useState(0);

  useEffect(() => {
    if (wait <= 0) return;
    const timer = setTimeout(() => setWait((w) => w - 1), 1000);
    return () => clearTimeout(timer);
  }, [wait]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setFailed(false);
    try {
      await api.post("/auth/login", { password });
      await queryClient.invalidateQueries({ queryKey: ["auth"] });
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        setWait(e.retryAfter ?? 30);
      } else {
        setFailed(true);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <Card className="w-full max-w-sm p-6">
        <h1 className="mb-1 text-xl font-semibold">{t("app.name")}</h1>
        <p className="mb-5 text-sm muted">{t("login.title")}</p>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <Input
            type="password"
            autoFocus
            autoComplete="current-password"
            placeholder={t("login.password")}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {wait > 0 ? (
            <p className="text-sm text-red-600">{t("login.locked", { wait: formatWait(wait, t) })}</p>
          ) : (
            failed && <p className="text-sm text-red-600">{t("login.wrong")}</p>
          )}
          <Button type="submit" disabled={busy || !password || wait > 0}>
            {t("login.submit")}
          </Button>
        </form>
      </Card>
    </div>
  );
}
