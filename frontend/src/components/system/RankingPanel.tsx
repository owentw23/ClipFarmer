"use client";

import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";

import { Panel, StatusWord, Tally } from "@/components/page-kit";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { LearningSummary, getLearningSummary, syncLearning } from "@/lib/api";

const SIGNALS: Array<[string, string]> = [
  ["words", "Exclamations"],
  ["llm", "AI judgement"],
  ["loud", "Sudden loudness"],
  ["motion", "Motion"],
  ["chat", "Chat activity"],
  ["shout", "Raised voice"],
  ["flash", "Flashes"],
  ["strobe", "Strobe light"],
  ["cuts", "Scene cuts"],
];
const LABELS = Object.fromEntries(SIGNALS);

/** What NeonClipping's clip ranking has learned from the drafts you post and reject. */
export function RankingPanel({ index, refreshKey, className }: { index: number; refreshKey: number; className?: string }) {
  const [data, setData] = useState<LearningSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);

  useEffect(() => {
    let alive = true;
    getLearningSummary()
      .then((res) => {
        if (!alive) return;
        setData(res);
        setError(null);
      })
      .catch((err) => alive && setError(err instanceof Error ? err.message : "Couldn't load the ranking"));
    return () => {
      alive = false;
    };
  }, [refreshKey]);

  const syncNow = async () => {
    setSyncing(true);
    try {
      const res = await syncLearning();
      setData(res.summary);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sync failed");
    } finally {
      setSyncing(false);
    }
  };

  const max = data ? Math.max(...Object.values(data.weights), 0.01) : 1;
  const toGo = data ? Math.max(0, data.min_ratings - data.ratings) : 0;

  return (
    <Panel
      title="Clip ranking"
      index={index}
      className={className}
      action={
        <Button variant="outline" size="sm" disabled={syncing} aria-busy={syncing} onClick={() => void syncNow()}>
          <RefreshCw className={`size-3.5 ${syncing ? "animate-spin" : ""}`} />
          {syncing ? "Learning" : "Learn now"}
        </Button>
      }
    >
      {error ? (
        <StatusWord tone="error">{error}</StatusWord>
      ) : !data ? (
        <div className="flex flex-col gap-(--space-xs)">
          <Skeleton className="h-16 rounded-(--radius-inner)" />
          <Skeleton className="h-40 rounded-(--radius-inner)" />
        </div>
      ) : (
        <div className="flex flex-col gap-(--space-lg)">
          <Tally
            columns={3}
            items={[
              { label: "Drafts rated", value: data.drafts.rated, note: `${data.drafts.liked} posted, ${data.drafts.rejected} rejected` },
              { label: "Waiting", value: data.drafts.waiting, note: "not posted or rejected yet" },
              {
                label: data.fitted ? "Adapting" : "Until it adapts",
                value: data.fitted ? Math.round((data.alpha ?? 0) * 100) : toGo,
                format: (n) => (data.fitted ? `${Math.round(n)}%` : `${Math.round(n)} more`),
                note: data.fitted ? "of the weighting is learned" : "different rated clips",
              },
            ]}
          />

          <div>
            <p className="mb-(--space-sm) text-sm text-(--color-ink-3)">
              {data.fitted
                ? "How much each signal counts when ranking moments now, against the starting weights."
                : "The starting weights. They shift toward what you post once enough clips are rated."}
            </p>
            <ul className="flex flex-col">
              {SIGNALS.map(([key, label], i) => {
                const w = data.weights[key] ?? 0;
                const base = data.defaults[key] ?? w;
                const delta = w - base;
                return (
                  <li
                    key={key}
                    className="rise-row grid grid-cols-[9rem_minmax(0,1fr)_4.5rem] items-center gap-(--space-sm) border-t border-(--rule-glass) py-(--space-xs) text-sm first:border-t-0"
                    style={{ "--i": i } as React.CSSProperties}
                  >
                    <span className="text-(--color-ink-2)">{label}</span>
                    <span className="relative h-1.5 overflow-hidden rounded-full bg-(--rule-glass)" aria-hidden="true">
                      <span
                        className="absolute inset-y-0 left-0 w-full origin-left rounded-full bg-(--color-signal) transition-transform duration-(--dur-long) ease-(--ease-out)"
                        style={{ transform: `scaleX(${w / max})` }}
                      />
                    </span>
                    <span className="text-right tabular-nums text-foreground">
                      {Math.round(w * 100)}%
                      {data.fitted && Math.abs(delta) >= 0.005 && (
                        <span className={delta > 0 ? "text-(--color-signal)" : "text-(--color-ink-3)"}>
                          {" "}
                          {delta > 0 ? "+" : ""}
                          {Math.round(delta * 100)}
                        </span>
                      )}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>

          {data.insight.length > 0 && (
            <ul className="flex flex-col gap-(--space-2xs) text-sm text-(--color-ink-2)">
              {data.insight.map((line) => (
                <li key={line}>{line.replace(/^(\w+):/, (_m, k: string) => `${LABELS[k] ?? k}:`)}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Panel>
  );
}
