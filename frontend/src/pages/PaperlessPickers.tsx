/** Searchable pickers for the Paperless card: a combobox that never forces a choice
 * (free typing always works), and a tag list built on it. */
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useI18n } from "../i18n";

export interface PickerOption {
  name: string;
  count?: number;
}

const MAX_ROWS = 60;

interface ComboboxProps {
  id: string;
  value: string;
  onChange: (text: string) => void;
  /** An option was chosen (click, or Enter on the highlighted row). */
  onPick: (name: string) => void;
  options: PickerOption[];
  /** Enter with no highlighted row. */
  onCommit?: (text: string) => void;
  /** Adds a last row for typed text that is not in the list, e.g. `Add "x"`. */
  addLabel?: (text: string) => string;
  /** Names to leave out of the list (already chosen). */
  exclude?: string[];
  /** Small status line under the list (truncated, loading). */
  footer?: ReactNode;
  placeholder?: string;
  describedBy?: string;
  invalid?: boolean;
}

export function Combobox({
  id,
  value,
  onChange,
  onPick,
  options,
  onCommit,
  addLabel,
  exclude,
  footer,
  placeholder,
  describedBy,
  invalid,
}: ComboboxProps) {
  const { t } = useI18n();
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  // The list narrows only once the person types: opening a field that already has a
  // value (the current document type) still shows every choice.
  const [typed, setTyped] = useState(false);
  const listRef = useRef<HTMLUListElement>(null);

  const rows = useMemo(() => {
    const needle = typed ? value.trim().toLowerCase() : "";
    const skip = new Set((exclude ?? []).map((n) => n.toLowerCase()));
    const found = options.filter(
      (o) => !skip.has(o.name.toLowerCase()) && (!needle || o.name.toLowerCase().includes(needle)),
    );
    const list: { name: string; count?: number; add?: boolean }[] = found.slice(0, MAX_ROWS);
    const text = value.trim();
    if (
      addLabel &&
      text &&
      !skip.has(text.toLowerCase()) &&
      !options.some((o) => o.name.toLowerCase() === text.toLowerCase())
    ) {
      list.push({ name: text, add: true });
    }
    return list;
  }, [options, value, typed, exclude, addLabel]);

  useEffect(() => setActive(-1), [value, options]);
  useEffect(() => {
    if (active >= 0) listRef.current?.children[active]?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const showPopup = open && (rows.length > 0 || !!footer);

  const pick = (index: number) => {
    const row = rows[index];
    if (!row) return;
    onPick(row.name);
    setTyped(false);
    setOpen(false);
    setActive(-1);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) setOpen(true);
      else if (rows.length) setActive((a) => (a + 1) % rows.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) setOpen(true);
      else if (rows.length) setActive((a) => (a <= 0 ? rows.length - 1 : a - 1));
    } else if (e.key === "Enter") {
      e.preventDefault(); // never submits the card's form from here
      if (open && active >= 0) pick(active);
      else {
        onCommit?.(value);
        setOpen(false);
      }
    } else if (e.key === "Escape" && open) {
      e.preventDefault();
      setOpen(false);
    }
  };

  return (
    <div className="relative">
      <input
        id={id}
        role="combobox"
        aria-expanded={showPopup}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={showPopup && active >= 0 ? `${listId}-${active}` : undefined}
        aria-describedby={describedBy}
        aria-invalid={invalid || undefined}
        autoComplete="off"
        autoCapitalize="off"
        spellCheck={false}
        className="surface min-h-11 w-full rounded-xl px-3 text-sm outline-none focus:ring-2 focus:ring-emerald-500/40"
        value={value}
        placeholder={placeholder}
        onChange={(e) => {
          onChange(e.target.value);
          setTyped(true);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => {
          setOpen(false);
          setTyped(false);
        }}
        onKeyDown={onKeyDown}
      />
      {showPopup && (
        <div
          className="surface absolute inset-x-0 top-full z-30 mt-1 overflow-hidden rounded-xl shadow-lg"
          // keep the focus in the input while the list is clicked
          onMouseDown={(e) => e.preventDefault()}
        >
          <ul id={listId} role="listbox" ref={listRef} className="max-h-60 overflow-y-auto py-1">
            {rows.map((row, i) => (
              <li
                key={`${row.add ? "add" : "opt"}:${row.name}`}
                id={`${listId}-${i}`}
                role="option"
                aria-selected={i === active}
                onClick={() => pick(i)}
                className={`flex min-h-10 cursor-pointer items-center justify-between gap-3 px-3 py-1.5 text-sm ${
                  i === active ? "bg-emerald-600/10" : "hover:bg-black/5 dark:hover:bg-white/5"
                }`}
              >
                <span className="min-w-0 break-words">
                  {row.add && addLabel ? addLabel(row.name) : row.name}
                </span>
                {row.count !== undefined && (
                  <span className="shrink-0 text-xs muted">{t("pl.docsN", { n: row.count })}</span>
                )}
              </li>
            ))}
          </ul>
          {footer && (
            <div className="border-t px-3 py-2 text-xs muted" style={{ borderColor: "var(--border)" }}>
              {footer}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Chosen tags as removable chips, with a combobox to add more (or type a new name). */
export function TagPicker({
  id,
  tags,
  onChange,
  options,
  onQuery,
  footer,
  placeholder,
  describedBy,
  invalid,
}: {
  id: string;
  tags: string[];
  onChange: (tags: string[]) => void;
  options: PickerOption[];
  /** The text being typed, so the parent can search on the server. */
  onQuery?: (text: string) => void;
  footer?: ReactNode;
  placeholder?: string;
  describedBy?: string;
  invalid?: boolean;
}) {
  const { t } = useI18n();
  const [draft, setDraft] = useState("");

  const add = (name: string) => {
    const clean = name.trim();
    setDraft("");
    onQuery?.("");
    if (!clean || tags.some((x) => x.toLowerCase() === clean.toLowerCase())) return;
    onChange([...tags, clean]);
  };

  return (
    <div className="flex flex-col gap-2">
      {tags.length > 0 ? (
        <ul className="flex flex-wrap gap-2" aria-label={t("pl.tags")}>
          {tags.map((tag) => (
            <li
              key={tag}
              className="surface inline-flex min-h-9 max-w-full items-center gap-1 rounded-full pl-3 pr-1 text-sm"
            >
              <span className="min-w-0 break-words">{tag}</span>
              <button
                type="button"
                className="inline-flex size-8 shrink-0 items-center justify-center rounded-full
                  hover:bg-black/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60
                  dark:hover:bg-white/10"
                aria-label={t("pl.tagRemove", { name: tag })}
                onClick={() => onChange(tags.filter((x) => x !== tag))}
              >
                <svg viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" />
                </svg>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs muted">{t("pl.tagsNone")}</p>
      )}
      <Combobox
        id={id}
        value={draft}
        onChange={(text) => {
          setDraft(text);
          onQuery?.(text);
        }}
        onPick={add}
        onCommit={add}
        options={options}
        exclude={tags}
        addLabel={(name) => t("pl.tagAdd", { name })}
        footer={footer}
        placeholder={placeholder}
        describedBy={describedBy}
        invalid={invalid}
      />
    </div>
  );
}
