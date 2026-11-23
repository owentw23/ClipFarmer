"use client";

import { FormEvent, useState } from "react";

import { FieldLabel, StatusWord } from "@/components/page-kit";
import { DEFAULT_CAM_BOX, WebcamFramePicker } from "@/components/WebcamFramePicker";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { CamBox, ClipOpts, Streamer, updateStreamer } from "@/lib/api";

export const CAPTION_LABELS: Record<string, string> = { hormozi: "Hormozi", clean: "Clean", none: "None" };

type WebcamMode = "auto" | "custom" | "none";

const WEBCAM_MODE_LABELS: Record<WebcamMode, string> = {
  auto: "Find webcam automatically",
  custom: "Custom frame",
  none: "No webcam (full gameplay)",
};

/** Turn "gta6, #Lacy shorts" into ["#gta6", "#Lacy", "#shorts"] (the backend cleans it again). */
export function parseHashtags(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.replace(/,/g, " ").split(/\s+/)) {
    const word = raw.replace(/^#+/, "").replace(/[^\p{L}\p{N}_]/gu, "");
    if (!word || seen.has(word.toLowerCase())) continue;
    seen.add(word.toLowerCase());
    out.push(`#${word}`);
  }
  return out.slice(0, 15);
}

interface Draft {
  clipCount: number;
  minLen: number;
  maxLen: number;
  captions: string;
  webcamMode: WebcamMode;
  camBox: CamBox;
  hashtags: string;
}

function isCamBox(v: unknown): v is CamBox {
  if (!v || typeof v !== "object") return false;
  const o = v as Record<string, unknown>;
  return (
    typeof o.x === "number" &&
    typeof o.y === "number" &&
    typeof o.w === "number" &&
    typeof o.h === "number"
  );
}

function draftFrom(streamer: Streamer): Draft {
  const o: ClipOpts = streamer.clip_opts ?? {};
  const hasCustomBox = o.cam_auto === false && isCamBox(o.cam_box);
  const webcamMode: WebcamMode =
    o.cam_auto !== false
      ? "auto"
      : hasCustomBox
        ? "custom"
        : o.layout === "blur" || o.layout === "fill" || o.layout === "game"
          ? "none"
          : "custom";

  return {
    clipCount: typeof o.clip_count === "number" ? o.clip_count : 3,
    minLen: typeof o.min_len === "number" ? o.min_len : 30,
    maxLen: typeof o.max_len === "number" ? o.max_len : 60,
    captions: typeof o.caption_style === "string" ? o.caption_style : "hormozi",
    webcamMode,
    camBox: isCamBox(o.cam_box) ? o.cam_box : DEFAULT_CAM_BOX,
    hashtags: (o.hashtags ?? []).join(" "),
  };
}

