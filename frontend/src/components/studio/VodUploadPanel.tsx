"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import {
  Download,
  Film,
  FolderOpen,
  Link2,
  Lock,
  MessageSquareText,
  Plus,
  RefreshCw,
  Scissors,
  Sparkles,
  Trash2,
  Unlock,
  Upload,
} from "lucide-react";
import { toast } from "sonner";

import {
  Checkbox,
  DotTabs,
  EmptyState,
  FieldLabel,
  Panel,
  StatusWord,
  Tone,
  fileBasename,
  formatDateTime,
  rise,
  statusLabel,
} from "@/components/page-kit";
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
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  CamBox,
  RecordingChunk,
  Streamer,
  StudioOptions,
  StudioProfile,
  StudioProject,
  StudioVodDownloader,
  StudioVodQuality,
  createStudioProject,
  createStudioProjectFromUrl,
  deleteStudioProject,
  getStudioPreviewUrl,
  getSystemRecordings,
  uploadStudioFileChunked,
} from "@/lib/api";

type SourceTab = "upload" | "url" | "chunks" | "projects";
type WebcamMode = "auto" | "custom" | "none";

const DOWNLOADER_LABELS: Record<StudioVodDownloader, string> = {
  auto: "Auto (Streamlink for Twitch, yt-dlp for YouTube)",
  streamlink: "Streamlink",
  "yt-dlp": "yt-dlp",
};

const QUALITY_LABELS: Record<StudioVodQuality, string> = {
  "1080p": "1080p 60fps",
  "720p": "720p 60fps (faster / smaller)",
};

const PROFILE_LABELS: Record<StudioProfile, string> = {
  balanced: "Balanced",
  action: "Action",
  dialogue: "Dialogue",
};

const CAPTION_LABELS: Record<string, string> = {
  hormozi: "Hormozi",
  clean: "Clean",
  bold: "Bold",
  none: "None",
};

const WEBCAM_MODE_LABELS: Record<WebcamMode, string> = {
  auto: "Find webcam automatically",
  custom: "Custom frame",
  none: "No webcam (full gameplay)",
};

const CAM_PANE_AR = 1080 / 614;
const SOURCE_AR = 16 / 9;
const BOX_NORM_AR = CAM_PANE_AR / SOURCE_AR;
const MIN_BOX_SIZE = 0.08;

const PRESETS: Array<{ label: string; box: CamBox }> = [
  { label: "Top-L", box: { x: 0.02, y: 0.03, w: 0.26, h: 0.26 } },
  { label: "Top-R", box: { x: 0.72, y: 0.03, w: 0.26, h: 0.26 } },
  { label: "Bot-L", box: { x: 0.02, y: 0.7, w: 0.26, h: 0.26 } },
  { label: "Bot-R", box: { x: 0.72, y: 0.7, w: 0.26, h: 0.26 } },
];

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

function clampBox(b: CamBox): CamBox {
  const w = Math.max(MIN_BOX_SIZE, Math.min(1, b.w));
  const h = Math.max(MIN_BOX_SIZE, Math.min(1, b.h));
  const x = Math.max(0, Math.min(1 - w, b.x));
  const y = Math.max(0, Math.min(1 - h, b.y));
  return {
    x: Math.round(x * 10000) / 10000,
    y: Math.round(y * 10000) / 10000,
    w: Math.round(w * 10000) / 10000,
    h: Math.round(h * 10000) / 10000,
  };
}

function formatDuration(sec?: number | null): string {
  if (typeof sec !== "number" || Number.isNaN(sec) || sec <= 0) return "--";
  const total = Math.round(sec);
  const hrs = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hrs > 0) return `${hrs}h ${String(mins).padStart(2, "0")}m`;
  if (mins > 0) return `${mins}m ${String(secs).padStart(2, "0")}s`;
  return `${secs}s`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function studioStatusTone(status?: string | null): Tone {
  const s = (status ?? "").toLowerCase();
  if (s === "analyzed" || s === "exported") return "done";
  if (s === "downloading" || s === "analyzing" || s === "exporting") return "owed";
  if (s === "error") return "error";
  return "quiet";
}

