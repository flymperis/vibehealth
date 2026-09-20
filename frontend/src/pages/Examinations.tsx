import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  api,
  type DocumentKind,
  type ExamCategory,
  type ExamTest,
  type ExaminationsResponse,
  type TestByCode,
  type TestValuePoint,
} from "../api";
import { useI18n } from "../i18n";
import { Flag } from "../reading";
import { Card, Chip, EmptyState, Spinner, buttonClass, formatDate } from "../ui";
import { DocumentRow } from "./Documents";

const KINDS: DocumentKind[] = ["blood_test", "imaging", "report", "prescription", "other"];

export default function Examinations() {
  const { t } = useI18n();
  const [params, setParams] = useSearchParams();
  const requestedKind = params.get("kind") as DocumentKind | null;
  const kind = requestedKind && KINDS.includes(requestedKind) ? requestedKind : "blood_test";
  const setKind = (k: DocumentKind) => setParams(k === "blood_test" ? {} : { kind: k });

  const { data, isLoading, error } = useQuery({
    queryKey: ["examinations", kind],
    queryFn: () => api.get<ExaminationsResponse>(`/examinations?kind=${kind}`),
  });

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl font-semibold tracking-tight">{t("exams.title")}</h1>

      <div className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-1">
        {KINDS.map((k) => (
          <Chip key={k} active={k === kind} onClick={() => setKind(k)}>
            {t(`kind.${k}` as "kind.other")}
          </Chip>
        ))}
      </div>

      {error ? (
        <EmptyState>
          <span>{t("common.error")}</span>
        </EmptyState>
      ) : isLoading || !data ? (
        <Spinner label={t("common.loading")} />
      ) : kind === "blood_test" ? (
        <BloodTestView categories={data.categories ?? []} />
      ) : (
        <DocumentsView kind={kind} documents={data.documents ?? []} />
      )}
    </div>
  );
}

function BloodTestView({ categories }: { categories: ExamCategory[] }) {
  const { t } = useI18n();
  const [openTest, setOpenTest] = useState<{ testCode: string; documentId: number } | null>(null);
  if (categories.length === 0) {
    return (
      <EmptyState>
        <div className="flex flex-col items-center gap-3">
          <span>{t("exams.blood.noData")}</span>
          <Link to="/documents" className={buttonClass("ghost")}>
            {t("nav.documents")}
          </Link>
        </div>
      </EmptyState>
    );
  }
  return (
    <div className="flex flex-col gap-6">
      {categories.map((cat) => (
        <section key={cat.category} className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold">{t(`category.${cat.category}` as "category.other")}</h2>
          <Card className="overflow-hidden">
            {cat.tests.map((test, i) => (
              <TestRow
                key={`${test.test_code}-${test.document_id}-${i}`}
                test={test}
                onOpen={() => setOpenTest({ testCode: test.test_code, documentId: test.document_id })}
              />
            ))}
          </Card>
        </section>
      ))}
      {openTest && (
        <TestValueModal
          testCode={openTest.testCode}
          initialDocumentId={openTest.documentId}
          onClose={() => setOpenTest(null)}
        />
      )}
    </div>
  );
}

function TestRow({ test, onOpen }: { test: ExamTest; onOpen: () => void }) {
  const { lang } = useI18n();
  const name = (lang === "el" ? test.name_el : test.name_en) || test.test_code;
  const flag = test.flag || test.secondary?.flag || "";
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex w-full items-center gap-3 p-3 text-left text-sm hover:bg-black/5 dark:hover:bg-white/5"
      style={{ borderTop: "1px solid var(--border)" }}
    >
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium">{name}</p>
        <p className="text-xs muted">{formatDate(test.doc_date)}</p>
      </div>
      <div className="flex shrink-0 items-center gap-2 whitespace-nowrap text-right text-sm font-semibold">
        <span>
          {test.value} <span className="font-normal muted">{test.unit}</span>
          {test.secondary && (
            <span className="ml-1 text-xs font-normal muted">
              ({test.secondary.value} {test.secondary.unit})
            </span>
          )}
        </span>
        <Flag flag={flag} />
      </div>
    </button>
  );
}

/** Parses "70-100", "70 - 100" into {low, high}; returns null if it can't (e.g. "<5", ">200"). */
function parseRefRange(range: string): { low: number; high: number } | null {
  const match = /^\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*$/.exec(range || "");
  if (!match) return null;
  const low = Number(match[1]);
  const high = Number(match[2]);
  if (!Number.isFinite(low) || !Number.isFinite(high) || low >= high) return null;
  return { low, high };
}