function SettingsForm({
  streamer,
  onClose,
  onSaved,
  onModeChange,
}: {
  streamer: Streamer;
  onClose: () => void;
  onSaved: (updated: Streamer) => void;
  onModeChange: (mode: WebcamMode) => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => draftFrom(streamer));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((d) => ({ ...d, [key]: value }));
    if (key === "webcamMode") {
      onModeChange(value as WebcamMode);
    }
  };
  const tags = parseHashtags(draft.hashtags);

  const save = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (draft.minLen > draft.maxLen) {
      setError("The shortest clip can't be longer than the longest.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const clip_opts: Record<string, unknown> = {
        clip_count: draft.clipCount,
        min_len: draft.minLen,
        max_len: draft.maxLen,
        caption_style: draft.captions,
        hashtags: tags,
      };
      if (draft.webcamMode === "auto") {
        clip_opts.cam_auto = true;
        clip_opts.cam_box = null;
        clip_opts.layout = "stack";
      } else if (draft.webcamMode === "custom") {
        clip_opts.cam_auto = false;
        clip_opts.cam_box = draft.camBox;
        clip_opts.layout = "stack";
      } else {
        clip_opts.cam_auto = false;
        clip_opts.cam_box = null;
        clip_opts.layout = "blur";
      }

      const updated = await updateStreamer(streamer.id, { clip_opts });
      onSaved(updated);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save the settings");
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={save} className="grid gap-(--space-md)">
      <DialogHeader>
        <DialogTitle>Clip settings for {streamer.name}</DialogTitle>
        <DialogDescription>Used for every hour recorded from now on.</DialogDescription>
      </DialogHeader>

      {error && <StatusWord tone="error">{error}</StatusWord>}

      <div className="grid grid-cols-3 gap-(--space-sm)">
        <div>
          <FieldLabel htmlFor="set-count">Clips per hour</FieldLabel>
          <Input
            id="set-count"
            name="clip_count"
            type="number"
            inputMode="numeric"
            min={1}
            max={10}
            required
            value={draft.clipCount}
            onChange={(e) => set("clipCount", Math.max(1, Math.min(10, Number(e.target.value) || 1)))}
            className="tabular-nums"
          />
        </div>
        <div>
          <FieldLabel htmlFor="set-min">Min seconds</FieldLabel>
          <Input
            id="set-min"
            name="min_len"
            type="number"
            inputMode="numeric"
            min={5}
            max={300}
            value={draft.minLen}
            onChange={(e) => set("minLen", Number(e.target.value))}
            className="tabular-nums"
          />
        </div>
        <div>
          <FieldLabel htmlFor="set-max">Max seconds</FieldLabel>
          <Input
            id="set-max"
            name="max_len"
            type="number"
            inputMode="numeric"
            min={5}
            max={300}
            value={draft.maxLen}
            onChange={(e) => set("maxLen", Number(e.target.value))}
            className="tabular-nums"
          />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-(--space-sm) sm:grid-cols-2">
        <div>
          <FieldLabel>Captions</FieldLabel>
          <Select value={draft.captions} onValueChange={(val) => set("captions", val ?? "hormozi")}>
            <SelectTrigger className="w-full" aria-label="Captions">
              <SelectValue>{CAPTION_LABELS[draft.captions] ?? draft.captions}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {Object.entries(CAPTION_LABELS).map(([value, label]) => (
                <SelectItem key={value} value={value}>
                  {label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div>
          <FieldLabel>Webcam framing</FieldLabel>
          <Select
            value={draft.webcamMode}
            onValueChange={(val) => set("webcamMode", (val as WebcamMode) ?? "auto")}
          >
            <SelectTrigger className="w-full" aria-label="Webcam framing">
              <SelectValue>{WEBCAM_MODE_LABELS[draft.webcamMode]}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {(Object.entries(WEBCAM_MODE_LABELS) as Array<[WebcamMode, string]>).map(
                ([value, label]) => (
                  <SelectItem key={value} value={value}>
                    {label}
                  </SelectItem>
                )
              )}
            </SelectContent>
          </Select>
        </div>
      </div>

      {draft.webcamMode === "custom" && (
        <WebcamFramePicker
          streamer={streamer}
          value={draft.camBox}
          onChange={(box) => set("camBox", box)}
          onStreamerUpdated={onSaved}
        />
      )}

      <div>
        <FieldLabel htmlFor="set-tags">Hashtags for the description</FieldLabel>
        <Input
          id="set-tags"
          name="hashtags"
          autoComplete="off"
          placeholder="#gta6 #shorts #lacy"
          value={draft.hashtags}
          onChange={(e) => set("hashtags", e.target.value)}
        />
        <p className="mt-(--space-xs) min-h-[1lh] text-sm text-(--color-ink-3)">
          {tags.length ? `Added under each description: ${tags.join(" ")}` : "None. Separate tags with spaces or commas."}
        </p>
      </div>

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" disabled={saving} aria-busy={saving}>
          {saving ? "Saving" : "Save settings"}
        </Button>
      </DialogFooter>
    </form>
  );
}

export function StreamerSettingsDialog({
  streamer,
  onClose,
  onSaved,
}: {
  streamer: Streamer | null;
  onClose: () => void;
  onSaved: (updated: Streamer) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const isWide =
    expanded ||
    (streamer !== null &&
      streamer.clip_opts?.cam_auto === false &&
      isCamBox(streamer.clip_opts?.cam_box));

  return (
    <Dialog
      open={streamer !== null}
      onOpenChange={(open) => {
        if (!open) {
          setExpanded(false);
          onClose();
        }
      }}
    >
      <DialogContent
        className={`max-h-[90vh] overflow-y-auto ${isWide ? "sm:max-w-2xl" : "sm:max-w-lg"}`}
      >
        {streamer && (
          <SettingsForm
            key={streamer.id}
            streamer={streamer}
            onClose={() => {
              setExpanded(false);
              onClose();
            }}
            onSaved={onSaved}
            onModeChange={(mode) => setExpanded(mode === "custom")}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
