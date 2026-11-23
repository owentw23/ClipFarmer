"use client";

import { useCallback, useEffect, useState } from "react";
import { Pause, Play, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";

import {
  EmptyState,
  Notice,
  PageHeader,
  Panel,
  StatusWord,
  Tally,
  fileBasename,
  formatDateTime,
  formatElapsed,
  rise,
  statusLabel,
  toneForStatus,
} from "@/components/page-kit";
import { RankingPanel } from "@/components/system/RankingPanel";
import { SessionsPanel } from "@/components/system/SessionsPanel";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  HealthResponse,
  ProcessingQueueStatus,
  SystemRecordingsResponse,
  getHealth,
  getProcessingQueue,
  getSystemRecordings,
  pauseProcessingQueue,
  resumeProcessingQueue,
  retryChunkProcessing,
  runRetentionGc,
} from "@/lib/api";

function formatDuration(sec?: number | null): string {
  if (typeof sec !== "number" || Number.isNaN(sec)) return "";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const total = Math.round(sec);
  const mins = Math.floor(total / 60);
  if (mins < 60) return `${mins}m ${total % 60}s`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

const canRetry = (status: string) => ["failed", "recorded"].includes(status.toLowerCase());

export default function SystemPage() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [recordings, setRecordings] = useState<SystemRecordingsResponse | null>(null);
  const [queue, setQueue] = useState<ProcessingQueueStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [gcRunning, setGcRunning] = useState(false);
  const [queueToggling, setQueueToggling] = useState(false);
  const [retryingId, setRetryingId] = useState<number | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const load = useCallback(async () => {
    const fail = (err: unknown) => setError(err instanceof Error ? err.message : "Could not reach the backend");
    setError(null);
    // Health can take ~15s (it probes the NeonClipping UI server), so the queue and
    // recordings render as soon as they arrive instead of waiting on it.
    const r = getSystemRecordings().then(setRecordings, fail);
    const q = getProcessingQueue().then(setQueue, fail);
    const h = getHealth().then(setHealth, fail);
    await Promise.all([r, q]);
    setLoading(false);
    await h;
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const handleRunGc = async () => {
    setGcRunning(true);
    try {
      const res = await runRetentionGc();
      toast(`Cleanup removed ${res.deleted_chunk_ids.length} recording${res.deleted_chunk_ids.length === 1 ? "" : "s"}, kept ${res.skipped_locked} still in use`);
      setRecordings(await getSystemRecordings());
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Cleanup failed");
    } finally {
      setGcRunning(false);
    }
  };

  const handleToggleQueue = async () => {
    if (!queue) return;
    setQueueToggling(true);
    try {
      await (queue.paused ? resumeProcessingQueue() : pauseProcessingQueue());
      setQueue(await getProcessingQueue());
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to change the queue");
    } finally {
      setQueueToggling(false);
    }
  };

  const handleRetry = async (chunkId: number) => {
    setRetryingId(chunkId);
    try {
      await retryChunkProcessing(chunkId);
      const [q, r] = await Promise.all([getProcessingQueue(), getSystemRecordings()]);
      setQueue(q);
      setRecordings(r);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : `Failed to queue chunk ${chunkId}`);
    } finally {
      setRetryingId(null);
    }
  };

  const active = recordings?.active ?? [];
  const recentChunks = recordings?.recent_chunks ?? [];
  const queueChunks = queue?.chunks ?? [];
  const firstLoad = loading && !queue && !recordings;

  const retryButton = (chunkId: number, status: string) =>
    canRetry(status) ? (
      <Button variant="outline" size="sm" disabled={retryingId === chunkId} aria-busy={retryingId === chunkId} onClick={() => void handleRetry(chunkId)}>
        {retryingId === chunkId ? "Queueing" : "Retry"}
      </Button>
    ) : null;

  const checks = health
    ? [
        { name: "Database", ok: health.checks.database.ok, word: health.checks.database.ok ? "Connected" : "Error", detail: health.checks.database.detail },
        { name: "Storage", ok: health.checks.storage.ok, word: `${health.checks.storage.free_gb.toFixed(0)} GB free`, detail: health.checks.storage.root },
        { name: "FFmpeg", ok: health.checks.ffmpeg.ok, word: health.checks.ffmpeg.nvenc ? "NVENC ready" : "CPU only", detail: health.checks.ffmpeg.detail },
        { name: "NeonClipping UI", ok: health.checks.neonclipping.ok, word: health.checks.neonclipping.ok ? "Online" : "Offline", detail: health.checks.neonclipping.detail },
        { name: "Copywriter model", ok: health.checks.llm.ok && health.checks.llm.configured, word: health.checks.llm.configured ? "Configured" : "Not set", detail: health.checks.llm.model },
      ]
    : [];

  return (
    <div className="flex flex-col gap-(--space-lg)">
      <PageHeader
        title="System"
        meta={health ? (health.status === "ok" ? "Every check passing" : "Some checks need attention") : "Checking the services"}
        actions={
          <Button
            variant="outline"
            className="glass"
            disabled={loading}
            onClick={() => {
              setLoading(true);
              setRefreshKey((k) => k + 1);
              void load();
            }}
          >
            <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
        }
      />

      {error && <Notice tone="error">{error}</Notice>}

      <div className="grid grid-cols-[minmax(0,1fr)] gap-(--space-lg) lg:grid-cols-12">
        <Panel title="Pipeline" index={1} className="lg:col-span-7">
          <Tally
            columns={4}
            loading={firstLoad}
            items={[
              { label: "Recording", value: active.length, note: "streams" },
              { label: "Queued", value: queue?.queued_count ?? 0, note: queue?.paused ? "paused" : "running" },
              { label: "On the GPU", value: queue?.running_chunk_id ?? 0, format: (n) => (n ? `#${Math.round(n)}` : "idle"), note: "one chunk at a time" },
              { label: "Failed", value: queueChunks.filter((c) => c.status === "failed").length, note: "retry below" },
            ]}
          />
        </Panel>

        <Panel title="Services" index={2} flush className="lg:col-span-5">
          {!health ? (
            <div className="flex flex-col gap-(--space-xs) px-(--space-lg)">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-9 rounded-(--radius-inner)" />
              ))}
            </div>
          ) : (
            <ul className="flex flex-col">
              {checks.map((c, i) => (
                <li
                  key={c.name}
                  className="rise-row flex items-center justify-between gap-(--space-sm) border-t border-(--rule-glass) px-(--space-lg) py-(--space-xs) first:border-t-0"
                  style={rise(i)}
                >
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-foreground">{c.name}</div>
                    <div className="truncate text-sm text-(--color-ink-3)" title={c.detail}>
                      {c.detail}
                    </div>
                  </div>
                  <StatusWord tone={c.ok ? "done" : "owed"}>{c.word}</StatusWord>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <Panel
        title="Processing queue"
        count={queueChunks.length}
        flush
        index={3}
        action={
          queue && (
            <>
              <StatusWord tone={queue.paused ? "owed" : "done"}>{queue.paused ? "Paused" : "Running"}</StatusWord>
              <Button variant="outline" size="sm" disabled={queueToggling} aria-busy={queueToggling} onClick={() => void handleToggleQueue()}>
                {queue.paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
                {queue.paused ? "Resume" : "Pause"}
              </Button>
            </>
          )
        }
      >
        {queueChunks.length === 0 ? (
          <EmptyState>Nothing waiting. Each finished hour of recording lands here.</EmptyState>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="pl-(--space-lg)">Chunk</TableHead>
                <TableHead>Streamer</TableHead>
                <TableHead>File</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Last error</TableHead>
                <TableHead className="pr-(--space-lg) text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="tabular-nums">
              {queueChunks.map((chunk, i) => (
                <TableRow key={chunk.id} className="rise-row" style={rise(i)}>
                  <TableCell className="pl-(--space-lg) text-(--color-ink-3)">#{chunk.id}</TableCell>
                  <TableCell className="font-medium">{chunk.streamer_name || chunk.streamer_slug || `Streamer ${chunk.streamer_id}`}</TableCell>
                  <TableCell className="max-w-[220px] truncate text-sm text-(--color-ink-3)" title={chunk.file_path}>
                    {fileBasename(chunk.file_path)}
                  </TableCell>
                  <TableCell>
                    <StatusWord tone={toneForStatus(chunk.status)} busy={chunk.status === "processing"}>
                      {statusLabel(chunk.status)}
                    </StatusWord>
                  </TableCell>
                  <TableCell className="max-w-xs truncate text-sm text-(--color-error)" title={chunk.last_error || undefined}>
                    {chunk.last_error || ""}
                  </TableCell>
                  <TableCell className="pr-(--space-lg) text-right">{retryButton(chunk.id, chunk.status)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>

      <Panel title="Recording now" count={active.length} flush index={4}>
        {active.length === 0 ? (
          <EmptyState>Nothing recording. A recorder starts when a watched streamer goes live.</EmptyState>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="pl-(--space-lg)">Streamer</TableHead>
                <TableHead>Platform</TableHead>
                <TableHead>Stream</TableHead>
                <TableHead>Started</TableHead>
                <TableHead className="pr-(--space-lg) text-right">Rolling for</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="tabular-nums">
              {active.map((rec, i) => (
                <TableRow key={`${rec.streamer_id}-${i}`} className="rise-row" style={rise(i)}>
                  <TableCell className="pl-(--space-lg) font-medium">{rec.streamer_name || rec.streamer_slug || `Streamer ${rec.streamer_id}`}</TableCell>
                  <TableCell className="capitalize text-(--color-ink-2)">{rec.platform || ""}</TableCell>
                  <TableCell className="max-w-xs truncate text-(--color-ink-2)">{rec.title || rec.stream_url || "Live capture"}</TableCell>
                  <TableCell className="text-(--color-ink-3)">{formatDateTime(rec.started_at)}</TableCell>
                  <TableCell className="pr-(--space-lg) text-right">
                    <StatusWord tone="on-air">{formatElapsed(rec.started_at)}</StatusWord>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>

      <SessionsPanel index={5} refreshKey={refreshKey} />

      <Panel
        title="Recent recordings"
        count={recentChunks.length}
        flush
        index={6}
        action={
          <Button variant="outline" size="sm" disabled={gcRunning} aria-busy={gcRunning} onClick={() => void handleRunGc()}>
            <Trash2 className="size-3.5" />
            {gcRunning ? "Cleaning up" : "Clean up now"}
          </Button>
        }
      >
        {recentChunks.length === 0 ? (
          <EmptyState>No recordings yet</EmptyState>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="pl-(--space-lg)">Streamer</TableHead>
                <TableHead>Chunk</TableHead>
                <TableHead>File</TableHead>
                <TableHead className="text-right">Length</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Deleted after</TableHead>
                <TableHead className="pr-(--space-lg) text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="tabular-nums">
              {recentChunks.map((chunk, i) => (
                <TableRow key={chunk.id} className="rise-row" style={rise(i)}>
                  <TableCell className="pl-(--space-lg) font-medium">{chunk.streamer_name || chunk.streamer_slug || `Streamer ${chunk.streamer_id}`}</TableCell>
                  <TableCell className="text-(--color-ink-2)">
                    {chunk.chunk_index}
                    {Boolean(chunk.merged_with_prev) && <span className="text-(--color-ink-3)"> (with tail)</span>}
                  </TableCell>
                  <TableCell className="max-w-[220px] truncate text-sm text-(--color-ink-3)" title={chunk.file_path}>
                    {fileBasename(chunk.file_path)}
                  </TableCell>
                  <TableCell className="text-right">{formatDuration(chunk.duration_sec)}</TableCell>
                  <TableCell>
                    <StatusWord tone={toneForStatus(chunk.status)}>{statusLabel(chunk.status)}</StatusWord>
                  </TableCell>
                  <TableCell className="text-(--color-ink-3)">{chunk.delete_after ? formatDateTime(chunk.delete_after) : ""}</TableCell>
                  <TableCell className="pr-(--space-lg) text-right">{retryButton(chunk.id, chunk.status)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>

      <RankingPanel index={7} refreshKey={refreshKey} />
    </div>
  );
}