function formatShortDate(value: string | null | undefined) {
  if (!value) return "—";
  // MM/YY: the year is what tells apart results that are years apart.
  const [year, month] = value.slice(0, 10).split("-");
  return `${month}/${year.slice(2)}`;
}

function TrendChart({ points, refRange }: { points: TestValuePoint[]; refRange: string }) {
  const { t } = useI18n();
  const width = 300;
  const height = 120;
  const padX = 24;
  const padTop = 16;
  const padBottom = 20;

  const values = points.map((p) => p.value_num as number);
  let min = Math.min(...values);
  let max = Math.max(...values);
  const band = parseRefRange(refRange);
  if (band) {
    min = Math.min(min, band.low);
    max = Math.max(max, band.high);
  }
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const spread = max - min;
  const pad = spread * 0.15;
  min -= pad;
  max += pad;

  const plotWidth = width - padX * 2;
  const plotHeight = height - padTop - padBottom;
  const xAt = (i: number) => padX + (points.length === 1 ? plotWidth / 2 : (i / (points.length - 1)) * plotWidth);
  const yAt = (v: number) => padTop + (1 - (v - min) / (max - min)) * plotHeight;

  const showAllLabels = points.length <= 6;
  const polylinePoints = points.map((p, i) => `${xAt(i)},${yAt(p.value_num as number)}`).join(" ");

  return (
    <div className="flex flex-col gap-1">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full text-slate-400 dark:text-slate-500"
        role="img"
        aria-label={t("exams.chartLegend")}
      >
        {band && (
          <rect
            x={padX}
            y={yAt(band.high)}
            width={plotWidth}
            height={Math.max(0, yAt(band.low) - yAt(band.high))}
            className="fill-emerald-500/10"
          />
        )}
        <polyline points={polylinePoints} stroke="currentColor" strokeWidth="2" fill="none" />
        {points.map((p, i) => {
          const flagged = !!p.flag;
          const shouldLabel = showAllLabels || i === 0 || i === points.length - 1 || flagged;
          const isEdge = i === 0 || i === points.length - 1;
          return (
            <g key={`${p.document_id}-${i}`}>
              {/* Hover (or long-press) shows the whole date; the wide invisible circle is an easier target than the dot. */}
              <title>{`${formatDate(p.doc_date)} · ${p.value} ${p.unit}`.trim()}</title>
              <circle cx={xAt(i)} cy={yAt(p.value_num as number)} r="12" fill="transparent" />
              <circle
                cx={xAt(i)}
                cy={yAt(p.value_num as number)}
                r="4"
                className={flagged ? "fill-red-500" : "fill-emerald-600"}
              />
              {shouldLabel && (
                <text
                  x={xAt(i)}
                  y={yAt(p.value_num as number) - 8}
                  fontSize="9"
                  textAnchor="middle"
                  className="fill-current"
                >
                  {p.value_num}
                </text>
              )}
              {(showAllLabels || isEdge) && (
                <text x={xAt(i)} y={height - 4} fontSize="9" textAnchor="middle" className="fill-current">
                  {formatShortDate(p.doc_date)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {band && <p className="text-xs muted">{t("exams.chartLegend")}</p>}
    </div>
  );
}

function TestValueModal({
  testCode,
  initialDocumentId,
  onClose,
}: {
  testCode: string;
  initialDocumentId?: number;
  onClose: () => void;
}) {
  const { t, lang } = useI18n();
  const dialogRef = useRef<HTMLDivElement>(null);
  const { data } = useQuery({
    queryKey: ["values-by-test"],
    queryFn: () => api.get<TestByCode[]>("/values/by-test"),
  });

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  useEffect(() => {
    dialogRef.current?.focus();
  }, []);

  const test = data?.find((entry) => entry.test_code === testCode);
  const name = test ? (lang === "el" ? test.name_el : test.name_en) || test.test_code : testCode;
  const documentId = initialDocumentId ?? test?.latest.document_id;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={name}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        className="surface flex max-h-[85vh] w-[calc(100%-2rem)] max-w-[400px] flex-col
          overflow-y-auto rounded-2xl p-4 outline-none"
      >
        <div className="mb-2 flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold">{name}</h2>
            {test && (
              <>
                <p className="text-xs muted">
                  {t("exams.refRange", { range: test.latest.ref_range || "—", unit: test.latest.unit })}
                </p>
                {test.secondary && test.secondary.ref_range !== test.latest.ref_range && (
                  <p className="text-xs muted">
                    {t("exams.refRange", { range: test.secondary.ref_range || "—", unit: test.secondary.unit })}
                  </p>
                )}
              </>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("common.cancel")}
            className="flex size-8 shrink-0 items-center justify-center rounded-full hover:bg-black/5 dark:hover:bg-white/5"
          >
            ✕
          </button>
        </div>

        {!test ? (
          <Spinner label={t("common.loading")} />
        ) : (
          <TestValueModalBody test={test} />
        )}

        <div className="mt-4 border-t pt-3" style={{ borderColor: "var(--border)" }}>
          <Link to={`/documents/${documentId}`} onClick={onClose} className={buttonClass("ghost", "w-full")}>
            {t("exams.viewDocument")}
          </Link>
        </div>
      </div>
    </div>
  );
}

function TestValueModalBody({ test }: { test: TestByCode }) {
  const { t } = useI18n();
  const hasSecondary = !!test.secondary && !!test.secondary_history;

  if (!hasSecondary) {
    return <TestHistorySection latest={test.latest} history={test.history} />;
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <p className="text-xs font-semibold muted">{t("exams.secondaryChart.percent")}</p>
        <TestHistorySection latest={test.latest} history={test.history} />
      </div>
      <div className="flex flex-col gap-1 border-t pt-3" style={{ borderColor: "var(--border)" }}>
        <p className="text-xs font-semibold muted">{t("exams.secondaryChart.absolute")}</p>
        <TestHistorySection latest={test.secondary as TestValuePoint} history={test.secondary_history ?? []} />
      </div>
    </div>
  );
}

/** Renders the history for a single test value: non-numeric list, empty/single-point fallback, or a trend chart. */
function TestHistorySection({ latest, history }: { latest: TestValuePoint; history: TestValuePoint[] }) {
  const { t } = useI18n();
  const isNumeric = latest.value_num != null;

  if (!isNumeric) {
    return (
      <div className="flex flex-col">
        {history.map((p, i) => (
          <div
            key={`${p.document_id}-${i}`}
            className="flex items-center justify-between gap-3 py-2 text-sm"
            style={i > 0 ? { borderTop: "1px solid var(--border)" } : undefined}
          >
            <span className="muted">{formatDate(p.doc_date)}</span>
            <span className="flex items-center gap-2 font-medium">
              {p.value} <span className="font-normal muted">{p.unit}</span>
              <Flag flag={p.flag} />
            </span>
          </div>
        ))}
      </div>
    );
  }

  const points = history.filter((p) => p.value_num != null);
  if (points.length === 0) {
    return <p className="py-4 text-sm muted">{t("exams.noHistory")}</p>;
  }
  if (points.length === 1) {
    const p = points[0];
    return (
      <div className="flex flex-col gap-2">
        <p className="text-sm muted">{t("exams.singlePoint")}</p>
        <div className="flex items-center justify-between gap-3 text-sm">
          <span className="muted">{formatDate(p.doc_date)}</span>
          <span className="flex items-center gap-2 font-medium">
            {p.value} <span className="font-normal muted">{p.unit}</span>
            <Flag flag={p.flag} />
          </span>
        </div>
      </div>
    );
  }

  const chronological = [...points].reverse();
  return (
    <div className="flex flex-col gap-3">
      <TrendChart points={chronological} refRange={latest.ref_range} />
      <HistoryTable points={points} />
    </div>
  );
}

/** Every recorded result with its full date, newest first: the exact numbers behind the chart. */
function HistoryTable({ points }: { points: TestValuePoint[] }) {
  const { t } = useI18n();
  return (
    <table className="w-full text-sm">
      <thead className="text-xs muted">
        <tr>
          <th className="py-1 text-left font-medium">{t("exams.table.date")}</th>
          <th className="py-1 text-right font-medium">{t("review.col.value")}</th>
        </tr>
      </thead>
      <tbody>
        {points.map((p, i) => (
          <tr key={`${p.document_id}-${i}`} style={{ borderTop: "1px solid var(--border)" }}>
            <td className="py-1.5 muted">{formatDate(p.doc_date)}</td>
            <td className="py-1.5 text-right font-medium">
              <span className="inline-flex items-center justify-end gap-2">
                {p.value} <span className="font-normal muted">{p.unit}</span>
                <Flag flag={p.flag} />
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DocumentsView({
  kind,
  documents,
}: {
  kind: DocumentKind;
  documents: ExaminationsResponse["documents"];
}) {
  const { t } = useI18n();
  if (!documents || documents.length === 0) {
    return (
      <EmptyState>
        <div className="flex flex-col items-center gap-3">
          <span>{t("exams.empty", { kind: t(`kind.${kind}` as "kind.other") })}</span>
          <Link to="/settings" className={buttonClass("ghost")}>
            {t("nav.settings")}
          </Link>
        </div>
      </EmptyState>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      {documents.map((doc) => (
        <DocumentRow key={doc.id} doc={doc} compact />
      ))}
    </div>
  );
}
