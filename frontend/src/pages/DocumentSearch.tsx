import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type SearchResults } from "../api";
import { useI18n } from "../i18n";
import { kindLine } from "../reading";
import { Card, Input, Spinner, formatDate } from "../ui";

const MIN_LENGTH = 2;

/** Search over the read text of the documents. `onActive` tells the page whether results are being shown. */
export default function DocumentSearch({ onActive }: { onActive: (active: boolean) => void }) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");

  // Wait for a pause in typing before asking the server.
  useEffect(() => {
    const id = setTimeout(() => setQuery(text.trim()), 300);
    return () => clearTimeout(id);
  }, [text]);
  const active = text.trim().length >= MIN_LENGTH;
  useEffect(() => onActive(active), [active, onActive]);

  const { data, isFetching, error } = useQuery({
    queryKey: ["search", query],
    queryFn: () => api.get<SearchResults>(`/search?q=${encodeURIComponent(query)}`),
    enabled: query.length >= MIN_LENGTH && active,
    staleTime: 30_000,
  });

  return (
    <div className="flex flex-col gap-2">
      <Input
        type="search"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={t("search.placeholder")}
        aria-label={t("search.label")}
        maxLength={100}
      />
      {!active && text === "" ? null : (
        <p className="text-xs muted">
          {active && data && !isFetching
            ? data.results.length === 1
              ? t("search.count1")
              : data.results.length
                ? t("search.count", { n: data.results.length })
                : t("search.none")
            : t("search.hint")}
        </p>
      )}
      {active && isFetching && !data && <Spinner label={t("common.loading")} />}
      {active && error && <p className="text-sm text-red-600">{(error as Error).message || t("common.error")}</p>}
      {active && data && (
        <div className="flex flex-col gap-2">
          {data.results.map(({ document: doc, snippets }) => (
            <Card key={doc.id} className="relative flex flex-col gap-1 p-3 hover:bg-black/5 dark:hover:bg-white/5">
              {/* The title link is stretched over the card: the whole result opens the document. */}
              <Link
                to={`/documents/${doc.id}`}
                className="line-clamp-2 text-sm font-medium [overflow-wrap:anywhere] after:absolute after:inset-0 after:content-['']"
              >
                {doc.title}
              </Link>
              <p className="text-xs muted">
                {doc.doc_date ? formatDate(doc.doc_date) : t("doc.notSet")} · {t(`kind.${doc.kind}` as "kind.other")}
                {kindLine(t, doc) && ` · ${kindLine(t, doc)}`}
              </p>
              {snippets.map((s, i) => (
                <p key={i} className="text-xs [overflow-wrap:anywhere]">
                  <span className="muted">
                    {t(`search.field.${s.field}` as "search.field.text")}
                    {s.page != null && ` · ${t("review.page", { n: s.page })}`}:{" "}
                  </span>
                  {s.before}
                  <mark className="rounded bg-amber-400/40 px-0.5 text-inherit">{s.match}</mark>
                  {s.after}
                </p>
              ))}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
