"use client";

import { useRef, useState } from "react";
import { Camera, Lock, RefreshCw, Unlock } from "lucide-react";

import { StatusWord } from "@/components/page-kit";
import { Button } from "@/components/ui/button";
import { CamBox, Streamer, getStreamerPreviewUrl, refreshStreamerPreview } from "@/lib/api";

const CAM_PANE_AR = 1080 / 614; // Matches layouts.py 'stack' top=0.32 (1080 x 614)
const SOURCE_AR = 16 / 9;
const BOX_NORM_AR = CAM_PANE_AR / SOURCE_AR; // ~0.989 in normalized (0..1) 16:9 coords
const MIN_SIZE = 0.08;

export const DEFAULT_CAM_BOX: CamBox = { x: 0.72, y: 0.68, w: 0.26, h: 0.26 };

const PRESETS: Array<{ label: string; box: CamBox }> = [
  { label: "Top-L", box: { x: 0.02, y: 0.03, w: 0.26, h: 0.26 } },
  { label: "Top-R", box: { x: 0.72, y: 0.03, w: 0.26, h: 0.26 } },
  { label: "Bot-L", box: { x: 0.02, y: 0.70, w: 0.26, h: 0.26 } },
  { label: "Bot-R", box: { x: 0.72, y: 0.70, w: 0.26, h: 0.26 } },
];

type DragMode = "move" | "nw" | "ne" | "sw" | "se" | "n" | "s" | "e" | "w";

function clampBox(b: CamBox): CamBox {
  const w = Math.max(MIN_SIZE, Math.min(1, b.w));
  const h = Math.max(MIN_SIZE, Math.min(1, b.h));
  const x = Math.max(0, Math.min(1 - w, b.x));
  const y = Math.max(0, Math.min(1 - h, b.y));
  return {
    x: Math.round(x * 10000) / 10000,
    y: Math.round(y * 10000) / 10000,
    w: Math.round(w * 10000) / 10000,
    h: Math.round(h * 10000) / 10000,
  };
}

