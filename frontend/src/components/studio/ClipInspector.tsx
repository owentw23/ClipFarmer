"use client";

import { Play, Repeat, Scissors, Trash2 } from "lucide-react";

import { EmptyState, FieldLabel, Panel, Switch } from "@/components/page-kit";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StudioCandidate, StudioCandidateReason } from "@/lib/api";

export interface ClipInspectorProps {
  candidate: StudioCandidate | null;
  candidateIndex: number;
  duration: number;
  currentTime: number;
  looping: boolean;
  onChange: (updated: StudioCandidate) => void;
  onSeek: (sec: number) => void;
  onPlayClip: () => void;
  onDelete: (id: string) => void;
}

function formatClockDecimal(sec: number): string {
  const safe = Math.max(0, sec);
  const mins = Math.floor(safe / 60);
  const rem = safe - mins * 60;
  return `${String(mins).padStart(2, "0")}:${rem.toFixed(1).padStart(4, "0")}`;
}

function formatReasonLabel(reason: StudioCandidateReason): string {
  const code = reason.code.replace(/_/g, " ");
  if (reason.value === undefined || reason.value === null || reason.value === "") {
    return code;
  }
  if (typeof reason.value === "number") {
    return `${code} ${Number.isInteger(reason.value) ? reason.value : reason.value.toFixed(1)}`;
  }
  return `${code}: ${reason.value}`;
}

