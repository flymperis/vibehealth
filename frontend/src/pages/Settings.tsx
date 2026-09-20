import { useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, type AuthStatus, type SystemStatus } from "../api";
import { useTheme, type Theme } from "../App";
import { useI18n, type Lang } from "../i18n";
import { Button, Card, Select, goToPasswordCard } from "../ui";
import ChangePasswordCard from "./ChangePasswordCard";
import ModelCheckCard from "./ModelCheckCard";
import PaperlessCard from "./PaperlessCard";
import ReadingSettingsCard from "./ReadingSettingsCard";
import UploadsCard from "./UploadsCard";

export default function Settings() {
  const { t, lang, setLang } = useI18n();
  const { theme, setTheme } = useTheme();

  const { data: status } = useQuery({
    queryKey: ["status"],
    queryFn: () => api.get<SystemStatus>("/status"),
    // faster while a document is being read, for the progress line
    refetchInterval: (query) => (query.state.data?.worker.reading.current ? 3000 : 10_000),
  });

  const logout = useMutation({
    mutationFn: () => api.post("/auth/logout"),
    onSuccess: () => window.location.reload(),
  });

  const logoutAll = useMutation({
    mutationFn: () => api.post("/auth/logout-all"),
    onSuccess: () => window.location.reload(),
  });
  const { data: auth } = useQuery({
    queryKey: ["auth"],
    queryFn: () => api.get<AuthStatus>("/auth/status"),
  });

  // Links from other pages (/settings#set-password, /settings#uploads). The cards above load and grow
  // after the page appears, so wait until the target has stopped moving, then jump once. Touching the
  // page first cancels the jump: it must never fight the reader.
  const { hash } = useLocation();
  useEffect(() => {
    const id = hash.slice(1);
    if (id !== "set-password" && id !== "uploads") return;
    const started = performance.now();
    let lastTop = NaN;
    let stableSince = started;
    let frame = 0;
    const cancel = () => cancelAnimationFrame(frame);
    const jump = () => {
      if (id === "set-password") return goToPasswordCard();
      const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const card = document.getElementById("uploads");
      card?.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "start" });
      card?.querySelector<HTMLElement>("input")?.focus({ preventScroll: true });
    };
    const tick = () => {
      const now = performance.now();
      const el = document.getElementById(id);
      const top = el ? Math.round(el.getBoundingClientRect().top + window.scrollY) : NaN;
      if (top !== lastTop) {
        lastTop = top;
        stableSince = now;
      }
      if (el && (now - stableSince > 400 || now - started > 3000)) return jump();
      if (now - started > 4000) return;
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    for (const type of ["wheel", "touchstart", "keydown"]) window.addEventListener(type, cancel, { once: true, passive: true });
    return () => {
      cancel();
      for (const type of ["wheel", "touchstart", "keydown"]) window.removeEventListener(type, cancel);
    };
  }, [hash]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">{t("settings.title")}</h1>
        <Link
          to="/setup"
          className="inline-flex min-h-8 items-center text-sm font-medium text-emerald-700 underline underline-offset-2 hover:text-emerald-800 dark:text-emerald-400"
        >
          {t("settings.rerunSetup")}
        </Link>
      </div>

      <section aria-labelledby="sources-heading" className="flex flex-col gap-4">
        <h2 id="sources-heading" className="text-xs font-semibold uppercase tracking-wide muted">
          {t("settings.sources")}
        </h2>
        <PaperlessCard status={status} />
        <UploadsCard />
        <ReadingSettingsCard worker={status?.worker.reading} />
        <ModelCheckCard />
      </section>

      <ChangePasswordCard />

      <Card className="flex flex-col gap-3 p-4">
        <h2 className="text-sm font-semibold">{t("settings.language")}</h2>
        <Select value={lang} onChange={(e) => setLang(e.target.value as Lang)}>
          <option value="en">English</option>
          <option value="el">Ελληνικά</option>
        </Select>
        <h2 className="mt-2 text-sm font-semibold">{t("settings.theme")}</h2>
        <Select value={theme} onChange={(e) => setTheme(e.target.value as Theme)}>
          <option value="system">{t("settings.theme.system")}</option>
          <option value="light">{t("settings.theme.light")}</option>
          <option value="dark">{t("settings.theme.dark")}</option>
        </Select>
        <Button variant="ghost" className="mt-2" onClick={() => logout.mutate()}>
          {t("settings.logout")}
        </Button>
        {auth?.password_set && (
          <Button variant="ghost" onClick={() => logoutAll.mutate()}>
            {t("settings.logoutAll")}
          </Button>
        )}
      </Card>
    </div>
  );
}
