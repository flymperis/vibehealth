/** Shared bits of the lab-value reading UI. */
import type { ExtractedValue, ReadingStage, ReadingState } from "./api";
import { useI18n } from "./i18n";

type T = ReturnType<typeof useI18n>["t"];

const REASONS: Record<string, Parameters<T>[0]> = {
  "both readers agree": "reason.agree",
  "readers differ; Paperless text matches reader A": "reason.textA",
  "readers differ; Paperless text matches reader B": "reason.textB",
  "readers differ": "reason.differ",
  "reader A and Paperless text agree": "reason.textOnlyA",
  "reader B and Paperless text agree": "reason.textOnlyB",
  "only reader A found it": "reason.onlyA",
  "only reader B found it": "reason.onlyB",
  "single reader, not in Paperless text": "reason.single",
  "unknown test name": "reason.unknown",
  "edited at review": "reason.edited",
};

export function reasonText(t: T, reason: string) {
  const key = REASONS[reason];
  return key ? t(key) : reason;
}

export function stageText(t: T, stage: ReadingStage) {
  return t(`stage.${stage}` as "stage.queued");
}

export function testName(value: ExtractedValue, lang: string) {
  return (lang === "el" ? value.name_el : value.name_en) || value.raw_name;
}

/** H/L pill for a value that has a flag; renders nothing otherwise. */
export function Flag({ flag }: { flag: "" | "H" | "L" }) {
  const { t } = useI18n();
  if (!flag) return null;
  return <Pill tone="red">{t(flag === "H" ? "flag.H" : "flag.L")}</Pill>;
}

/** True while the server is still working on the document. */
export const isActive = (r: ReadingState | null | undefined) =>
  r?.state === "queued" || r?.state === "reading";

const TONES = {
  gray: "bg-slate-500/10 muted",
  blue: "bg-sky-500/15 text-sky-700 dark:text-sky-300",
  amber: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  green: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  red: "bg-red-500/15 text-red-700 dark:text-red-300",
};

export function Pill({ tone, children, title }: { tone: keyof typeof TONES; children: React.ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex max-w-full items-center gap-1 truncate rounded-full px-2 py-0.5 text-[11px] font-medium ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

export function ReadingBadge({ reading }: { reading: ReadingState | null | undefined }) {
  const { t } = useI18n();
  if (!reading) return null;
  switch (reading.state) {
    case "not_read":
      return <Pill tone="gray">{t("reading.not_read")}</Pill>;
    case "queued":
      return <Pill tone="blue">{t("reading.queued")}</Pill>;
    case "reading":
      return (
        <Pill tone="blue" title={stageText(t, reading.stage)}>
          <span className="size-2 animate-pulse rounded-full bg-current" />
          {reading.pages
            ? t("reading.reading", { page: reading.page, pages: reading.pages })
            : t("reading.readingStart")}
        </Pill>
      );
    case "error":
      return (
        <Pill tone="red" title={reading.error}>
          {t("reading.error")}
        </Pill>
      );
    case "done": {
      const { verified, needs_review: review, approved } = reading;
      if (!verified && !review && !approved) return <Pill tone="gray">{t("reading.doneEmpty")}</Pill>;
      if (!verified && !review) return <Pill tone="green">{t("reading.doneApproved", { approved })}</Pill>;
      return (
        <Pill tone={review ? "amber" : "green"}>
          {t("reading.done", { verified, review })}
        </Pill>
      );
    }
  }
}
