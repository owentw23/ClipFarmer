"use client";

import { useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { animate, motion, useReducedMotion } from "motion/react";
import {
  AlertCircle,
  Check,
  CircleDashed,
  Clock3,
  Loader2,
  MinusCircle,
} from "lucide-react";
import { cn } from "cn";

import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * Dusk page kit. Every panel is smoked glass over the hour photograph; figures live in
 * rows and one ruled tally; status is a word with its icon (periwinkle done, apricot owed).
 * Motion: panels rise once (staggered by --i), figures tick, indicators slide.
 */

export const PRESS = "press";

/** Stagger index for the .rise / .rise-row entrance. */
export function rise(i: number): CSSProperties {
  return { "--i": i } as CSSProperties;
}

export function PageHeader({
  title,
  meta,
  actions,
}: {
  title: string;
  meta?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="rise flex flex-wrap items-end justify-between gap-(--space-md)" style={rise(0)}>
      <div className="flex min-w-0 flex-col gap-(--space-2xs)">
        <h1 className="on-sky text-display leading-[1.05]">{title}</h1>
        {meta && <p className="on-sky-muted text-sm font-medium tabular-nums">{meta}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-(--space-xs)">{actions}</div>}
    </div>
  );
}

/** A glass panel with a title row and an optional action. */
export function Panel({
  title,
  count,
  action,
  children,
  flush = false,
  index = 1,
  className,
}: {
  title: ReactNode;
  count?: number;
  action?: ReactNode;
  children: ReactNode;
  /** Edge-to-edge content (tables). */
  flush?: boolean;
  /** Entrance order on the page. */
  index?: number;
  className?: string;
}) {
  return (
    <Card className={cn("rise", className)} style={rise(index)}>
      <CardHeader>
        <CardTitle className="flex items-baseline gap-(--space-xs)">
          {title}
          {count !== undefined && (
            <span className="font-sans text-sm font-normal text-(--color-ink-3) tabular-nums">
              <Ticker value={count} />
            </span>
          )}
        </CardTitle>
        {action && <CardAction className="flex flex-wrap items-center gap-(--space-xs)">{action}</CardAction>}
      </CardHeader>
      <CardContent className={flush ? "px-0" : undefined}>{children}</CardContent>
    </Card>
  );
}

/** Counts a figure up from its previous value; announces only the final value. */
export function Ticker({
  value,
  format = (n: number) => Math.round(n).toLocaleString(),
}: {
  value: number;
  format?: (n: number) => string;
}) {
  const reduce = useReducedMotion();
  const [shown, setShown] = useState(reduce ? value : 0);
  const from = useRef(reduce ? value : 0);

  useEffect(() => {
    if (reduce) {
      from.current = value;
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setShown(value);
      return;
    }
    const controls = animate(from.current, value, {
      duration: 0.7,
      ease: [0.16, 1, 0.3, 1],
      onUpdate: (v) => setShown(v),
    });
    from.current = value;
    return () => controls.stop();
  }, [value, reduce]);

  return (
    <>
      <span aria-hidden="true">{format(shown)}</span>
      <span className="visually-hidden" aria-live="polite">
        {format(value)}
      </span>
    </>
  );
}

export interface TallyItem {
  label: string;
  value: number;
  format?: (n: number) => string;
  note?: ReactNode;
}

/** One ruled tally of figures: the Dusk answer to a grid of stat tiles. */
export function Tally({
  items,
  loading = false,
  columns = 3,
}: {
  items: TallyItem[];
  loading?: boolean;
  columns?: 2 | 3 | 4 | 6;
}) {
  const cols = {
    2: "grid-cols-2",
    3: "grid-cols-2 md:grid-cols-3",
    4: "grid-cols-2 md:grid-cols-4",
    6: "grid-cols-2 md:grid-cols-3 2xl:grid-cols-6",
  }[columns];
  return (
    <dl className={cn("grid gap-x-(--space-lg) gap-y-(--space-md)", cols)}>
      {items.map((item, i) => (
        <div
          key={item.label}
          className="rise-row flex min-w-0 flex-col gap-(--space-2xs) border-t border-(--rule-glass) pt-(--space-sm)"
          style={rise(i)}
        >
          <dt className="truncate text-sm font-medium text-(--color-ink-3)">{item.label}</dt>
          <dd className="m-0 font-heading text-2xl leading-none tracking-(--tracking-display) text-foreground tabular-nums">
            {loading ? <Skeleton className="h-7 w-20" /> : <Ticker value={item.value} format={item.format} />}
          </dd>
          {item.note !== undefined && (
            <dd className="m-0 text-sm text-(--color-ink-3) tabular-nums">{item.note}</dd>
          )}
        </div>
      ))}
    </dl>
  );
}

export type Tone = "done" | "owed" | "error" | "quiet" | "on-air";

const TONE: Record<Tone, string> = {
  done: "text-(--color-signal)",
  owed: "text-(--color-owed)",
  error: "text-(--color-error)",
  quiet: "text-(--color-ink-3)",
  "on-air": "text-(--color-signal)",
};

const TONE_ICON: Record<Tone, typeof Check> = {
  done: Check,
  owed: Clock3,
  error: AlertCircle,
  quiet: MinusCircle,
  "on-air": Check,
};

/** A status word with its icon. Only an on-air recorder carries the breathing light. */
export function StatusWord({
  tone,
  children,
  busy = false,
  icon,
  className,
}: {
  tone: Tone;
  children: ReactNode;
  busy?: boolean;
  icon?: typeof Check;
  className?: string;
}) {
  const Icon = busy ? Loader2 : icon ?? TONE_ICON[tone];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-(--space-2xs) text-sm font-medium whitespace-nowrap transition-colors duration-(--dur-short)",
        TONE[tone],
        className
      )}
    >
      {tone === "on-air" ? (
        <span className="breathe inline-block size-2 rounded-full bg-(--color-signal)" aria-hidden="true" />
      ) : (
        <Icon className={cn("size-3.5 shrink-0", busy && "animate-spin")} aria-hidden="true" />
      )}
      <span>{children}</span>
    </span>
  );
}