export function WebcamFramePicker({
  streamer,
  value,
  onChange,
  onStreamerUpdated,
}: {
  streamer: Streamer;
  value: CamBox;
  onChange: (next: CamBox) => void;
  onStreamerUpdated?: (updated: Streamer) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [hasPreview, setHasPreview] = useState(Boolean(streamer.has_preview));
  const [previewVersion, setPreviewVersion] = useState<string | null>(
    streamer.preview_updated_at ?? null
  );
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [lockAspect, setLockAspect] = useState(true);
  const [dragging, setDragging] = useState(false);

  const previewUrl = hasPreview ? getStreamerPreviewUrl(streamer.id, previewVersion) : null;

  const handleRefresh = async () => {
    setRefreshing(true);
    setRefreshError(null);
    try {
      const updated = await refreshStreamerPreview(streamer.id);
      setHasPreview(Boolean(updated.has_preview));
      setPreviewVersion(updated.preview_updated_at ?? String(Date.now()));
      onStreamerUpdated?.(updated);
    } catch (err) {
      setRefreshError(
        err instanceof Error ? err.message : "Could not capture a live screenshot yet."
      );
    } finally {
      setRefreshing(false);
    }
  };

  const startDrag = (mode: DragMode, e: React.PointerEvent<HTMLElement>) => {
    e.preventDefault();
    e.stopPropagation();
    const el = containerRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const startX = e.clientX;
    const startY = e.clientY;
    const orig = { ...value };
    setDragging(true);

    const onMove = (ev: PointerEvent) => {
      const dx = (ev.clientX - startX) / rect.width;
      const dy = (ev.clientY - startY) / rect.height;

      if (mode === "move") {
        onChange(clampBox({ x: orig.x + dx, y: orig.y + dy, w: orig.w, h: orig.h }));
        return;
      }

      let x = orig.x;
      let y = orig.y;
      let w = orig.w;
      let h = orig.h;

      if (mode.includes("e")) w = orig.w + dx;
      if (mode.includes("s")) h = orig.h + dy;
      if (mode.includes("w")) {
        const clampedDx = Math.min(dx, orig.w - MIN_SIZE);
        x = Math.max(0, orig.x + clampedDx);
        w = orig.x + orig.w - x;
      }
      if (mode.includes("n")) {
        const clampedDy = Math.min(dy, orig.h - MIN_SIZE);
        y = Math.max(0, orig.y + clampedDy);
        h = orig.y + orig.h - y;
      }

      w = Math.max(MIN_SIZE, Math.min(1 - x, w));
      h = Math.max(MIN_SIZE, Math.min(1 - y, h));

      if (lockAspect) {
        if (mode === "n" || mode === "s") {
          w = h * BOX_NORM_AR;
          if (x + w > 1) {
            w = 1 - x;
            h = w / BOX_NORM_AR;
          }
        } else {
          h = w / BOX_NORM_AR;
          if (mode.includes("n")) {
            y = Math.max(0, orig.y + orig.h - h);
            h = orig.y + orig.h - y;
            w = h * BOX_NORM_AR;
          } else if (y + h > 1) {
            h = 1 - y;
            w = h * BOX_NORM_AR;
          }
        }
      }

      onChange(clampBox({ x, y, w, h }));
    };

    const onUp = () => {
      setDragging(false);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  return (
    <div className="grid gap-(--space-sm) rounded-(--radius-inner) border border-(--rule-glass) bg-(--color-paper-2)/60 p-(--space-sm)">
      <div className="flex flex-wrap items-center justify-between gap-(--space-xs)">
        <div className="flex flex-wrap items-center gap-(--space-2xs)">
          {PRESETS.map((preset) => (
            <Button
              key={preset.label}
              type="button"
              variant="outline"
              size="xs"
              onClick={() => onChange(preset.box)}
            >
              {preset.label}
            </Button>
          ))}
          <Button
            type="button"
            variant="outline"
            size="xs"
            onClick={() => setLockAspect((v) => !v)}
            aria-pressed={lockAspect}
          >
            {lockAspect ? <Lock className="size-3" /> : <Unlock className="size-3" />}
            {lockAspect ? "16:9 locked" : "Free resize"}
          </Button>
        </div>

        <Button
          type="button"
          variant="outline"
          size="xs"
          disabled={refreshing}
          aria-busy={refreshing}
          onClick={() => void handleRefresh()}
        >
          <RefreshCw className={`size-3 ${refreshing ? "animate-spin" : ""}`} />
          {refreshing ? "Capturing" : "Refresh frame"}
        </Button>
      </div>

      {refreshError && <StatusWord tone="error">{refreshError}</StatusWord>}

      <div
        ref={containerRef}
        className="relative aspect-video w-full overflow-hidden rounded-(--radius-inner) border border-(--rule-glass) bg-black select-none touch-none"
      >
        {previewUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={previewUrl}
            alt={`Live frame for ${streamer.name}`}
            draggable={false}
            onError={() => setHasPreview(false)}
            className="pointer-events-none h-full w-full object-fill"
          />
        ) : (
          <div className="flex h-full w-full flex-col items-center justify-center gap-(--space-xs) px-(--space-md) text-center text-sm text-(--color-ink-3)">
            <Camera className="size-5 opacity-60" />
            <span>
              No live screenshot yet. One is captured automatically when {streamer.name} goes live,
              or click Refresh frame.
            </span>
          </div>
        )}

        {/* Draggable & resizable webcam quadrilateral */}
        <div
          role="application"
          aria-label="Webcam crop box"
          onPointerDown={(e) => startDrag("move", e)}
          style={{
            left: `${value.x * 100}%`,
            top: `${value.y * 100}%`,
            width: `${value.w * 100}%`,
            height: `${value.h * 100}%`,
            boxShadow: "0 0 0 9999px rgba(8, 10, 18, 0.56)",
          }}
          className="absolute cursor-move border-2 border-(--color-signal) bg-(--color-signal)/10"
        >
          {dragging && (
            <div className="pointer-events-none absolute inset-0 grid grid-cols-3 grid-rows-3 opacity-35">
              <div className="border-r border-b border-white/60" />
              <div className="border-r border-b border-white/60" />
              <div className="border-b border-white/60" />
              <div className="border-r border-b border-white/60" />
              <div className="border-r border-b border-white/60" />
              <div className="border-b border-white/60" />
              <div className="border-r border-white/60" />
              <div className="border-r border-white/60" />
              <div />
            </div>
          )}

          {/* 4 corner resize handles */}
          {(
            [
              ["nw", "-top-1.5 -left-1.5 cursor-nwse-resize"],
              ["ne", "-top-1.5 -right-1.5 cursor-nesw-resize"],
              ["sw", "-bottom-1.5 -left-1.5 cursor-nesw-resize"],
              ["se", "-bottom-1.5 -right-1.5 cursor-nwse-resize"],
            ] as const
          ).map(([corner, pos]) => (
            <button
              key={corner}
              type="button"
              aria-label={`Resize ${corner}`}
              onPointerDown={(e) => startDrag(corner, e)}
              className={`absolute size-3.5 rounded-full border border-(--color-signal-ink) bg-(--color-signal) ${pos}`}
            />
          ))}

          {/* 4 edge resize handles */}
          {(
            [
              ["n", "-top-1 left-1/2 -translate-x-1/2 h-2 w-6 cursor-ns-resize"],
              ["s", "-bottom-1 left-1/2 -translate-x-1/2 h-2 w-6 cursor-ns-resize"],
              ["w", "top-1/2 -left-1 -translate-y-1/2 h-6 w-2 cursor-ew-resize"],
              ["e", "top-1/2 -right-1 -translate-y-1/2 h-6 w-2 cursor-ew-resize"],
            ] as const
          ).map(([edge, pos]) => (
            <button
              key={edge}
              type="button"
              aria-label={`Resize ${edge}`}
              onPointerDown={(e) => startDrag(edge, e)}
              className={`absolute rounded-full bg-(--color-signal)/85 ${pos}`}
            />
          ))}
        </div>
      </div>

      {/* Live cropped top-pane preview + coordinates */}
      <div className="flex flex-wrap items-center justify-between gap-(--space-sm)">
        <div className="flex items-center gap-(--space-sm)">
          <div className="relative aspect-[1080/614] w-28 overflow-hidden rounded-md border border-(--rule-glass) bg-black">
            {previewUrl ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={previewUrl}
                alt="Cropped webcam preview"
                draggable={false}
                style={{
                  width: `${100 / value.w}%`,
                  height: `${100 / value.h}%`,
                  maxWidth: "none",
                  transform: `translate(${-value.x * 100}%, ${-value.y * 100}%)`,
                }}
                className="pointer-events-none block"
              />
            ) : (
              <div className="flex h-full w-full items-center justify-center text-[10px] text-(--color-ink-3)">
                Preview
              </div>
            )}
          </div>
          <div className="text-xs text-(--color-ink-3) tabular-nums">
            <div className="font-medium text-(--color-ink-2)">Top cam pane preview</div>
            <div>
              x {Math.round(value.x * 100)}% · y {Math.round(value.y * 100)}% ·{" "}
              {Math.round(value.w * 100)}% × {Math.round(value.h * 100)}%
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

