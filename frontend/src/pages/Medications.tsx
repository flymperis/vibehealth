import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type CurrentMedication, type CurrentMedications } from "../api";
import { useI18n } from "../i18n";
import { Card, formatDate } from "../ui";

/**
 * The medications on recent prescriptions, one line per medicine. It is an AUTOMATIC list (a local model read the
 * scans, and a field the text did not confirm was left out): the disclaimer says so and is always shown with it.
 * Nothing is rendered when there is no prescription at all.
 */
export default function Medications() {
  const { t } = useI18n();
  const { data } = useQuery({
    queryKey: ["medications"],
    queryFn: () => api.get<CurrentMedications>("/dashboard/medications"),
  });
  if (!data || (!data.medications.length && !data.undated && !data.older)) return null;

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-sm font-semibold">{t("meds.title")}</h2>
      <Card className="overflow-hidden">
        <p className="p-3 text-xs text-amber-900 dark:text-amber-200" style={{ background: "rgb(245 158 11 / 0.1)" }}>
          {t("meds.disclaimer", { days: data.days })}
        </p>
        {data.medications.length === 0 && <p className="p-3 text-sm muted">{t("meds.empty")}</p>}
        {data.medications.map((m) => (
          <MedicationRow key={`${m.document_id}-${m.name}-${m.active_substance}`} m={m} />
        ))}
        {(data.undated > 0 || data.older > 0) && (
          <div className="flex flex-col gap-0.5 p-3 text-xs muted" style={{ borderTop: "1px solid var(--border)" }}>
            {data.undated > 0 && <span>{t("meds.undated", { n: data.undated })}</span>}
            {data.older > 0 && <span>{t("meds.older", { n: data.older })}</span>}
          </div>
        )}
      </Card>
    </section>
  );
}

function MedicationRow({ m }: { m: CurrentMedication }) {
  const { t } = useI18n();
  const title = m.name || m.active_substance;
  const detail = [m.name ? m.active_substance : "", m.strength, m.dose_instruction, m.duration_or_quantity]
    .filter(Boolean)
    .join(" · ");
  const left = m.unverified.map((f) => t(`med.${f}` as "med.name")).join(", ");
  return (
    <Link
      to={`/documents/${m.document_id}`}
      className="flex flex-col gap-0.5 p-3 text-sm hover:bg-black/5 dark:hover:bg-white/5"
      style={{ borderTop: "1px solid var(--border)" }}
    >
      <span className="font-medium [overflow-wrap:anywhere]">{title}</span>
      {detail && <span className="[overflow-wrap:anywhere]">{detail}</span>}
      {left && <span className="text-xs text-amber-600">{t("med.unverified", { fields: left })}</span>}
      <span className="text-xs muted">
        {t("meds.from", { date: formatDate(m.date) })}
        {m.earlier_dates.length > 0 &&
          ` · ${t("meds.earlier", { dates: m.earlier_dates.map((d) => formatDate(d)).join(", ") })}`}
      </span>
    </Link>
  );
}