export function ClipInspector({
  candidate,
  candidateIndex,
  duration,
  currentTime,
  looping,
  onChange,
  onSeek,
  onPlayClip,
  onDelete,
}: ClipInspectorProps) {
  if (!candidate) {
    return (
      <Panel title="Clip inspector" index={2} className="h-full">
        <EmptyState icon={<Scissors />}>
          Select a clip region on the timeline or add a manual clip to edit its title, hook, and
          in/out boundaries.
        </EmptyState>
      </Panel>
    );
  }

  const maxDur = duration > 0 ? duration : Math.max(candidate.end, 3600);
  const clipDuration = Math.max(0, candidate.end - candidate.start);

  const updateBounds = (nextStart: number, nextEnd: number) => {
    const s = Math.max(0, Math.min(nextEnd - 3, Math.round(nextStart * 10) / 10));
    const e = Math.min(maxDur, Math.max(s + 3, Math.round(nextEnd * 10) / 10));
    onChange({
      ...candidate,
      start: s,
      end: e,
      duration: Math.round((e - s) * 10) / 10,
    });
  };

  return (
    <Panel
      title={`Clip #${candidateIndex + 1}`}
      index={2}
      className="h-full"
      action={
        <div className="flex items-center gap-(--space-xs)">
          <span className="text-xs text-(--color-ink-2)">Include in export</span>
          <Switch
            checked={candidate.selected !== false}
            label="Include in export"
            onChange={() => onChange({ ...candidate, selected: !(candidate.selected !== false) })}
          />
        </div>
      }
    >
      <div className="flex flex-col gap-(--space-md)">
        <div className="flex flex-col gap-(--space-sm)">
          <div>
            <FieldLabel htmlFor="inspector-title">Title</FieldLabel>
            <Input
              id="inspector-title"
              value={candidate.title}
              placeholder="Short punchy title"
              onChange={(e) => onChange({ ...candidate, title: e.target.value })}
            />
          </div>
          <div>
            <FieldLabel htmlFor="inspector-hook">Hook</FieldLabel>
            <Input
              id="inspector-hook"
              value={candidate.hook ?? ""}
              placeholder="Opening hook line"
              onChange={(e) => onChange({ ...candidate, hook: e.target.value })}
            />
          </div>
        </div>

        {(candidate.visual_summary ||
          candidate.category ||
          typeof candidate.score === "number" ||
          (candidate.reasons && candidate.reasons.length > 0)) && (
          <div className="flex flex-col gap-(--space-xs) rounded-(--radius-inner) border border-(--rule-glass) bg-(--color-paper-2)/65 p-(--space-sm)">
            <div className="flex flex-wrap items-center gap-1.5">
              {candidate.category && <Badge variant="secondary">{candidate.category}</Badge>}
              {typeof candidate.score === "number" && (
                <Badge variant="outline" className="border-(--color-signal)/40 text-(--color-signal) tabular-nums">
                  Score {candidate.score.toFixed(1)}
                </Badge>
              )}
              {typeof candidate.llm === "number" && (
                <Badge variant="outline" className="tabular-nums">
                  VLM {candidate.llm.toFixed(1)}/10
                </Badge>
              )}
              {typeof candidate.visual_action === "number" && (
                <Badge variant="outline" className="tabular-nums">
                  Visual {candidate.visual_action.toFixed(0)}/10
                </Badge>
              )}
              {(candidate.reasons ?? []).map((r, i) => (
                <Badge
                  key={`${r.code}-${i}`}
                  variant="outline"
                  className="border-(--rule-glass) text-(--color-ink-2)"
                >
                  {formatReasonLabel(r)}
                </Badge>
              ))}
            </div>
            {candidate.visual_summary && (
              <p className="text-xs leading-relaxed text-(--color-ink-2)">
                {candidate.visual_summary}
              </p>
            )}
          </div>
        )}

        <div className="grid grid-cols-1 gap-(--space-sm) sm:grid-cols-2">
          <div className="rounded-(--radius-inner) border border-(--rule-glass) bg-(--row-hover) p-(--space-xs)">
            <div className="mb-1 flex items-center justify-between text-xs text-(--color-ink-3)">
              <span>Start</span>
              <span className="font-medium text-foreground tabular-nums">
                {formatClockDecimal(candidate.start)}
              </span>
            </div>
            <div className="flex items-center gap-1">
              <Button
                type="button"
                variant="outline"
                size="xs"
                onClick={() => {
                  const next = Math.max(0, candidate.start - 2);
                  updateBounds(next, candidate.end);
                  onSeek(next);
                }}
              >
                -2s
              </Button>
              <Input
                type="number"
                step={0.1}
                min={0}
                max={Math.max(0, candidate.end - 3)}
                aria-label="Clip start seconds"
                value={candidate.start}
                onChange={(e) => updateBounds(Number(e.target.value) || 0, candidate.end)}
                className="h-6 px-1.5 text-xs tabular-nums"
              />
              <Button
                type="button"
                variant="outline"
                size="xs"
                onClick={() => {
                  const next = Math.min(candidate.end - 3, candidate.start + 2);
                  updateBounds(next, candidate.end);
                  onSeek(next);
                }}
              >
                +2s
              </Button>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="xs"
              className="mt-1 w-full justify-center text-(--color-signal)"
              onClick={() => updateBounds(currentTime, candidate.end)}
            >
              Set In [ ({formatClockDecimal(currentTime)})
            </Button>
          </div>

          <div className="rounded-(--radius-inner) border border-(--rule-glass) bg-(--row-hover) p-(--space-xs)">
            <div className="mb-1 flex items-center justify-between text-xs text-(--color-ink-3)">
              <span>End</span>
              <span className="font-medium text-foreground tabular-nums">
                {formatClockDecimal(candidate.end)}
              </span>
            </div>
            <div className="flex items-center gap-1">
              <Button
                type="button"
                variant="outline"
                size="xs"
                onClick={() => {
                  const next = Math.max(candidate.start + 3, candidate.end - 2);
                  updateBounds(candidate.start, next);
                  onSeek(next);
                }}
              >
                -2s
              </Button>
              <Input
                type="number"
                step={0.1}
                min={candidate.start + 3}
                max={maxDur}
                aria-label="Clip end seconds"
                value={candidate.end}
                onChange={(e) => updateBounds(candidate.start, Number(e.target.value) || candidate.start + 3)}
                className="h-6 px-1.5 text-xs tabular-nums"
              />
              <Button
                type="button"
                variant="outline"
                size="xs"
                onClick={() => {
                  const next = Math.min(maxDur, candidate.end + 2);
                  updateBounds(candidate.start, next);
                  onSeek(next);
                }}
              >
                +2s
              </Button>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="xs"
              className="mt-1 w-full justify-center text-(--color-signal)"
              onClick={() => updateBounds(candidate.start, currentTime)}
            >
              Set Out ] ({formatClockDecimal(currentTime)})
            </Button>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-(--space-xs) border-t border-(--rule-glass) pt-(--space-sm)">
          <div className="text-xs text-(--color-ink-3) tabular-nums">
            Duration:{" "}
            <span className="font-medium text-foreground">{formatClockDecimal(clipDuration)}</span> (
            {clipDuration.toFixed(1)}s)
          </div>
          <div className="flex items-center gap-(--space-xs)">
            <Button
              type="button"
              variant={looping ? "default" : "outline"}
              size="sm"
              onClick={onPlayClip}
            >
              {looping ? <Repeat className="size-3.5" /> : <Play className="size-3.5" />}
              {looping ? "Looping clip" : "Play clip"}
            </Button>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              onClick={() => onDelete(candidate.id)}
            >
              <Trash2 className="size-3.5" />
              Delete region
            </Button>
          </div>
        </div>
      </div>
    </Panel>
  );
}
