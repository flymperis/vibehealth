import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type DashboardSummary, type FlaggedValue } from "../api";
import { useI18n } from "../i18n";
import { Flag } from "../reading";
import { Card, EmptyState, Spinner, StatCard, buttonClass, formatDate } from "../ui";
import { DocumentRow, useOwnDocumentsOnly } from "./Documents";
import AddDocuments from "./AddDocuments";
import Medications from "./Medications";

export default function Dashboard() {
  const { t } = useI18n();
  const ownOnly = useOwnDocumentsOnly();
  const { data, isLoading } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api.get<DashboardSummary>("/dashboard/summary?recent_limit=5&flagged_limit=5"),
  });

  if (isLoading) return <Spinner label={t("common.loading")} />;
  if (!data) return <EmptyState>{t("common.error")}</EmptyState>;

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-semibold tracking-tight">{t("dashboard.title")}</h1>

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold">{t("dashboard.attention.title")}</h2>
        {data.flagged_values.length === 0 ? (
          <EmptyState>
            {data.has_any_approved ? (
              t("dashboard.attention.empty")
            ) : (
              <div className="flex flex-col items-center gap-3">
                <span>{t("dashboard.attention.noData")}</span>
                <Link to="/documents" className={buttonClass("ghost")}>
                  {t("nav.documents")}
                </Link>
              </div>
            )}
          </EmptyState>
        ) : (
          <Card className="overflow-hidden">
            {data.flagged_values.map((v) => (
              <FlaggedValueRow key={v.id} v={v} />
            ))}
          </Card>
        )}
      </section>

      <Medications />

      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">{t("dashboard.recent.title")}</h2>
          {data.recent_documents.length > 0 && (
            <Link to="/documents" className="text-sm text-emerald-600">
              {t("dashboard.recent.seeAll")}
            </Link>
          )}
        </div>
        {data.recent_documents.length === 0 ? (
          <EmptyState>
            <div className="flex flex-col items-center gap-3">
              <span>{ownOnly ? t("docs.emptyOwn") : `${t("dashboard.recent.empty")} ${t("docs.emptyOrAdd")}`}</span>
              <AddDocuments label={ownOnly ? t("up.addFirst") : t("up.add")} variant={ownOnly ? "primary" : "ghost"} />
              {!ownOnly && (
                <Link to="/settings" className={buttonClass("ghost")}>
                  {t("nav.settings")}
                </Link>
              )}
            </div>
          </EmptyState>
        ) : (
          <div className="flex flex-col gap-2">
            {data.recent_documents.map((doc) => (
              <DocumentRow key={doc.id} doc={doc} compact />
            ))}
          </div>
        )}
      </section>

      {data.category_counts.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold">{t("dashboard.category.title")}</h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {data.category_counts.map((c) => (
              <StatCard
                key={c.kind}
                to={`/examinations?kind=${c.kind}`}
                label={t(`kind.${c.kind}` as "kind.other")}
                count={c.count}
                sub={c.last_date ? t("dashboard.category.last", { date: formatDate(c.last_date) }) : undefined}
              />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function FlaggedValueRow({ v }: { v: FlaggedValue }) {
  const { lang } = useI18n();
  const name = (lang === "el" ? v.name_el : v.name_en) || v.test_code;
  return (
    <Link
      to={`/documents/${v.document_id}`}
      className="flex items-center gap-3 p-3 text-sm hover:bg-black/5 dark:hover:bg-white/5"
      style={{ borderTop: "1px solid var(--border)" }}
    >
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium">{name}</p>
        <p className="text-xs muted">{formatDate(v.doc_date)}</p>
      </div>
      <div className="flex shrink-0 items-center gap-2 whitespace-nowrap text-right text-sm font-semibold">
        <span>
          {v.value} <span className="font-normal muted">{v.unit}</span>
        </span>
        <Flag flag={v.flag} />
      </div>
    </Link>
  );
}
