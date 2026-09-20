import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type AuthStatus, type SetupStatus } from "./api";
import { useI18n } from "./i18n";
import { Spinner } from "./ui";
import { UploadProvider } from "./upload";
import Dashboard from "./pages/Dashboard";
import Documents from "./pages/Documents";
import Examinations from "./pages/Examinations";
import DocumentReview from "./pages/DocumentReview";
import Settings from "./pages/Settings";
import Login from "./pages/Login";
import Setup, { SETUP_EXIT_KEY } from "./pages/Setup";
import { UploadDialog } from "./pages/AddDocuments";

// --- theme -----------------------------------------------------------------

export type Theme = "system" | "light" | "dark";
const ThemeContext = createContext<{ theme: Theme; setTheme: (t: Theme) => void }>({
  theme: "system",
  setTheme: () => {},
});
export const useTheme = () => useContext(ThemeContext);

function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem("theme") as Theme) || "system",
  );

  useEffect(() => {
    localStorage.setItem("theme", theme);
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      const dark = theme === "dark" || (theme === "system" && media.matches);
      document.documentElement.dataset.theme = dark ? "dark" : "light";
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);

  return <ThemeContext.Provider value={{ theme, setTheme }}>{children}</ThemeContext.Provider>;
}

// --- shell -----------------------------------------------------------------

const TABS = [
  { to: "/", key: "nav.home", icon: "M4 11l8-6 8 6M6 10v9h12v-9" },
  { to: "/examinations", key: "nav.examinations", icon: "M9 3h6M10 3v5l-5.5 9.5A2 2 0 0 0 6.2 21h11.6a2 2 0 0 0 1.7-3.5L14 8V3M8.5 14h7" },
  { to: "/documents", key: "nav.documents", icon: "M7 4h7l5 5v11H7zM14 4v5h5" },
  { to: "/settings", key: "nav.settings", icon: "M10 4h4l.6 2.4 2.1 1.2 2.3-.8 2 3.4-1.7 1.6v2.4l1.7 1.6-2 3.4-2.3-.8-2.1 1.2L14 20h-4l-.6-2.4-2.1-1.2-2.3.8-2-3.4L4.7 12v-2.4L3 8l2-3.4 2.3.8 2.1-1.2z" },
] as const;

function Icon({ path }: { path: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
         strokeLinecap="round" strokeLinejoin="round" className="size-6">
      <path d={path} />
    </svg>
  );
}

function Shell({ children }: { children: ReactNode }) {
  const { t, lang, setLang } = useI18n();
  const { pathname } = useLocation();

  const languageToggle = (
    <button
      onClick={() => setLang(lang === "en" ? "el" : "en")}
      className="surface rounded-lg px-2 py-1 text-xs font-medium uppercase"
    >
      {lang === "en" ? "ελ" : "en"}
    </button>
  );

  return (
    <div className="mx-auto flex min-h-full w-full max-w-5xl flex-col md:flex-row">
      <header
        className="sticky top-0 z-20 flex items-center justify-between gap-3 px-4 py-3
          backdrop-blur md:hidden"
        style={{ background: "color-mix(in srgb, var(--bg) 85%, transparent)" }}
      >
        <span className="text-lg font-semibold tracking-tight">{t("app.name")}</span>
        {languageToggle}
      </header>

      {/* Desktop rail */}
      <nav className="hidden w-52 shrink-0 flex-col gap-1 p-4 md:flex">
        <div className="mb-4 flex items-center justify-between">
          <span className="text-lg font-semibold tracking-tight">{t("app.name")}</span>
          {languageToggle}
        </div>
        {TABS.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.to === "/"}
            className={({ isActive }) =>
              `flex items-center gap-3 rounded-xl px-3 py-2 text-sm font-medium transition ${
                isActive ? "bg-emerald-600/10 text-emerald-600" : "muted hover:bg-black/5 dark:hover:bg-white/5"
              }`
            }
          >
            <Icon path={tab.icon} />
            {t(tab.key)}
          </NavLink>
        ))}
      </nav>

      <main className="min-w-0 flex-1 px-4 pb-28 md:pb-8 md:pt-6">{children}</main>

      {/* Mobile tab bar */}
      <nav
        className="fixed inset-x-0 bottom-0 z-20 flex justify-around border-t px-2 pt-2 backdrop-blur md:hidden"
        style={{
          background: "color-mix(in srgb, var(--surface) 92%, transparent)",
          borderColor: "var(--border)",
          paddingBottom: "calc(env(safe-area-inset-bottom, 0px) + 0.5rem)",
        }}
      >
        {TABS.map((tab) => {
          const active = tab.to === "/" ? pathname === "/" : pathname.startsWith(tab.to);
          return (
            <NavLink
              key={tab.to}
              to={tab.to}
              className={`flex flex-1 flex-col items-center gap-1 rounded-xl py-1 text-[11px] font-medium ${
                active ? "text-emerald-600" : "muted"
              }`}
            >
              <Icon path={tab.icon} />
              {t(tab.key)}
            </NavLink>
          );
        })}
      </nav>
    </div>
  );
}

export default function App() {
  const { pathname } = useLocation();
  const auth = useQuery({
    queryKey: ["auth"],
    queryFn: () => api.get<AuthStatus>("/auth/status"),
  });
  const setup = useQuery({
    queryKey: ["setup"],
    queryFn: () => api.get<SetupStatus>("/setup/status"),
  });

  if (auth.isLoading || setup.isLoading) return <Spinner />;

  // A fresh install cannot be used at all until its first password is set: the guide comes first, before
  // any login. Once a password exists the guide carries on (still at /setup) with the session it made.
  const needsSetup = setup.data?.state === "needs_setup";
  const data = auth.data;
  const loginNeeded = !needsSetup && !!data && data.password_required && !data.authenticated;
  const exited = sessionStorage.getItem(SETUP_EXIT_KEY) === "1";

  // One slot, so the guide is the same element before and after the password is set (it keeps its state).
  let content: ReactNode;
  if (needsSetup && pathname !== "/setup") content = <Navigate to="/setup" replace />;
  else if (loginNeeded) content = <Login />;
  else if (needsSetup || pathname === "/setup") content = <Setup />;
  else if (setup.data?.wizard_pending && !exited) content = <Navigate to="/setup" replace />;
  else {
    content = (
      <Shell>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/examinations" element={<Examinations />} />
          <Route path="/documents" element={<Documents />} />
          <Route path="/documents/:id" element={<DocumentReview />} />
          <Route path="/settings" element={<Settings />} />
          {/* Old bookmarks (/review, /test/...) point at screens that are gone. */}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Shell>
    );
  }

  return (
    <ThemeProvider>
      <UploadProvider>
        {content}
        {!needsSetup && !loginNeeded && <UploadDialog />}
      </UploadProvider>
    </ThemeProvider>
  );
}