/** Map backend statuses to a Dusk tone: periwinkle done, apricot owed, error for failures. */
export function toneForStatus(status: string | null | undefined): Tone {
  const s = (status ?? "").toLowerCase();
  if (s === "recording") return "on-air";
  if (["posted", "processed", "published", "active", "ok"].includes(s)) return "done";
  if (["queued", "notified", "approved", "scheduled", "draft", "uploading", "processing", "paused"].includes(s)) return "owed";
  if (s.includes("fail") || s === "error" || s === "revoked" || s === "unreadable") return "error";
  return "quiet";
}

export function statusLabel(status: string): string {
  return status.replace(/_/g, " ");
}

export function Notice({
  tone,
  children,
  action,
}: {
  tone: "error" | "success";
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <Card className="fade-in py-(--space-sm)" role={tone === "error" ? "alert" : "status"}>
      <CardContent className="flex flex-wrap items-center justify-between gap-(--space-sm) text-sm">
        <StatusWord tone={tone === "error" ? "error" : "done"}>{children}</StatusWord>
        {action}
      </CardContent>
    </Card>
  );
}

export function EmptyState({ icon, children }: { icon?: ReactNode; children: ReactNode }) {
  return (
    <div className="fade-in flex flex-col items-center justify-center gap-(--space-xs) px-(--space-md) py-(--space-xl) text-center text-sm text-(--color-ink-3)">
      <span className="[&_svg]:size-5" aria-hidden="true">
        {icon ?? <CircleDashed />}
      </span>
      <span>{children}</span>
    </div>
  );
}

export function FieldLabel({ htmlFor, children }: { htmlFor?: string; children: ReactNode }) {
  return (
    <label htmlFor={htmlFor} className="mb-(--space-xs) block text-sm font-medium text-(--color-ink-3)">
      {children}
    </label>
  );
}

export function Checkbox({
  checked,
  onChange,
  children,
  disabled,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  children?: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-(--space-xs) text-sm text-(--color-ink-2) select-none has-disabled:cursor-not-allowed has-disabled:opacity-55">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="size-4 shrink-0 accent-(--color-signal)"
      />
      {children}
    </label>
  );
}

/** A switch: periwinkle when on; the thumb slides by transform. */
export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: () => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={onChange}
      className={cn(
        "press relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors duration-(--dur-short) disabled:cursor-not-allowed disabled:opacity-55",
        checked ? "bg-(--color-signal)" : "bg-(--rule-glass) shadow-[inset_0_0_0_1px_var(--color-ink-3)]"
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "absolute left-0.5 size-4 rounded-full transition-transform duration-(--dur-short) ease-(--ease-out)",
          checked ? "translate-x-4 bg-(--color-signal-ink)" : "translate-x-0 bg-(--color-ink-2)"
        )}
      />
    </button>
  );
}

/** A round initial: the Dusk account-list mark. */
export function Initial({ name }: { name: string }) {
  return (
    <span
      aria-hidden="true"
      className="flex size-8 shrink-0 items-center justify-center rounded-full bg-(--rule-glass) text-sm font-medium text-(--color-ink-2) shadow-[inset_0_0_0_1px_var(--rule-glass)]"
    >
      {(name.trim()[0] ?? "?").toUpperCase()}
    </span>
  );
}

/** Tabs as a ruled list with a dot; the marker slides between choices. */
export function DotTabs<T extends string>({
  value,
  onChange,
  options,
  label,
  onSky = false,
}: {
  value: T;
  onChange: (value: T) => void;
  options: Array<{ value: T; label: string }>;
  label: string;
  onSky?: boolean;
}) {
  const id = useId();
  return (
    <div role="tablist" aria-label={label} className="flex items-center gap-(--space-2xs)">
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(opt.value)}
            className={cn(
              "press relative flex h-8 items-center gap-(--space-xs) rounded-full px-(--space-sm) text-sm font-medium",
              onSky
                ? active
                  ? "text-(--color-day-ink)"
                  : "text-(--color-day-muted) hover:text-(--color-day-ink)"
                : active
                  ? "text-foreground"
                  : "text-(--color-ink-3) hover:text-foreground"
            )}
          >
            {active && (
              <motion.span
                layoutId={`dot-tab-${id}`}
                className={cn(
                  "absolute inset-0 rounded-full",
                  onSky ? "bg-[color-mix(in_oklab,var(--color-fog)_70%,transparent)]" : "bg-(--rule-glass)"
                )}
                transition={{ type: "tween", duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
              />
            )}
            <span
              aria-hidden="true"
              className={cn(
                "relative size-1.5 rounded-full transition-opacity duration-(--dur-short)",
                active ? "bg-(--color-signal-deep) opacity-100" : "opacity-0"
              )}
            />
            <span className="relative">{opt.label}</span>
          </button>
        );
      })}
    </div>
  );
}

export function formatDateTime(iso?: string | null): string {
  if (!iso) return "not yet";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatElapsed(fromIso?: string | null, now: number = Date.now()): string {
  if (!fromIso) return "0m";
  const start = new Date(fromIso).getTime();
  if (Number.isNaN(start)) return "0m";
  const mins = Math.max(0, Math.floor((now - start) / 60000));
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, "0")}m`;
}

export function fileBasename(filePath?: string | null): string {
  if (!filePath) return "";
  const parts = filePath.split(/[/\\]/);
  return parts[parts.length - 1] || filePath;
}
