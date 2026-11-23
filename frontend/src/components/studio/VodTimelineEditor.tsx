"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Maximize2, Plus, ZoomIn, ZoomOut } from "lucide-react";

import { Panel } from "@/components/page-kit";
import { Button } from "@/components/ui/button";
import { StudioCandidate, StudioTimelineCurve } from "@/lib/api";

export interface VodTimelineEditorProps {
  duration: number;
  currentTime: number;
  candidates: StudioCandidate[];
  selectedId: string | null;
  timelineCurve?: StudioTimelineCurve | null;
  onSeek: (sec: number) => void;
  onSelect: (id: string) => void;
  onChangeCandidates: (next: StudioCandidate[]) => void;
  onAddClipAtPlayhead: () => void;
  disabled?: boolean;
}

const MIN_CLIP_SEC = 3;
const TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800];

function formatRulerTime(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  const hrs = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  if (hrs > 0) {
    return `${hrs}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function buildPolylinePath(
  points: Array<{ t: number; val: number }>,
  duration: number,
  maxVal: number,
  closed: boolean
): string {
  if (points.length === 0 || duration <= 0) return "";
  const safeMax = maxVal > 0 ? maxVal : 1;
  const coords = points.map((p) => {
    const x = Math.max(0, Math.min(1000, (p.t / duration) * 1000));
    const norm = Math.max(0, Math.min(1, p.val / safeMax));
    const y = 96 - norm * 86;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  if (!closed) {
    return `M ${coords.join(" L ")}`;
  }
  const firstX = Math.max(0, Math.min(1000, (points[0].t / duration) * 1000)).toFixed(1);
  const lastX = Math.max(0, Math.min(1000, (points[points.length - 1].t / duration) * 1000)).toFixed(1);
  return `M ${firstX},98 L ${coords.join(" L ")} L ${lastX},98 Z`;
}

export function VodTimelineEditor({
  duration,
  currentTime,
  candidates,
  selectedId,
  timelineCurve,
  onSeek,
  onSelect,
  onChangeCandidates,
  onAddClipAtPlayhead,
  disabled = false,
}: VodTimelineEditorProps) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const trackRef = useRef<HTMLDivElement | null>(null);
  const candidatesRef = useRef(candidates);
  useEffect(() => {
    candidatesRef.current = candidates;
  }, [candidates]);

  const [zoom, setZoom] = useState(1);
  const [draggingState, setDraggingState] = useState<{
    id: string;
    mode: "left" | "right" | "move";
    startX: number;
    origStart: number;
    origEnd: number;
    trackWidth: number;
  } | null>(null);
  const [scrubbing, setScrubbing] = useState(false);

  const safeDuration = duration > 0 ? duration : timelineCurve?.duration ?? 0;

  const clientXToTime = useCallback(
    (clientX: number): number => {
      const el = trackRef.current;
      if (!el || safeDuration <= 0) return 0;
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0) return 0;
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return ratio * safeDuration;
    },
    [safeDuration]
  );

  const handleRulerPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (disabled || safeDuration <= 0) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    setScrubbing(true);
    onSeek(clientXToTime(e.clientX));
  };

  const handleRulerPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!scrubbing || disabled || safeDuration <= 0) return;
    onSeek(clientXToTime(e.clientX));
  };

  const handleRulerPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!scrubbing) return;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    setScrubbing(false);
  };

  const startRegionDrag = (
    e: React.PointerEvent<HTMLElement>,
    cand: StudioCandidate,
    mode: "left" | "right" | "move"
  ) => {
    if (disabled || safeDuration <= 0) return;
    e.preventDefault();
    e.stopPropagation();
    const trackEl = trackRef.current;
    if (!trackEl) return;
    const rect = trackEl.getBoundingClientRect();
    e.currentTarget.setPointerCapture(e.pointerId);
    onSelect(cand.id);
    setDraggingState({
      id: cand.id,
      mode,
      startX: e.clientX,
      origStart: cand.start,
      origEnd: cand.end,
      trackWidth: Math.max(1, rect.width),
    });
  };

  const handleRegionPointerMove = (e: React.PointerEvent<HTMLElement>) => {
    if (!draggingState || safeDuration <= 0) return;
    const dxSec = ((e.clientX - draggingState.startX) / draggingState.trackWidth) * safeDuration;
    let nextStart = draggingState.origStart;
    let nextEnd = draggingState.origEnd;

    if (draggingState.mode === "left") {
      nextStart = Math.max(0, Math.min(draggingState.origEnd - MIN_CLIP_SEC, draggingState.origStart + dxSec));
      onSeek(nextStart);
    } else if (draggingState.mode === "right") {
      nextEnd = Math.min(safeDuration, Math.max(draggingState.origStart + MIN_CLIP_SEC, draggingState.origEnd + dxSec));
      onSeek(nextEnd);
    } else {
      const span = draggingState.origEnd - draggingState.origStart;
      nextStart = Math.max(0, Math.min(safeDuration - span, draggingState.origStart + dxSec));
      nextEnd = nextStart + span;
      onSeek(nextStart);
    }

    const roundedStart = Math.round(nextStart * 10) / 10;
    const roundedEnd = Math.round(nextEnd * 10) / 10;
    onChangeCandidates(
      candidatesRef.current.map((c) =>
        c.id === draggingState.id
          ? {
              ...c,
              start: roundedStart,
              end: roundedEnd,
              duration: Math.round((roundedEnd - roundedStart) * 10) / 10,
            }
          : c
      )
    );
  };

  const handleRegionPointerUp = (e: React.PointerEvent<HTMLElement>) => {
    if (!draggingState) return;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    setDraggingState(null);
  };

  const handleWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (!e.ctrlKey && !e.altKey) return;
    e.preventDefault();
    const vp = viewportRef.current;
    const prevZoom = zoom;
    const factor = e.deltaY < 0 ? 1.25 : 0.8;
    const nextZoom = Math.max(1, Math.min(20, Math.round(prevZoom * factor * 10) / 10));
    if (nextZoom === prevZoom) return;
    if (vp) {
      const rect = vp.getBoundingClientRect();
      const pointerOffset = e.clientX - rect.left;
      const contentX = vp.scrollLeft + pointerOffset;
      const ratio = contentX / Math.max(1, vp.scrollWidth);
      setZoom(nextZoom);
      requestAnimationFrame(() => {
        if (!viewportRef.current) return;
        viewportRef.current.scrollLeft = ratio * viewportRef.current.scrollWidth - pointerOffset;
      });
    } else {
      setZoom(nextZoom);
    }
  };

  const ticks = useMemo(() => {
    if (safeDuration <= 0) return [];
    const visibleSpan = safeDuration / zoom;
    const targetTickCount = 10;
    const idealStep = visibleSpan / targetTickCount;
    const step = TICK_STEPS.find((s) => s >= idealStep) ?? 1800;
    const out: number[] = [];
    for (let t = 0; t <= safeDuration; t += step) {
      out.push(t);
    }
    return out;
  }, [safeDuration, zoom]);

  const points = useMemo(() => timelineCurve?.points ?? [], [timelineCurve]);
  const hasChatSignal = useMemo(() => points.some((p) => p.chat > 0), [points]);

  const { hypeArea, hypeLine, audioLine, chatLine } = useMemo(() => {
    if (points.length === 0 || safeDuration <= 0) {
      return { hypeArea: "", hypeLine: "", audioLine: "", chatLine: "" };
    }
    let maxHype = 0.001;
    let maxAudio = 0.001;
    let maxChat = 0.001;
    for (const p of points) {
      if (p.hype > maxHype) maxHype = p.hype;
      const a = Math.max(p.audio, p.laugh);
      if (a > maxAudio) maxAudio = a;
      if (p.chat > maxChat) maxChat = p.chat;
    }
    const hypePts = points.map((p) => ({ t: p.t, val: p.hype }));
    const audioPts = points.map((p) => ({ t: p.t, val: Math.max(p.audio, p.laugh) }));
    const chatPts = points.map((p) => ({ t: p.t, val: p.chat }));
    return {
      hypeArea: buildPolylinePath(hypePts, safeDuration, maxHype, true),
      hypeLine: buildPolylinePath(hypePts, safeDuration, maxHype, false),
      audioLine: buildPolylinePath(audioPts, safeDuration, maxAudio, false),
      chatLine: hasChatSignal ? buildPolylinePath(chatPts, safeDuration, maxChat, false) : "",
    };
  }, [points, safeDuration, hasChatSignal]);

  const playheadPct =
    safeDuration > 0 ? Math.max(0, Math.min(100, (currentTime / safeDuration) * 100)) : 0;

  return (
    <Panel
      title="Timeline"
      count={candidates.length}
      index={3}
      action={
        <div className="flex flex-wrap items-center gap-(--space-xs)">
          <div className="hidden items-center gap-(--space-sm) text-xs text-(--color-ink-3) md:flex">
            <span className="inline-flex items-center gap-1.5">
              <span className="size-2 rounded-full bg-(--color-signal)" aria-hidden="true" />
              Hype
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span className="size-2 rounded-full bg-(--color-owed)" aria-hidden="true" />
              Audio/Laugh
            </span>
            {hasChatSignal && (
              <span className="inline-flex items-center gap-1.5">
                <span className="size-2 rounded-full bg-(--color-dusk)" aria-hidden="true" />
                Chat
              </span>
            )}
          </div>

          <span className="hidden rounded-full border border-(--rule-glass) bg-(--row-hover) px-2.5 py-0.5 text-[11px] text-(--color-ink-3) lg:inline-flex">
            Space Play · [ In · ] Out · J/L ±5s
          </span>

          <div className="inline-flex items-center gap-1 rounded-full border border-(--rule-glass) bg-(--row-hover) px-1.5 py-0.5">
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label="Zoom out timeline"
              disabled={zoom <= 1}
              onClick={() => setZoom((z) => Math.max(1, Math.round((z - 1) * 10) / 10))}
            >
              <ZoomOut className="size-3" />
            </Button>
            <input
              type="range"
              aria-label="Timeline zoom"
              min={1}
              max={20}
              step={0.5}
              value={zoom}
              onChange={(e) => setZoom(Number(e.target.value))}
              className="h-1 w-20 cursor-pointer accent-(--color-signal)"
            />
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label="Zoom in timeline"
              disabled={zoom >= 20}
              onClick={() => setZoom((z) => Math.min(20, Math.round((z + 1) * 10) / 10))}
            >
              <ZoomIn className="size-3" />
            </Button>
            <span className="min-w-9 text-center text-[11px] text-(--color-ink-2) tabular-nums">
              {zoom.toFixed(1)}×
            </span>
            <Button
              type="button"
              variant="ghost"
              size="xs"
              disabled={zoom === 1}
              onClick={() => {
                setZoom(1);
                if (viewportRef.current) viewportRef.current.scrollLeft = 0;
              }}
            >
              <Maximize2 className="size-3" />
              Fit
            </Button>
          </div>

          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={disabled || safeDuration <= 0}
            onClick={onAddClipAtPlayhead}
          >
            <Plus className="size-3.5" />
            Add clip at playhead
          </Button>
        </div>
      }
    >
      {safeDuration <= 0 ? (
        <div className="flex h-36 items-center justify-center rounded-(--radius-inner) border border-(--rule-glass) bg-(--color-paper-2)/50 text-sm text-(--color-ink-3)">
          Waiting for video duration metadata…
        </div>
      ) : (
        <div
          ref={viewportRef}
          onWheel={handleWheel}
          className="overflow-x-auto rounded-(--radius-inner) border border-(--rule-glass) bg-(--color-paper-2)/55 select-none"
        >
          <div
            ref={trackRef}
            className="relative"
            style={{ width: `${zoom * 100}%`, minWidth: "100%" }}
          >
            {/* Time ruler */}
            <div
              role="slider"
              aria-label="Timeline scrubber"
              aria-valuemin={0}
              aria-valuemax={Math.round(safeDuration)}
              aria-valuenow={Math.round(currentTime)}
              tabIndex={0}
              onPointerDown={handleRulerPointerDown}
              onPointerMove={handleRulerPointerMove}
              onPointerUp={handleRulerPointerUp}
              onPointerCancel={handleRulerPointerUp}
              className="relative h-7 cursor-ew-resize border-b border-(--rule-glass) bg-(--color-paper-3)/45 touch-none"
            >
              {ticks.map((t) => {
                const leftPct = (t / safeDuration) * 100;
                return (
                  <div
                    key={t}
                    style={{ left: `${leftPct}%` }}
                    className="pointer-events-none absolute top-0 bottom-0 flex flex-col justify-between"
                  >
                    <span className="h-2 w-px bg-(--color-ink-3)/50" />
                    <span className="-translate-x-1/2 pb-0.5 pl-1 text-[10px] text-(--color-ink-3) tabular-nums">
                      {formatRulerTime(t)}
                    </span>
                  </div>
                );
              })}
            </div>

            {/* Activity waveform lane */}
            <div
              onPointerDown={handleRulerPointerDown}
              onPointerMove={handleRulerPointerMove}
              onPointerUp={handleRulerPointerUp}
              onPointerCancel={handleRulerPointerUp}
              className="relative h-16 cursor-pointer border-b border-(--rule-glass) bg-(--color-paper)/40 touch-none"
            >
              {points.length > 0 ? (
                <svg
                  viewBox="0 0 1000 100"
                  preserveAspectRatio="none"
                  className="pointer-events-none h-full w-full"
                >
                  <line x1="0" y1="50" x2="1000" y2="50" stroke="var(--rule-glass)" strokeWidth="1" />
                  <path d={hypeArea} fill="var(--color-signal)" fillOpacity="0.22" />
                  <path
                    d={hypeLine}
                    fill="none"
                    stroke="var(--color-signal)"
                    strokeWidth="1.6"
                    vectorEffect="non-scaling-stroke"
                  />
                  <path
                    d={audioLine}
                    fill="none"
                    stroke="var(--color-owed)"
                    strokeWidth="1.2"
                    strokeOpacity="0.85"
                    vectorEffect="non-scaling-stroke"
                  />
                  {chatLine && (
                    <path
                      d={chatLine}
                      fill="none"
                      stroke="var(--color-dusk)"
                      strokeWidth="1.3"
                      strokeOpacity="0.9"
                      vectorEffect="non-scaling-stroke"
                    />
                  )}
                </svg>
              ) : (
                <div className="pointer-events-none flex h-full items-center justify-center text-xs text-(--color-ink-3)">
                  Activity curve appears after Analyze VOD (or add manual clips below)
                </div>
              )}
            </div>

            {/* Clip regions track */}
            <div
              onPointerDown={(e) => {
                if (e.target === e.currentTarget) {
                  handleRulerPointerDown(e);
                }
              }}
              onPointerMove={handleRulerPointerMove}
              onPointerUp={handleRulerPointerUp}
              onPointerCancel={handleRulerPointerUp}
              className="relative h-14 bg-(--color-paper-2)/40 touch-none"
            >
              {candidates.map((cand, idx) => {
                const leftPct = Math.max(0, Math.min(100, (cand.start / safeDuration) * 100));
                const widthPct = Math.max(0, Math.min(100 - leftPct, ((cand.end - cand.start) / safeDuration) * 100));
                const isSelected = cand.id === selectedId;
                const included = cand.selected !== false;
                const clipDur = Math.max(0, Math.round(cand.end - cand.start));
                const peakPct =
                  typeof cand.peak_sec === "number" &&
                  cand.peak_sec >= cand.start &&
                  cand.peak_sec <= cand.end &&
                  cand.end > cand.start
                    ? ((cand.peak_sec - cand.start) / (cand.end - cand.start)) * 100
                    : null;

                return (
                  <div
                    key={cand.id}
                    role="button"
                    tabIndex={0}
                    aria-label={`Clip ${idx + 1}: ${cand.title || "Untitled"}`}
                    onPointerDown={(e) => startRegionDrag(e, cand, "move")}
                    onPointerMove={handleRegionPointerMove}
                    onPointerUp={handleRegionPointerUp}
                    onPointerCancel={handleRegionPointerUp}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onSelect(cand.id);
                        onSeek(cand.start);
                      }
                    }}
                    style={{
                      left: `${leftPct}%`,
                      width: `${widthPct}%`,
                      minWidth: "18px",
                    }}
                    className={`group absolute top-2 bottom-2 flex items-center overflow-hidden rounded-[8px] transition-shadow ${
                      draggingState?.id === cand.id ? "cursor-grabbing" : "cursor-grab"
                    } ${
                      isSelected
                        ? "z-10 border-2 border-(--color-signal) bg-(--color-signal)/22 shadow-[0_0_0_1px_var(--color-signal),0_6px_18px_-4px_oklch(77%_0.1_274/0.45)]"
                        : included
                          ? "border border-(--color-signal)/45 bg-(--color-paper-3)/85 hover:border-(--color-signal)/80"
                          : "border border-dashed border-(--color-ink-3)/45 bg-(--color-paper-2)/50 opacity-55 hover:opacity-80"
                    }`}
                  >
                    {/* Left trim handle */}
                    <div
                      role="separator"
                      aria-label={`Trim start of clip ${idx + 1}`}
                      onPointerDown={(e) => startRegionDrag(e, cand, "left")}
                      onPointerMove={handleRegionPointerMove}
                      onPointerUp={handleRegionPointerUp}
                      onPointerCancel={handleRegionPointerUp}
                      className="absolute top-0 bottom-0 left-0 z-20 flex w-2.5 cursor-ew-resize items-center justify-center bg-(--color-owed)/20 opacity-75 transition-opacity group-hover:opacity-100 hover:bg-(--color-owed)/45"
                    >
                      <span className="h-4 w-0.5 rounded-full bg-(--color-owed)" />
                    </div>

                    {/* Payoff marker pip */}
                    {peakPct !== null && (
                      <span
                        title={`Peak moment at ${formatRulerTime(cand.peak_sec ?? 0)}`}
                        style={{ left: `${peakPct}%` }}
                        className="pointer-events-none absolute top-1 size-2 -translate-x-1/2 rounded-full bg-(--color-owed) shadow-[0_0_6px_var(--color-owed)]"
                      />
                    )}

                    {/* Compact label */}
                    <div className="pointer-events-none flex min-w-0 flex-1 items-center gap-1.5 px-3 text-[11px] font-medium tabular-nums">
                      <span className="shrink-0 text-foreground">#{idx + 1}</span>
                      <span className="shrink-0 text-(--color-ink-2)">{clipDur}s</span>
                      {typeof cand.score === "number" && (
                        <span className="truncate rounded-full bg-(--rule-glass) px-1.5 py-px text-[10px] text-(--color-signal-hi)">
                          {cand.score.toFixed(1)}
                        </span>
                      )}
                    </div>

                    {/* Right trim handle */}
                    <div
                      role="separator"
                      aria-label={`Trim end of clip ${idx + 1}`}
                      onPointerDown={(e) => startRegionDrag(e, cand, "right")}
                      onPointerMove={handleRegionPointerMove}
                      onPointerUp={handleRegionPointerUp}
                      onPointerCancel={handleRegionPointerUp}
                      className="absolute top-0 right-0 bottom-0 z-20 flex w-2.5 cursor-ew-resize items-center justify-center bg-(--color-owed)/20 opacity-75 transition-opacity group-hover:opacity-100 hover:bg-(--color-owed)/45"
                    >
                      <span className="h-4 w-0.5 rounded-full bg-(--color-owed)" />
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Full-height playhead needle */}
            <div
              aria-hidden="true"
              style={{ left: `${playheadPct}%` }}
              className="pointer-events-none absolute top-0 bottom-0 z-30 -translate-x-1/2"
            >
              <div className="mx-auto size-2.5 rotate-45 bg-(--color-signal) shadow-[0_0_8px_var(--color-signal)]" />
              <div className="mx-auto h-full w-0.5 bg-(--color-signal) shadow-[0_0_8px_var(--color-signal)]" />
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}
