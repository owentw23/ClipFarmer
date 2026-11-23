"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ExternalLink, Radio } from "lucide-react";

import {
  EmptyState,
  Initial,
  Panel,
  StatusWord,
  formatDateTime,
  formatElapsed,
  rise,
} from "@/components/page-kit";
import { Skeleton } from "@/components/ui/skeleton";
import { ActiveRecording, Streamer, getStreamers, getSystemRecordings } from "@/lib/api";

const REFRESH_MS = 30_000;
const PLATFORM: Record<string, string> = { twitch: "Twitch", youtube: "YouTube" };

/** The signature: who is on air right now and how long each recorder has been rolling. */
export function LiveNowCard({ index = 1 }: { index?: number }) {
  const [streamers, setStreamers] = useState<Streamer[] | null>(null);
  const [active, setActive] = useState<ActiveRecording[]>([]);
  const [error, setError] = useState<string | null>(null);
  // Set from the effect, never during render, so prerendering stays deterministic.
  const [now, setNow] = useState(0);

  useEffect(() => {
    let alive = true;
    const load = () => {
      Promise.allSettled([getStreamers(), getSystemRecordings()]).then(([s, r]) => {
        if (!alive) return;
        if (s.status === "fulfilled") {
          setStreamers(s.value.items);
          setError(null);
        } else {
          setError(s.reason instanceof Error ? s.reason.message : "Could not reach the backend");
        }
        if (r.status === "fulfilled") setActive(r.value.active ?? []);
        setNow(Date.now());
      });
    };
    load();
    const id = window.setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  const recordingById = new Map(active.map((rec) => [Number(rec.streamer_id), rec]));
  const live = (streamers ?? [])
    .filter((s) => s.is_live || recordingById.has(s.id))
    .map((s) => ({ streamer: s, recording: recordingById.get(s.id) ?? null }))
    .sort((a, b) => Number(Boolean(b.recording)) - Number(Boolean(a.recording)));
  const watched = (streamers ?? []).filter((s) => s.auto_watch).length;

  return (
    <Panel
      title="Live now"
      count={streamers ? live.length : undefined}
      index={index}
      flush
      className="h-full"
      action={streamers ? <span className="text-sm text-(--color-ink-3)">{watched} watched</span> : undefined}
    >
      {streamers === null && !error ? (
        <div className="flex flex-col gap-(--space-xs) px-(--space-lg)">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-12 rounded-(--radius-inner)" />
          ))}
        </div>
      ) : error && streamers === null ? (
        <div className="px-(--space-lg)">
          <StatusWord tone="error">{error}</StatusWord>
        </div>
      ) : live.length === 0 ? (
        <EmptyState icon={<Radio />}>
          Nobody on air.{" "}
          {(streamers?.length ?? 0) === 0 ? (
            <Link href="/streamers" className="text-foreground underline underline-offset-4">
              Add a streamer
            </Link>
          ) : (
            "The watcher checks every minute."
          )}
        </EmptyState>
      ) : (
        <ul className="flex flex-col">
          {live.map(({ streamer, recording }, i) => (
            <li
              key={streamer.id}
              className="rise-row row-hover flex items-center gap-(--space-sm) border-t border-(--rule-glass) px-(--space-lg) py-(--space-sm) first:border-t-0"
              style={rise(i)}
            >
              <Initial name={streamer.name} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-(--space-xs)">
                  <span className="truncate font-medium text-foreground">{streamer.name}</span>
                  <a
                    href={recording?.stream_url || streamer.channel_url}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={`Open ${streamer.name} on ${PLATFORM[streamer.platform] ?? streamer.platform}`}
                    className="press shrink-0 rounded-full text-(--color-ink-3) hover:text-foreground"
                  >
                    <ExternalLink className="size-3.5" />
                  </a>
                </div>
                <div className="truncate text-sm text-(--color-ink-3)" title={recording?.title || undefined}>
                  {recording
                    ? recording.title || `On ${PLATFORM[streamer.platform] ?? streamer.platform}`
                    : `${PLATFORM[streamer.platform] ?? streamer.platform}, checked ${formatDateTime(streamer.last_checked_at)}`}
                </div>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-(--space-3xs) tabular-nums">
                {recording ? (
                  <>
                    <StatusWord tone="on-air">Recording</StatusWord>
                    <span className="text-sm text-(--color-ink-3)">
                      {formatElapsed(recording.started_at, now)}, {Number(recording.chunks_completed ?? 0)} done
                    </span>
                  </>
                ) : (
                  <StatusWord tone="owed">Not recording</StatusWord>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
