"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { FileText, Scissors } from "lucide-react";

import { EmptyState, Panel, StatusWord, Tone, formatDateTime, rise } from "@/components/page-kit";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { RecordingChunk, RecordingSession, getRecordingSessions, getSystemRecordings } from "@/lib/api";

const OUTCOME: Record<string, [Tone, string]> = {
  stream_ended: ["done", "Stream ended"],
  stopped_by_user: ["quiet", "Stopped by you"],
  backend_stopped: ["quiet", "Backend stopped"],
  stalled: ["error", "Stalled"],
  error: ["error", "Failed"],
  cancelled: ["quiet", "Cancelled"],
};

function outcome(s: RecordingSession): [Tone, string] {
  if (s.status === "recording") return ["on-air", "Recording"];
  if (s.status === "interrupted") return ["owed", "Interrupted"];
  return OUTCOME[s.end_reason ?? ""] ?? (s.status === "error" ? ["error", "Failed"] : ["done", "Finished"]);
}

function duration(s: RecordingSession): string {
  const start = new Date(s.started_at).getTime();
  const end = s.ended_at ? new Date(s.ended_at).getTime() : NaN;
  if (Number.isNaN(start) || Number.isNaN(end)) return "";
  const mins = Math.max(0, Math.round((end - start) / 60000));
  return mins < 60 ? `${mins}m` : `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, "0")}m`;
}

/** Every recording attempt, how it ended, and the recorder's own words when it failed. */
export function SessionsPanel({ index, refreshKey }: { index: number; refreshKey: number }) {
  const [items, setItems] = useState<RecordingSession[] | null>(null);
  const [chunks, setChunks] = useState<RecordingChunk[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    Promise.allSettled([getRecordingSessions(20), getSystemRecordings()])
      .then(([sessionsRes, recordingsRes]) => {
        if (!alive) return;
        if (sessionsRes.status === "fulfilled") {
          setItems(sessionsRes.value.items);
          setError(null);
        } else {
          setError(
            sessionsRes.reason instanceof Error
              ? sessionsRes.reason.message
              : "Couldn't load sessions"
          );
        }
        if (recordingsRes.status === "fulfilled") {
          setChunks(recordingsRes.value.recent_chunks ?? []);
        }
      });
    return () => {
      alive = false;
    };
  }, [refreshKey]);

  const chunksBySession = useMemo(() => {
    const map = new Map<number, RecordingChunk[]>();
    for (const chunk of chunks) {
      if (chunk.status === "deleted" || chunk.status === "discarded_short") continue;
      const list = map.get(chunk.session_id);
      if (list) {
        list.push(chunk);
      } else {
        map.set(chunk.session_id, [chunk]);
      }
    }
    return map;
  }, [chunks]);

  const failures = (items ?? []).filter((s) => s.status === "error").length;

  return (
    <Panel
      title="Recording sessions"
      count={items?.length}
      flush
      index={index}
      action={
        items && (
          <StatusWord tone={failures ? "error" : "done"}>
            {failures ? `${failures} failed` : "No failures"}
          </StatusWord>
        )
      }
    >
      {error ? (
        <div className="px-(--space-lg)">
          <StatusWord tone="error">{error}</StatusWord>
        </div>
      ) : items === null ? (
        <div className="flex flex-col gap-(--space-xs) px-(--space-lg)">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-10 rounded-(--radius-inner)" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <EmptyState>No recordings yet. A session starts when a watched streamer goes live.</EmptyState>
      ) : (
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-(--space-lg)">Streamer</TableHead>
              <TableHead>Started</TableHead>
              <TableHead className="text-right">Length</TableHead>
              <TableHead>Outcome</TableHead>
              <TableHead className="text-right">Chunks</TableHead>
              <TableHead className="pr-(--space-lg)">What happened</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody className="tabular-nums">
            {items.map((s, i) => {
              const [tone, word] = outcome(s);
              return (
                <TableRow key={s.id} className="rise-row" style={rise(i)}>
                  <TableCell className="pl-(--space-lg) font-medium">{s.streamer_name}</TableCell>
                  <TableCell className="text-(--color-ink-2)">{formatDateTime(s.started_at)}</TableCell>
                  <TableCell className="text-right text-(--color-ink-2)">{duration(s)}</TableCell>
                  <TableCell>
                    <StatusWord tone={tone}>{word}</StatusWord>
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="inline-flex flex-wrap items-center justify-end gap-(--space-xs)">
                      <span>{s.chunks}</span>
                      {(chunksBySession.get(s.id) ?? []).map((chunk) => (
                        <Link
                          key={chunk.id}
                          href={`/studio?chunk_id=${chunk.id}`}
                          title={`Open chunk #${chunk.chunk_index} in VOD Studio`}
                          className="press inline-flex items-center gap-1 rounded-full border border-(--rule-glass) bg-(--row-hover) px-2 py-0.5 text-xs font-medium text-(--color-signal) transition-colors hover:border-(--color-signal)/50 hover:bg-(--rule-glass)"
                        >
                          <Scissors className="size-3" aria-hidden="true" />
                          {(chunksBySession.get(s.id)?.length ?? 0) > 1
                            ? `#${chunk.chunk_index} Studio`
                            : "Open in Studio"}
                        </Link>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell className="max-w-[420px] pr-(--space-lg)">
                    {s.last_error ? (
                      <span
                        className={`line-clamp-2 text-sm whitespace-normal ${tone === "error" ? "text-(--color-error)" : "text-(--color-ink-3)"}`}
                        title={s.last_error}
                      >
                        {s.last_error}
                      </span>
                    ) : s.log_path ? (
                      <span className="inline-flex items-center gap-(--space-2xs) text-sm text-(--color-ink-3)" title={s.log_path}>
                        <FileText className="size-3.5" aria-hidden="true" /> Logged to file
                      </span>
                    ) : null}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </Panel>
  );
}