function StudioPreviewCamPicker({
  project,
  value,
  onChange,
}: {
  project: StudioProject;
  value: CamBox;
  onChange: (next: CamBox) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [imgFailed, setImgFailed] = useState(false);
  const [lockAspect, setLockAspect] = useState(true);
  const [dragging, setDragging] = useState(false);

  const previewUrl = project.has_preview && !imgFailed ? getStudioPreviewUrl(project.id, project.updated_at) : null;

  const startDrag = (
    mode: "move" | "nw" | "ne" | "sw" | "se" | "n" | "s" | "e" | "w",
    e: React.PointerEvent<HTMLElement>
  ) => {
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
        const clampedDx = Math.min(dx, orig.w - MIN_BOX_SIZE);
        x = Math.max(0, orig.x + clampedDx);
        w = orig.x + orig.w - x;
      }
      if (mode.includes("n")) {
        const clampedDy = Math.min(dy, orig.h - MIN_BOX_SIZE);
        y = Math.max(0, orig.y + clampedDy);
        h = orig.y + orig.h - y;
      }
      w = Math.max(MIN_BOX_SIZE, Math.min(1 - x, w));
      h = Math.max(MIN_BOX_SIZE, Math.min(1 - y, h));
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
            <Button key={preset.label} type="button" variant="outline" size="xs" onClick={() => onChange(preset.box)}>
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
        <span className="text-xs text-(--color-ink-3) tabular-nums">
          x {Math.round(value.x * 100)}% · y {Math.round(value.y * 100)}% · {Math.round(value.w * 100)}% ×{" "}
          {Math.round(value.h * 100)}%
        </span>
      </div>

      <div
        ref={containerRef}
        className="relative aspect-video w-full overflow-hidden rounded-(--radius-inner) border border-(--rule-glass) bg-black select-none touch-none"
      >
        {previewUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={previewUrl}
            alt={`Preview frame for ${project.title}`}
            draggable={false}
            onError={() => setImgFailed(true)}
            className="pointer-events-none h-full w-full object-fill"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center px-(--space-md) text-center text-sm text-(--color-ink-3)">
            Preview frame unavailable; position the webcam crop box relative to 16:9 video.
          </div>
        )}

        <div
          role="application"
          aria-label="Studio webcam crop box"
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
    </div>
  );
}

export interface VodUploadPanelProps {
  project: StudioProject | null;
  projects: StudioProject[];
  streamers: Streamer[];
  options: StudioOptions;
  onOptionsChange: (next: StudioOptions) => void;
  onProjectSelected: (project: StudioProject) => void;
  onProjectDeleted: (id: number) => void;
  onProjectsRefresh: () => Promise<void>;
  onAnalyze: (options: StudioOptions) => Promise<void>;
  onAddManualClip?: () => void;
  analyzing: boolean;
}

export function VodUploadPanel({
  project,
  projects,
  streamers,
  options,
  onOptionsChange,
  onProjectSelected,
  onProjectDeleted,
  onProjectsRefresh,
  onAnalyze,
  onAddManualClip,
  analyzing,
}: VodUploadPanelProps) {
  const [tab, setTab] = useState<SourceTab>(projects.length > 0 && !project ? "projects" : "upload");
  const [chunks, setChunks] = useState<RecordingChunk[] | null>(null);
  const [loadingChunks, setLoadingChunks] = useState(false);

  // Upload state
  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [chatFile, setChatFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [selectedStreamerId, setSelectedStreamerId] = useState<string>("none");
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadPct, setUploadPct] = useState(0);
  const [uploadStage, setUploadStage] = useState("");
  const [uploadSpeedMb, setUploadSpeedMb] = useState<number | null>(null);
  const [openingChunkId, setOpeningChunkId] = useState<number | null>(null);
  const [deletingTarget, setDeletingTarget] = useState<StudioProject | null>(null);
  const [deleting, setDeleting] = useState(false);

  // VOD URL download state
  const [vodUrl, setVodUrl] = useState("");
  const [urlTitle, setUrlTitle] = useState("");
  const [urlStartTime, setUrlStartTime] = useState("");
  const [urlEndTime, setUrlEndTime] = useState("");
  const [urlDownloader, setUrlDownloader] = useState<StudioVodDownloader>("auto");
  const [urlQuality, setUrlQuality] = useState<StudioVodQuality>("1080p");
  const [urlFetchChat, setUrlFetchChat] = useState(true);
  const [urlAutoAnalyze, setUrlAutoAnalyze] = useState(false);
  const [startingUrlDownload, setStartingUrlDownload] = useState(false);

  const videoInputRef = useRef<HTMLInputElement>(null);
  const chatInputRef = useRef<HTMLInputElement>(null);

  const loadChunks = useCallback(async () => {
    setLoadingChunks(true);
    try {
      const res = await getSystemRecordings();
      const available = (res.recent_chunks ?? []).filter(
        (c) => c.status !== "deleted" && c.status !== "discarded_short"
      );
      setChunks(available);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't load recorded chunks");
    } finally {
      setLoadingChunks(false);
    }
  }, []);

  const handleTabChange = (nextTab: SourceTab) => {
    setTab(nextTab);
    if (nextTab === "chunks" && chunks === null && !loadingChunks) {
      void loadChunks();
    }
  };

  const streamerById = useMemo(() => {
    const map = new Map<number, Streamer>();
    for (const s of streamers) map.set(s.id, s);
    return map;
  }, [streamers]);

  const applyStreamerDefaults = (streamerIdVal: string) => {
    setSelectedStreamerId(streamerIdVal);
    if (streamerIdVal === "none") return;
    const s = streamerById.get(Number(streamerIdVal));
    if (!s?.clip_opts) return;
    const o = s.clip_opts;
    onOptionsChange({
      ...options,
      clip_count: typeof o.clip_count === "number" ? o.clip_count : options.clip_count ?? 6,
      min_len: typeof o.min_len === "number" ? o.min_len : options.min_len ?? 20,
      max_len: typeof o.max_len === "number" ? o.max_len : options.max_len ?? 60,
      caption_style: typeof o.caption_style === "string" ? o.caption_style : options.caption_style ?? "hormozi",
      cam_auto: typeof o.cam_auto === "boolean" ? o.cam_auto : options.cam_auto ?? true,
      cam_box: isCamBox(o.cam_box) ? o.cam_box : null,
      layout: typeof o.layout === "string" ? o.layout : options.layout ?? "stack",
      fallback_layout: typeof o.fallback_layout === "string" ? o.fallback_layout : options.fallback_layout ?? "blur",
      language: o.language ?? options.language ?? null,
    });
  };

  const handleVideoPick = (file: File | null) => {
    if (!file) return;
    setVideoFile(file);
    if (!title.trim()) {
      setTitle(file.name.replace(/\.[^/.]+$/, ""));
    }
  };

  const handleUploadAndCreate = async () => {
    if (!videoFile) {
      toast.error("Choose a video file first.");
      return;
    }
    setUploading(true);
    setUploadPct(0);
    setUploadSpeedMb(null);
    const startTs = performance.now();
    try {
      let chatUploadId: string | undefined;
      if (chatFile) {
        setUploadStage("Uploading chat replay…");
        const chatRes = await uploadStudioFileChunked(chatFile, "chat");
        chatUploadId = chatRes.upload_id;
      }

      setUploadStage("Uploading video…");
      const videoRes = await uploadStudioFileChunked(videoFile, "video", (pct) => {
        setUploadPct(pct);
        const elapsedSec = Math.max(0.2, (performance.now() - startTs) / 1000);
        const bytesDone = (videoFile.size * pct) / 100;
        setUploadSpeedMb(bytesDone / (1024 * 1024) / elapsedSec);
      });

      setUploadStage("Creating Studio project…");
      const created = await createStudioProject({
        title: title.trim() || videoFile.name.replace(/\.[^/.]+$/, ""),
        source_type: "upload",
        upload_id: videoRes.upload_id,
        chat_upload_id: chatUploadId,
        streamer_id: selectedStreamerId !== "none" ? Number(selectedStreamerId) : undefined,
        options,
      });

      toast.success(`Project "${created.title}" is ready`);
      setVideoFile(null);
      setChatFile(null);
      setTitle("");
      setUploadPct(0);
      setUploadStage("");
      await onProjectsRefresh();
      onProjectSelected(created);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  const handleDownloadFromUrl = async () => {
    const cleanUrl = vodUrl.trim();
    if (!cleanUrl) {
      toast.error("Paste a Twitch or YouTube VOD URL first.");
      return;
    }
    setStartingUrlDownload(true);
    try {
      const created = await createStudioProjectFromUrl({
        url: cleanUrl,
        title: urlTitle.trim() || undefined,
        streamer_id: selectedStreamerId !== "none" ? Number(selectedStreamerId) : undefined,
        downloader: urlDownloader,
        quality: urlQuality,
        start_time: urlStartTime.trim() || undefined,
        end_time: urlEndTime.trim() || undefined,
        fetch_chat: urlFetchChat,
        auto_analyze: urlAutoAnalyze,
        options,
      });
      toast.success(`Downloading "${created.title}"…`);
      setVodUrl("");
      setUrlTitle("");
      setUrlStartTime("");
      setUrlEndTime("");
      await onProjectsRefresh();
      onProjectSelected(created);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't start VOD download");
    } finally {
      setStartingUrlDownload(false);
    }
  };

  const handleOpenChunk = async (chunk: RecordingChunk) => {
    setOpeningChunkId(chunk.id);
    try {
      const s = streamerById.get(chunk.streamer_id);
      const chunkOpts: StudioOptions = s?.clip_opts
        ? {
            ...options,
            clip_count: typeof s.clip_opts.clip_count === "number" ? s.clip_opts.clip_count : options.clip_count,
            min_len: typeof s.clip_opts.min_len === "number" ? s.clip_opts.min_len : options.min_len,
            max_len: typeof s.clip_opts.max_len === "number" ? s.clip_opts.max_len : options.max_len,
            caption_style:
              typeof s.clip_opts.caption_style === "string"
                ? s.clip_opts.caption_style
                : options.caption_style,
            cam_auto: typeof s.clip_opts.cam_auto === "boolean" ? s.clip_opts.cam_auto : options.cam_auto,
            cam_box: isCamBox(s.clip_opts.cam_box) ? s.clip_opts.cam_box : options.cam_box,
            layout: typeof s.clip_opts.layout === "string" ? s.clip_opts.layout : options.layout,
          }
        : options;

      const created = await createStudioProject({
        title: `${chunk.streamer_name || chunk.streamer_slug || `Streamer ${chunk.streamer_id}`} · Chunk #${chunk.chunk_index}`,
        source_type: "chunk",
        chunk_id: chunk.id,
        streamer_id: chunk.streamer_id,
        options: chunkOpts,
      });
      await onProjectsRefresh();
      onProjectSelected(created);
      toast.success("Opened recorded chunk in Studio");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't open recorded chunk");
    } finally {
      setOpeningChunkId(null);
    }
  };

  const handleConfirmDelete = async () => {
    if (!deletingTarget) return;
    setDeleting(true);
    try {
      await deleteStudioProject(deletingTarget.id);
      onProjectDeleted(deletingTarget.id);
      setDeletingTarget(null);
      toast.success("Studio project deleted");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't delete project");
    } finally {
      setDeleting(false);
    }
  };

  const webcamMode: WebcamMode =
    options.cam_auto !== false
      ? "auto"
      : isCamBox(options.cam_box)
        ? "custom"
        : options.layout === "blur" || options.layout === "fill" || options.layout === "game"
          ? "none"
          : "custom";

  const handleWebcamModeChange = (mode: WebcamMode) => {
    if (mode === "auto") {
      onOptionsChange({ ...options, cam_auto: true, cam_box: null, layout: "stack" });
    } else if (mode === "custom") {
      onOptionsChange({
        ...options,
        cam_auto: false,
        cam_box: isCamBox(options.cam_box) ? options.cam_box : DEFAULT_CAM_BOX,
        layout: "stack",
      });
    } else {
      onOptionsChange({ ...options, cam_auto: false, cam_box: null, layout: "blur" });
    }
  };

  const activeStreamer =
    project?.streamer_id != null
      ? streamerById.get(project.streamer_id) ?? null
      : selectedStreamerId !== "none"
        ? streamerById.get(Number(selectedStreamerId)) ?? null
        : null;

  const busyStatus =
    project?.status === "downloading" ||
    project?.status === "analyzing" ||
    project?.status === "exporting";

  return (
    <div className="grid grid-cols-1 gap-(--space-lg) lg:grid-cols-12">
      <Panel
        title="Source VOD"
        index={1}
        className="lg:col-span-7"
        action={
          <DotTabs
            value={tab}
            onChange={handleTabChange}
            label="VOD source"
            options={[
              { value: "upload", label: "Upload video" },
              { value: "url", label: "VOD URL" },
              { value: "chunks", label: "Recorded chunks" },
              { value: "projects", label: `Saved projects (${projects.length})` },
            ]}
          />
        }
      >
        {tab === "upload" && (
          <div className="flex flex-col gap-(--space-md)">
            <div
              role="button"
              tabIndex={0}
              onClick={() => !uploading && videoInputRef.current?.click()}
              onKeyDown={(e) => {
                if ((e.key === "Enter" || e.key === " ") && !uploading) {
                  e.preventDefault();
                  videoInputRef.current?.click();
                }
              }}
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                if (uploading) return;
                const dropped = e.dataTransfer.files?.[0];
                if (dropped) handleVideoPick(dropped);
              }}
              className={`flex cursor-pointer flex-col items-center justify-center gap-(--space-xs) rounded-(--radius-inner) border border-dashed px-(--space-lg) py-(--space-xl) text-center transition-colors ${
                dragOver
                  ? "border-(--color-signal) bg-(--color-signal)/10"
                  : "border-(--rule-glass) bg-(--row-hover) hover:border-(--color-signal)/60"
              }`}
            >
              <Upload className="size-6 text-(--color-signal)" aria-hidden="true" />
              {videoFile ? (
                <div className="flex flex-col gap-1">
                  <span className="font-medium text-foreground">{videoFile.name}</span>
                  <span className="text-xs text-(--color-ink-3) tabular-nums">
                    {formatBytes(videoFile.size)} · click or drop to replace
                  </span>
                </div>
              ) : (
                <div className="flex flex-col gap-1">
                  <span className="font-medium text-foreground">Drop a VOD here or browse</span>
                  <span className="text-xs text-(--color-ink-3)">
                    MP4, MKV, MOV, or WebM · streamed in 16 MB chunks directly to local storage
                  </span>
                </div>
              )}
              <input
                ref={videoInputRef}
                type="file"
                accept=".mp4,.mkv,.mov,.webm,video/*"
                className="hidden"
                onChange={(e) => handleVideoPick(e.target.files?.[0] ?? null)}
              />
            </div>

            <div className="grid grid-cols-1 gap-(--space-sm) sm:grid-cols-2">
              <div>
                <FieldLabel htmlFor="studio-upload-title">Project title</FieldLabel>
                <Input
                  id="studio-upload-title"
                  placeholder="Optional title (defaults to filename)"
                  value={title}
                  disabled={uploading}
                  onChange={(e) => setTitle(e.target.value)}
                />
              </div>

              <div>
                <FieldLabel>Chat replay (optional)</FieldLabel>
                <div className="flex items-center gap-(--space-xs)">
                  <Button
                    type="button"
                    variant="outline"
                    className="w-full justify-start truncate"
                    disabled={uploading}
                    onClick={() => chatInputRef.current?.click()}
                  >
                    <MessageSquareText className="size-3.5 shrink-0 text-(--color-dusk)" />
                    <span className="truncate">
                      {chatFile ? chatFile.name : "Attach .json / .jsonl / .txt"}
                    </span>
                  </Button>
                  {chatFile && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      disabled={uploading}
                      aria-label="Remove chat file"
                      onClick={() => setChatFile(null)}
                    >
                      <Trash2 className="size-3.5" />
                    </Button>
                  )}
                  <input
                    ref={chatInputRef}
                    type="file"
                    accept=".json,.jsonl,.txt"
                    className="hidden"
                    onChange={(e) => setChatFile(e.target.files?.[0] ?? null)}
                  />
                </div>
              </div>
            </div>

            {uploading && (
              <div className="flex flex-col gap-(--space-xs) rounded-(--radius-inner) border border-(--rule-glass) bg-(--color-paper-2)/70 p-(--space-sm)">
                <div className="flex items-center justify-between text-xs tabular-nums">
                  <StatusWord tone="owed" busy>
                    {uploadStage || "Uploading…"}
                  </StatusWord>
                  <span className="text-(--color-ink-2)">
                    {uploadPct}%
                    {uploadSpeedMb !== null ? ` · ${uploadSpeedMb.toFixed(1)} MB/s` : ""}
                  </span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-(--rule-glass)">
                  <div
                    className="h-full rounded-full bg-(--color-signal) transition-[width] duration-150"
                    style={{ width: `${uploadPct}%` }}
                  />
                </div>
              </div>
            )}

            <div className="flex items-center justify-between gap-(--space-sm)">
              <span className="text-xs text-(--color-ink-3)">
                Uploaded VODs stay in local studio storage until you delete the project.
              </span>
              <Button
                type="button"
                disabled={!videoFile || uploading}
                aria-busy={uploading}
                onClick={() => void handleUploadAndCreate()}
              >
                <Upload className="size-3.5" />
                {uploading ? "Uploading…" : "Upload & Open"}
              </Button>
            </div>
          </div>
        )}

        {tab === "url" && (
          <div className="flex flex-col gap-(--space-md)">
            <div>
              <FieldLabel htmlFor="studio-vod-url">Twitch or YouTube VOD URL</FieldLabel>
              <div className="relative">
                <Link2
                  className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-(--color-ink-3)"
                  aria-hidden="true"
                />
                <Input
                  id="studio-vod-url"
                  type="url"
                  placeholder="https://www.twitch.tv/videos/1234567890 or https://www.youtube.com/watch?v=..."
                  value={vodUrl}
                  disabled={startingUrlDownload}
                  onChange={(e) => setVodUrl(e.target.value)}
                  className="pl-9"
                />
              </div>
            </div>

            <div className="grid grid-cols-1 gap-(--space-sm) sm:grid-cols-3">
              <div>
                <FieldLabel htmlFor="studio-url-title">Project title (optional)</FieldLabel>
                <Input
                  id="studio-url-title"
                  placeholder="Defaults to VOD ID"
                  value={urlTitle}
                  disabled={startingUrlDownload}
                  onChange={(e) => setUrlTitle(e.target.value)}
                />
              </div>

              <div>
                <FieldLabel>Quality (60fps)</FieldLabel>
                <Select
                  value={urlQuality}
                  disabled={startingUrlDownload}
                  onValueChange={(v) => setUrlQuality((v as StudioVodQuality) ?? "1080p")}
                >
                  <SelectTrigger className="w-full" aria-label="VOD download quality">
                    <SelectValue>{QUALITY_LABELS[urlQuality]}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {(Object.keys(QUALITY_LABELS) as StudioVodQuality[]).map((key) => (
                      <SelectItem key={key} value={key}>
                        {QUALITY_LABELS[key]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div>
                <FieldLabel>Downloader engine</FieldLabel>
                <Select
                  value={urlDownloader}
                  disabled={startingUrlDownload}
                  onValueChange={(v) => setUrlDownloader((v as StudioVodDownloader) ?? "auto")}
                >
                  <SelectTrigger className="w-full" aria-label="VOD downloader engine">
                    <SelectValue>{DOWNLOADER_LABELS[urlDownloader]}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {(Object.keys(DOWNLOADER_LABELS) as StudioVodDownloader[]).map((key) => (
                      <SelectItem key={key} value={key}>
                        {DOWNLOADER_LABELS[key]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="grid grid-cols-1 gap-(--space-sm) sm:grid-cols-2">
              <div>
                <FieldLabel htmlFor="studio-url-start">Start offset (optional)</FieldLabel>
                <Input
                  id="studio-url-start"
                  placeholder="e.g. 00:30:00 (blank = start)"
                  value={urlStartTime}
                  disabled={startingUrlDownload}
                  onChange={(e) => setUrlStartTime(e.target.value)}
                  className="tabular-nums"
                />
              </div>

              <div>
                <FieldLabel htmlFor="studio-url-end">End offset (optional)</FieldLabel>
                <Input
                  id="studio-url-end"
                  placeholder="e.g. 02:30:00 (blank = end)"
                  value={urlEndTime}
                  disabled={startingUrlDownload}
                  onChange={(e) => setUrlEndTime(e.target.value)}
                  className="tabular-nums"
                />
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-x-(--space-lg) gap-y-(--space-xs)">
              <Checkbox checked={urlFetchChat} onChange={setUrlFetchChat} disabled={startingUrlDownload}>
                Auto-fetch VOD chat replay
              </Checkbox>
              <Checkbox checked={urlAutoAnalyze} onChange={setUrlAutoAnalyze} disabled={startingUrlDownload}>
                Auto-analyze highlights after download
              </Checkbox>
            </div>

            <div className="flex items-center justify-between gap-(--space-sm)">
              <span className="text-xs text-(--color-ink-3)">
                Downloaded VODs stay in local studio storage until you delete the project.
              </span>
              <Button
                type="button"
                disabled={!vodUrl.trim() || startingUrlDownload}
                aria-busy={startingUrlDownload}
                onClick={() => void handleDownloadFromUrl()}
              >
                <Download className="size-3.5" />
                {startingUrlDownload ? "Starting…" : "Download & Open"}
              </Button>
            </div>
          </div>
        )}

        {tab === "chunks" && (
          <div className="flex flex-col gap-(--space-sm)">
            <div className="flex items-center justify-between">
              <span className="text-xs text-(--color-ink-3)">
                Open any existing recording chunk directly in the Studio timeline.
              </span>
              <Button
                type="button"
                variant="outline"
                size="xs"
                disabled={loadingChunks}
                onClick={() => void loadChunks()}
              >
                <RefreshCw className={`size-3 ${loadingChunks ? "animate-spin" : ""}`} />
                Refresh
              </Button>
            </div>

            {chunks === null ? (
              <EmptyState>Loading recent chunks…</EmptyState>
            ) : chunks.length === 0 ? (
              <EmptyState icon={<Film />}>
                No available recorded chunks yet. Upload a VOD or record a live stream.
              </EmptyState>
            ) : (
              <div className="max-h-80 overflow-y-auto rounded-(--radius-inner) border border-(--rule-glass)">
                <Table>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent">
                      <TableHead className="pl-(--space-sm)">Streamer</TableHead>
                      <TableHead>Chunk</TableHead>
                      <TableHead className="text-right">Length</TableHead>
                      <TableHead>Recorded</TableHead>
                      <TableHead className="pr-(--space-sm) text-right">Action</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody className="tabular-nums">
                    {chunks.map((chunk, i) => (
                      <TableRow key={chunk.id} className="rise-row" style={rise(i)}>
                        <TableCell className="pl-(--space-sm) font-medium">
                          {chunk.streamer_name || chunk.streamer_slug || `Streamer ${chunk.streamer_id}`}
                        </TableCell>
                        <TableCell className="text-(--color-ink-2)" title={chunk.file_path}>
                          #{chunk.chunk_index} · {fileBasename(chunk.file_path)}
                        </TableCell>
                        <TableCell className="text-right text-(--color-ink-2)">
                          {formatDuration(chunk.duration_sec)}
                        </TableCell>
                        <TableCell className="text-(--color-ink-3)">
                          {formatDateTime(chunk.created_at)}
                        </TableCell>
                        <TableCell className="pr-(--space-sm) text-right">
                          <Button
                            type="button"
                            variant="outline"
                            size="xs"
                            disabled={openingChunkId === chunk.id}
                            aria-busy={openingChunkId === chunk.id}
                            onClick={() => void handleOpenChunk(chunk)}
                          >
                            <Scissors className="size-3" />
                            {openingChunkId === chunk.id ? "Opening…" : "Open in Studio"}
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </div>
        )}

        {tab === "projects" && (
          <div className="flex flex-col gap-(--space-sm)">
            {projects.length === 0 ? (
              <EmptyState icon={<FolderOpen />}>
                No saved Studio projects yet. Upload a VOD or open a recorded chunk to create one.
              </EmptyState>
            ) : (
              <div className="max-h-80 overflow-y-auto rounded-(--radius-inner) border border-(--rule-glass)">
                <Table>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent">
                      <TableHead className="pl-(--space-sm)">Project</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead className="text-right">Length</TableHead>
                      <TableHead className="text-right">Clips</TableHead>
                      <TableHead className="pr-(--space-sm) text-right">Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody className="tabular-nums">
                    {projects.map((p, i) => {
                      const active = project?.id === p.id;
                      const busy = p.status === "analyzing" || p.status === "exporting";
                      return (
                        <TableRow
                          key={p.id}
                          data-state={active ? "selected" : undefined}
                          className="rise-row"
                          style={rise(i)}
                        >
                          <TableCell className="pl-(--space-sm) max-w-[200px]">
                            <div className="truncate font-medium text-foreground" title={p.title}>
                              {p.title}
                            </div>
                            <div className="text-xs text-(--color-ink-3)">
                              {p.source_type === "chunk" ? "Recorded chunk" : "Uploaded VOD"} ·{" "}
                              {formatDateTime(p.updated_at)}
                            </div>
                          </TableCell>
                          <TableCell>
                            <StatusWord tone={studioStatusTone(p.status)} busy={busy}>
                              {statusLabel(p.status)}
                            </StatusWord>
                          </TableCell>
                          <TableCell className="text-right text-(--color-ink-2)">
                            {formatDuration(p.duration_sec)}
                          </TableCell>
                          <TableCell className="text-right text-(--color-ink-2)">
                            {p.candidates.length}
                          </TableCell>
                          <TableCell className="pr-(--space-sm) text-right">
                            <div className="inline-flex items-center gap-(--space-2xs)">
                              <Button
                                type="button"
                                variant={active ? "default" : "outline"}
                                size="xs"
                                onClick={() => onProjectSelected(p)}
                              >
                                {active ? "Active" : "Open"}
                              </Button>
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon-xs"
                                aria-label={`Delete ${p.title}`}
                                onClick={() => setDeletingTarget(p)}
                              >
                                <Trash2 className="size-3 text-(--color-error)" />
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
          </div>
        )}
      </Panel>

      <Panel
        title="Pipeline settings"
        index={2}
        className="lg:col-span-5"
        action={
          project && (
            <StatusWord tone={studioStatusTone(project.status)} busy={busyStatus}>
              {statusLabel(project.status)}
            </StatusWord>
          )
        }
      >
        <div className="flex flex-col gap-(--space-md)">
          <div>
            <FieldLabel>Streamer preset (optional)</FieldLabel>
            <Select
              value={selectedStreamerId}
              onValueChange={(val) => applyStreamerDefaults(val ?? "none")}
            >
              <SelectTrigger className="w-full" aria-label="Streamer preset">
                <SelectValue>
                  {selectedStreamerId === "none"
                    ? "Custom / none"
                    : streamerById.get(Number(selectedStreamerId))?.name ?? "Select streamer"}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">Custom / none</SelectItem>
                {streamers.map((s) => (
                  <SelectItem key={s.id} value={String(s.id)}>
                    {s.name} ({s.platform})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="grid grid-cols-2 gap-(--space-sm) sm:grid-cols-4">
            <div>
              <FieldLabel htmlFor="studio-clip-count">Clips</FieldLabel>
              <Input
                id="studio-clip-count"
                type="number"
                min={1}
                max={15}
                value={options.clip_count ?? 6}
                onChange={(e) =>
                  onOptionsChange({
                    ...options,
                    clip_count: Math.max(1, Math.min(15, Number(e.target.value) || 1)),
                  })
                }
                className="tabular-nums"
              />
            </div>
            <div>
              <FieldLabel htmlFor="studio-min-len">Min (s)</FieldLabel>
              <Input
                id="studio-min-len"
                type="number"
                min={10}
                max={90}
                value={options.min_len ?? 20}
                onChange={(e) =>
                  onOptionsChange({
                    ...options,
                    min_len: Math.max(10, Math.min(90, Number(e.target.value) || 10)),
                  })
                }
                className="tabular-nums"
              />
            </div>
            <div>
              <FieldLabel htmlFor="studio-max-len">Max (s)</FieldLabel>
              <Input
                id="studio-max-len"
                type="number"
                min={15}
                max={180}
                value={options.max_len ?? 60}
                onChange={(e) =>
                  onOptionsChange({
                    ...options,
                    max_len: Math.max(15, Math.min(180, Number(e.target.value) || 15)),
                  })
                }
                className="tabular-nums"
              />
            </div>
            <div>
              <FieldLabel htmlFor="studio-min-score">VLM floor</FieldLabel>
              <Input
                id="studio-min-score"
                type="number"
                min={0}
                max={9}
                step={0.5}
                title="0 = show all candidates"
                value={options.min_score ?? 0}
                onChange={(e) =>
                  onOptionsChange({
                    ...options,
                    min_score: Math.max(0, Math.min(9, Number(e.target.value) || 0)),
                  })
                }
                className="tabular-nums"
              />
            </div>
          </div>

          <div className="grid grid-cols-1 gap-(--space-sm) sm:grid-cols-2">
            <div>
              <FieldLabel>Moment profile</FieldLabel>
              <Select
                value={options.profile ?? "balanced"}
                onValueChange={(val) =>
                  onOptionsChange({ ...options, profile: (val as StudioProfile) ?? "balanced" })
                }
              >
                <SelectTrigger className="w-full" aria-label="Moment profile">
                  <SelectValue>{PROFILE_LABELS[options.profile ?? "balanced"]}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {(Object.entries(PROFILE_LABELS) as Array<[StudioProfile, string]>).map(
                    ([value, label]) => (
                      <SelectItem key={value} value={value}>
                        {label}
                      </SelectItem>
                    )
                  )}
                </SelectContent>
              </Select>
            </div>

            <div>
              <FieldLabel>Caption style</FieldLabel>
              <Select
                value={options.caption_style ?? "hormozi"}
                onValueChange={(val) =>
                  onOptionsChange({ ...options, caption_style: val ?? "hormozi" })
                }
              >
                <SelectTrigger className="w-full" aria-label="Caption style">
                  <SelectValue>
                    {CAPTION_LABELS[options.caption_style ?? "hormozi"] ?? options.caption_style}
                  </SelectValue>
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
          </div>

          <div>
            <FieldLabel htmlFor="studio-focus-brief">Focus brief (optional)</FieldLabel>
            <Input
              id="studio-focus-brief"
              placeholder="e.g. boss fights, funny squad comms, clutch 1v3s"
              value={options.focus_brief ?? ""}
              onChange={(e) => onOptionsChange({ ...options, focus_brief: e.target.value })}
            />
          </div>

          <div>
            <FieldLabel>Webcam framing</FieldLabel>
            <Select
              value={webcamMode}
              onValueChange={(val) => handleWebcamModeChange((val as WebcamMode) ?? "auto")}
            >
              <SelectTrigger className="w-full" aria-label="Webcam framing">
                <SelectValue>{WEBCAM_MODE_LABELS[webcamMode]}</SelectValue>
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

          {webcamMode === "custom" && (
            project ? (
              <StudioPreviewCamPicker
                project={project}
                value={isCamBox(options.cam_box) ? options.cam_box : DEFAULT_CAM_BOX}
                onChange={(box) => onOptionsChange({ ...options, cam_auto: false, cam_box: box, layout: "stack" })}
              />
            ) : activeStreamer ? (
              <WebcamFramePicker
                streamer={activeStreamer}
                value={isCamBox(options.cam_box) ? options.cam_box : DEFAULT_CAM_BOX}
                onChange={(box) => onOptionsChange({ ...options, cam_auto: false, cam_box: box, layout: "stack" })}
              />
            ) : (
              <p className="text-xs text-(--color-ink-3)">
                Load a VOD project or pick a streamer preset to position the custom webcam frame.
              </p>
            )
          )}

          <div className="flex flex-wrap items-center justify-between gap-(--space-xs) pt-(--space-2xs)">
            <span className="text-xs text-(--color-ink-3)">VLM floor 0 = show all candidates</span>
            <div className="flex flex-wrap items-center gap-(--space-xs)">
              {project && onAddManualClip && (
                <Button
                  type="button"
                  variant="outline"
                  disabled={busyStatus}
                  onClick={onAddManualClip}
                >
                  <Plus className="size-3.5" />
                  Add manual clip
                </Button>
              )}
              <Button
                type="button"
                disabled={!project || analyzing || busyStatus}
                aria-busy={analyzing || project?.status === "analyzing"}
                onClick={() => void onAnalyze(options)}
              >
                <Sparkles className="size-3.5" />
                {analyzing || project?.status === "analyzing" ? "Analyzing VOD…" : "Analyze VOD"}
              </Button>
            </div>
          </div>
        </div>
      </Panel>

      <Dialog open={deletingTarget !== null} onOpenChange={(open) => !open && setDeletingTarget(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete Studio project?</DialogTitle>
            <DialogDescription>
              {deletingTarget?.source_type === "upload"
                ? `Deleting "${deletingTarget?.title}" removes the project, timeline regions, and its uploaded VOD file from studio storage.`
                : `Deleting "${deletingTarget?.title}" removes this Studio project and its timeline regions. The underlying recorded chunk is kept.`}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setDeletingTarget(null)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={deleting}
              aria-busy={deleting}
              onClick={() => void handleConfirmDelete()}
            >
              {deleting ? "Deleting…" : "Delete project"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
