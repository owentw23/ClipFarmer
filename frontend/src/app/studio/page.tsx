"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  Check,
  ExternalLink,
  FolderOpen,
  Pause,
  Play,
  Plus,
  RectangleVertical,
  Repeat,
  RotateCcw,
  RotateCw,
  Scissors,
  Trash2,
  UploadCloud,
} from "lucide-react";
import { toast } from "sonner";

import {
  EmptyState,
  Notice,
  PageHeader,
  Panel,
  StatusWord,
  Switch,
  rise,
} from "@/components/page-kit";
import { ClipInspector } from "@/components/studio/ClipInspector";
import { VodTimelineEditor } from "@/components/studio/VodTimelineEditor";
import { VodUploadPanel, studioStatusTone } from "@/components/studio/VodUploadPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  Streamer,
  StudioCandidate,
  StudioOptions,
  StudioProject,
  analyzeStudioProject,
  createStudioProject,
  exportStudioProject,
  getStreamers,
  getStudioProject,
  getStudioProjects,
  getStudioVideoUrl,
  saveStudioCandidates,
} from "@/lib/api";

const DEFAULT_OPTIONS: StudioOptions = {
  clip_count: 6,
  min_len: 20,
  max_len: 60,
  min_score: 0,
  profile: "balanced",
  focus_brief: "",
  cam_auto: true,
  cam_box: null,
  layout: "stack",
  fallback_layout: "blur",
  caption_style: "hormozi",
};

function formatTimecode(sec: number, withTenths = false): string {
  const safe = Math.max(0, Number.isFinite(sec) ? sec : 0);
  const hrs = Math.floor(safe / 3600);
  const mins = Math.floor((safe % 3600) / 60);
  const rem = safe % 60;
  const secStr = withTenths ? rem.toFixed(1).padStart(4, "0") : String(Math.floor(rem)).padStart(2, "0");
  if (hrs > 0) {
    return `${hrs}:${String(mins).padStart(2, "0")}:${secStr}`;
  }
  return `${String(mins).padStart(2, "0")}:${secStr}`;
}

function StudioPageInner() {
  const searchParams = useSearchParams();
  const chunkIdParam = searchParams.get("chunk_id");
  const projectIdParam = searchParams.get("project_id");

  const [projects, setProjects] = useState<StudioProject[]>([]);
  const [streamers, setStreamers] = useState<Streamer[]>([]);
  const [project, setProject] = useState<StudioProject | null>(null);
  const [candidates, setCandidates] = useState<StudioCandidate[]>([]);
  const [options, setOptions] = useState<StudioOptions>(DEFAULT_OPTIONS);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showSourcePanel, setShowSourcePanel] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Video transport state
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [videoDuration, setVideoDuration] = useState(0);
  const [playbackRate, setPlaybackRate] = useState<1 | 1.5 | 2>(1);
  const [loopSelected, setLoopSelected] = useState(false);
  const [verticalPreview, setVerticalPreview] = useState(false);

  // Save / action state
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [analyzing, setAnalyzing] = useState(false);
  const [exporting, setExporting] = useState(false);

  const dirtyRef = useRef(false);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const handledChunkRef = useRef<string | null>(null);

  const applyProject = useCallback((p: StudioProject, collapseDrawer = true) => {
    dirtyRef.current = false;
    if (saveTimerRef.current) {
      clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    setProject(p);
    setCandidates(p.candidates ?? []);
    setOptions({ ...DEFAULT_OPTIONS, ...(p.options ?? {}) });
    setSelectedId((prev) => {
      if (prev && (p.candidates ?? []).some((c) => c.id === prev)) return prev;
      return p.candidates?.[0]?.id ?? null;
    });
    setVideoDuration(p.duration_sec ?? 0);
    setCurrentTime(0);
    setPlaying(false);
    setSaveState("idle");
    if (collapseDrawer) {
      setShowSourcePanel(false);
    }
  }, []);

  const refreshProjects = useCallback(async () => {
    const res = await getStudioProjects();
    setProjects(res.items ?? []);
  }, []);

  // Initial load & ?chunk_id= / ?project_id= auto-open
  useEffect(() => {
    let alive = true;
    async function init() {
      try {
        const [projectsRes, streamersRes] = await Promise.all([
          getStudioProjects(),
          getStreamers(),
        ]);
        if (!alive) return;
        const list = projectsRes.items ?? [];
        setProjects(list);
        setStreamers(streamersRes.items ?? []);
        setError(null);

        if (chunkIdParam && handledChunkRef.current !== chunkIdParam) {
          handledChunkRef.current = chunkIdParam;
          const numericChunkId = Number(chunkIdParam);
          if (Number.isFinite(numericChunkId) && numericChunkId > 0) {
            const existing = list.find((p) => p.chunk_id === numericChunkId);
            if (existing) {
              const full = await getStudioProject(existing.id);
              if (!alive) return;
              applyProject(full, true);
              return;
            }
            const created = await createStudioProject({
              source_type: "chunk",
              chunk_id: numericChunkId,
            });
            if (!alive) return;
            setProjects((prev) => [created, ...prev]);
            applyProject(created, true);
            toast.success("Opened recorded chunk in VOD Studio");
            return;
          }
        }

        if (projectIdParam) {
          const numericPid = Number(projectIdParam);
          const match = list.find((p) => p.id === numericPid);
          if (match) {
            const full = await getStudioProject(match.id);
            if (!alive) return;
            applyProject(full, true);
            return;
          }
        }

        if (list.length > 0 && !project) {
          // Keep source panel collapsed if user wants to pick, or auto-open the most recent active project
          const latest = await getStudioProject(list[0].id);
          if (!alive) return;
          applyProject(latest, false);
        }
      } catch (err) {
        if (alive) {
          setError(err instanceof Error ? err.message : "Failed to load VOD Studio");
        }
      } finally {
        if (alive) setLoading(false);
      }
    }
    void init();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chunkIdParam, projectIdParam]);

  // Poll every 2s while downloading, analyzing, or exporting
  useEffect(() => {
    if (!project) return;
    if (
      project.status !== "downloading" &&
      project.status !== "analyzing" &&
      project.status !== "exporting"
    ) {
      return;
    }
    const pid = project.id;
    const prevStatus = project.status;
    const timer = setInterval(() => {
      void getStudioProject(pid)
        .then((fresh) => {
          setProject(fresh);
          setProjects((prev) => prev.map((p) => (p.id === fresh.id ? fresh : p)));
          if (
            fresh.status !== "downloading" &&
            fresh.status !== "analyzing" &&
            fresh.status !== "exporting"
          ) {
            if (!dirtyRef.current) {
              setCandidates(fresh.candidates ?? []);
              setOptions({ ...DEFAULT_OPTIONS, ...(fresh.options ?? {}) });
              setSelectedId((prev) =>
                prev && (fresh.candidates ?? []).some((c) => c.id === prev)
                  ? prev
                  : fresh.candidates?.[0]?.id ?? null
              );
            }
            if (fresh.duration_sec && fresh.duration_sec > 0) {
              setVideoDuration(fresh.duration_sec);
            }
            if (fresh.status === "ready" && prevStatus === "downloading") {
              toast.success(
                `VOD downloaded${fresh.has_chat ? " with chat replay" : ""} — ready to analyze or cut`
              );
            } else if (fresh.status === "analyzed") {
              toast.success(`Scout finished — found ${fresh.candidates.length} clip regions`);
            } else if (fresh.status === "exported") {
              const count = fresh.options?.last_exported_clip_ids?.length ?? 0;
              toast.success(`Exported ${count} clip${count === 1 ? "" : "s"} to Clips`);
            } else if (fresh.status === "error") {
              toast.error(fresh.last_error || "Studio job failed");
            }
          }
        })
        .catch(() => {
          // Keep polling quietly on transient network hiccup
        });
    }, 2000);
    return () => clearInterval(timer);
  }, [project]);

  // Auto-debounced save of candidates + options (600ms)
  const flushSave = useCallback(
    async (pid: number, nextCandidates: StudioCandidate[], nextOptions: StudioOptions) => {
      if (saveTimerRef.current) {
        clearTimeout(saveTimerRef.current);
        saveTimerRef.current = null;
      }
      dirtyRef.current = false;
      setSaveState("saving");
      try {
        const updated = await saveStudioCandidates(pid, nextCandidates, nextOptions);
        setProject((prev) =>
          prev && prev.id === updated.id
            ? { ...prev, candidates: updated.candidates, options: updated.options, updated_at: updated.updated_at }
            : prev
        );
        setProjects((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
        setSaveState("saved");
      } catch (err) {
        dirtyRef.current = true;
        setSaveState("error");
        toast.error(err instanceof Error ? err.message : "Failed to save timeline changes");
      }
    },
    []
  );

  const scheduleSave = useCallback(
    (nextCandidates: StudioCandidate[], nextOptions: StudioOptions) => {
      if (!project) return;
      const pid = project.id;
      dirtyRef.current = true;
      setSaveState("saving");
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
      saveTimerRef.current = setTimeout(() => {
        void flushSave(pid, nextCandidates, nextOptions);
      }, 600);
    },
    [project, flushSave]
  );

  useEffect(() => {
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    };
  }, []);

  const effectiveDuration = useMemo(() => {
    if (videoDuration > 0) return videoDuration;
    if (project?.duration_sec && project.duration_sec > 0) return project.duration_sec;
    if (project?.timeline_curve?.duration && project.timeline_curve.duration > 0) {
      return project.timeline_curve.duration;
    }
    return 0;
  }, [videoDuration, project]);

  const selectedIndex = useMemo(
    () => candidates.findIndex((c) => c.id === selectedId),
    [candidates, selectedId]
  );
  const selectedCandidate = selectedIndex >= 0 ? candidates[selectedIndex] : null;

  const selectedForExportCount = useMemo(
    () => candidates.filter((c) => c.selected !== false).length,
    [candidates]
  );

  const seekTo = useCallback(
    (sec: number) => {
      const clamped =
        effectiveDuration > 0 ? Math.max(0, Math.min(effectiveDuration, sec)) : Math.max(0, sec);
      setCurrentTime(clamped);
      if (videoRef.current) {
        videoRef.current.currentTime = clamped;
      }
    },
    [effectiveDuration]
  );

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) {
      void v.play().catch(() => {});
    } else {
      v.pause();
    }
  }, []);

  const handleRateChange = (rate: 1 | 1.5 | 2) => {
    setPlaybackRate(rate);
    if (videoRef.current) {
      videoRef.current.playbackRate = rate;
    }
  };

  const handleCandidatesChange = useCallback(
    (next: StudioCandidate[]) => {
      setCandidates(next);
      scheduleSave(next, options);
    },
    [options, scheduleSave]
  );

  const handleOptionsChange = useCallback(
    (nextOpts: StudioOptions) => {
      setOptions(nextOpts);
      if (project) {
        scheduleSave(candidates, nextOpts);
      }
    },
    [project, candidates, scheduleSave]
  );

  const handleUpdateSelectedCandidate = useCallback(
    (updated: StudioCandidate) => {
      const next = candidates.map((c) => (c.id === updated.id ? updated : c));
      handleCandidatesChange(next);
    },
    [candidates, handleCandidatesChange]
  );

  const handleAddClipAtPlayhead = useCallback(() => {
    if (!project) return;
    const dur = effectiveDuration > 0 ? effectiveDuration : Math.max(60, currentTime + 30);
    const start = Math.max(0, Math.round((currentTime - 15) * 10) / 10);
    const end = Math.min(dur, Math.max(start + 10, Math.round((start + 30) * 10) / 10));
    const newId = `manual-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
    const newCand: StudioCandidate = {
      id: newId,
      start,
      end,
      peak_sec: Math.round(((start + end) / 2) * 10) / 10,
      duration: Math.round((end - start) * 10) / 10,
      title: `Clip #${candidates.length + 1}`,
      hook: "",
      category: "Manual",
      selected: true,
    };
    const next = [...candidates, newCand].sort((a, b) => a.start - b.start);
    setSelectedId(newId);
    handleCandidatesChange(next);
    toast.success("Added 30s manual clip region");
  }, [project, effectiveDuration, currentTime, candidates, handleCandidatesChange]);

  const handleDeleteCandidate = useCallback(
    (id: string) => {
      const idx = candidates.findIndex((c) => c.id === id);
      if (idx < 0) return;
      const removed = candidates[idx];
      const next = candidates.filter((c) => c.id !== id);
      handleCandidatesChange(next);
      if (selectedId === id) {
        setSelectedId(next[Math.min(idx, next.length - 1)]?.id ?? null);
      }
      toast(`Removed "${removed.title || `Clip #${idx + 1}`}"`, {
        action: {
          label: "Undo",
          onClick: () => {
            setCandidates((curr) => {
              const restored = [...curr];
              restored.splice(Math.min(idx, restored.length), 0, removed);
              scheduleSave(restored, options);
              return restored;
            });
            setSelectedId(removed.id);
          },
        },
      });
    },
    [candidates, selectedId, handleCandidatesChange, scheduleSave, options]
  );

  const handlePlaySelectedClip = useCallback(() => {
    if (!selectedCandidate) return;
    if (loopSelected && playing) {
      setLoopSelected(false);
      videoRef.current?.pause();
      return;
    }
    setLoopSelected(true);
    seekTo(selectedCandidate.start);
    void videoRef.current?.play().catch(() => {});
  }, [selectedCandidate, loopSelected, playing, seekTo]);

  // Global keyboard shortcuts
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!project) return;
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      if (e.code === "Space") {
        e.preventDefault();
        togglePlay();
      } else if (e.key === "j" || e.key === "J") {
        e.preventDefault();
        seekTo(currentTime - 5);
      } else if (e.key === "l" || e.key === "L") {
        e.preventDefault();
        seekTo(currentTime + 5);
      } else if (e.key === "[" && selectedCandidate) {
        e.preventDefault();
        const nextStart = Math.max(0, Math.min(selectedCandidate.end - 3, Math.round(currentTime * 10) / 10));
        handleUpdateSelectedCandidate({
          ...selectedCandidate,
          start: nextStart,
          duration: Math.round((selectedCandidate.end - nextStart) * 10) / 10,
        });
      } else if (e.key === "]" && selectedCandidate) {
        e.preventDefault();
        const maxD = effectiveDuration > 0 ? effectiveDuration : selectedCandidate.end + 300;
        const nextEnd = Math.min(maxD, Math.max(selectedCandidate.start + 3, Math.round(currentTime * 10) / 10));
        handleUpdateSelectedCandidate({
          ...selectedCandidate,
          end: nextEnd,
          duration: Math.round((nextEnd - selectedCandidate.start) * 10) / 10,
        });
      } else if ((e.key === "Delete" || e.key === "Backspace") && selectedCandidate) {
        e.preventDefault();
        handleDeleteCandidate(selectedCandidate.id);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    project,
    togglePlay,
    seekTo,
    currentTime,
    selectedCandidate,
    effectiveDuration,
    handleUpdateSelectedCandidate,
    handleDeleteCandidate,
  ]);

  const handleAnalyze = async (optsToUse: StudioOptions) => {
    if (!project) return;
    setAnalyzing(true);
    try {
      if (dirtyRef.current) {
        await flushSave(project.id, candidates, optsToUse);
      }
      const updated = await analyzeStudioProject(project.id, optsToUse);
      setProject(updated);
      setProjects((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
      setShowSourcePanel(false);
      toast("Started AI VOD analysis");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to start analysis");
    } finally {
      setAnalyzing(false);
    }
  };

  const handleExportSelected = async () => {
    if (!project) return;
    const selectedIds = candidates.filter((c) => c.selected !== false).map((c) => c.id);
    if (selectedIds.length === 0) {
      toast.error("Select at least one clip region to export.");
      return;
    }
    setExporting(true);
    try {
      await flushSave(project.id, candidates, options);
      const updated = await exportStudioProject(project.id, {
        candidate_ids: selectedIds,
        options,
      });
      setProject(updated);
      setProjects((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
      toast(`Exporting ${selectedIds.length} clip${selectedIds.length === 1 ? "" : "s"}…`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Export failed to start");
    } finally {
      setExporting(false);
    }
  };

  const isBusy =
    project?.status === "downloading" ||
    project?.status === "analyzing" ||
    project?.status === "exporting";
  const progressPct = (() => {
    const raw = project?.progress ?? 0;
    if (!Number.isFinite(raw) || raw <= 0) return 0;
    return Math.min(100, Math.round(raw <= 1 ? raw * 100 : raw));
  })();
  const exportedIds = project?.options?.last_exported_clip_ids ?? [];
  const camBox = options.cam_box;

  return (
    <div className="flex flex-col gap-(--space-lg)">
      <PageHeader
        title="VOD Studio"
        meta={
          project
            ? `${project.title} · ${effectiveDuration > 0 ? formatTimecode(effectiveDuration) : "--"} · ${candidates.length} region${candidates.length === 1 ? "" : "s"} · ${selectedForExportCount} selected`
            : "Upload a VOD or pick a recorded chunk to scout and cut Shorts"
        }
        actions={
          <>
            {project && saveState !== "idle" && (
              <span className="glass inline-flex h-8 items-center gap-1.5 rounded-full px-3 text-xs text-(--color-ink-2)">
                {saveState === "saving" && "Saving…"}
                {saveState === "saved" && (
                  <>
                    <Check className="size-3 text-(--color-signal)" /> Saved
                  </>
                )}
                {saveState === "error" && (
                  <span className="text-(--color-error)">Save failed</span>
                )}
              </span>
            )}
            <Button
              type="button"
              variant="outline"
              className="glass"
              onClick={() => setShowSourcePanel((v) => !v)}
            >
              {showSourcePanel && project ? (
                <>
                  <FolderOpen className="size-3.5" /> Hide sources
                </>
              ) : (
                <>
                  <UploadCloud className="size-3.5" /> New / Switch VOD
                </>
              )}
            </Button>
            {project && (
              <Button
                type="button"
                disabled={selectedForExportCount === 0 || exporting || isBusy}
                aria-busy={exporting || project.status === "exporting"}
                onClick={() => void handleExportSelected()}
              >
                <Scissors className="size-3.5" />
                {project.status === "exporting"
                  ? "Exporting…"
                  : `Export Selected (${selectedForExportCount})`}
              </Button>
            )}
          </>
        }
      />

      {error && <Notice tone="error">{error}</Notice>}

      {loading && !project ? (
        <div className="grid grid-cols-1 gap-(--space-lg) lg:grid-cols-12">
          <Skeleton className="h-80 rounded-(--radius-card) lg:col-span-7" />
          <Skeleton className="h-80 rounded-(--radius-card) lg:col-span-5" />
        </div>
      ) : (
        <>
          {(showSourcePanel || !project) && (
            <VodUploadPanel
              project={project}
              projects={projects}
              streamers={streamers}
              options={options}
              onOptionsChange={handleOptionsChange}
              onProjectSelected={(p) => {
                void getStudioProject(p.id)
                  .then((full) => applyProject(full, true))
                  .catch(() => applyProject(p, true));
              }}
              onProjectDeleted={(id) => {
                setProjects((prev) => prev.filter((p) => p.id !== id));
                if (project?.id === id) {
                  setProject(null);
                  setCandidates([]);
                  setSelectedId(null);
                  setShowSourcePanel(true);
                }
              }}
              onProjectsRefresh={refreshProjects}
              onAnalyze={handleAnalyze}
              onAddManualClip={handleAddClipAtPlayhead}
              analyzing={analyzing}
            />
          )}

          {project && isBusy && (
            <Panel
              title={
                project.status === "downloading"
                  ? "Downloading VOD"
                  : project.status === "analyzing"
                    ? "Analyzing VOD"
                    : "Exporting Shorts"
              }
              index={1}
              action={
                <StatusWord tone="owed" busy>
                  {progressPct}%
                </StatusWord>
              }
            >
              <div className="flex flex-col gap-(--space-xs)">
                <div className="flex items-center justify-between text-sm text-(--color-ink-2)">
                  <span>{project.stage || "Working…"}</span>
                  <span className="tabular-nums">{progressPct}%</span>
                </div>
                <div className="h-2 w-full overflow-hidden rounded-full bg-(--rule-glass)">
                  <div
                    className="h-full rounded-full bg-(--color-signal) transition-[width] duration-300"
                    style={{ width: `${Math.max(2, progressPct)}%` }}
                  />
                </div>
              </div>
            </Panel>
          )}

          {project?.status === "error" && project.last_error && (
            <Notice tone="error">{project.last_error}</Notice>
          )}

          {project?.status === "exported" && exportedIds.length > 0 && (
            <Notice
              tone="success"
              action={
                <Button render={<Link href="/clips" />} variant="outline" size="sm">
                  <ExternalLink className="size-3.5" />
                  View in Clips
                </Button>
              }
            >
              Exported {exportedIds.length} Short{exportedIds.length === 1 ? "" : "s"} to your Clips queue.
            </Notice>
          )}

          {project && (
            <>
              <div className="grid grid-cols-1 gap-(--space-lg) lg:grid-cols-12">
                <Panel
                  title="Preview"
                  index={1}
                  className="lg:col-span-7"
                  action={
                    <div className="flex items-center gap-(--space-xs)">
                      <Button
                        type="button"
                        variant={verticalPreview ? "default" : "outline"}
                        size="xs"
                        aria-pressed={verticalPreview}
                        onClick={() => setVerticalPreview((v) => !v)}
                      >
                        <RectangleVertical className="size-3" />
                        9:16 crop
                      </Button>
                      <StatusWord tone={studioStatusTone(project.status)} busy={isBusy}>
                        {project.status}
                      </StatusWord>
                    </div>
                  }
                >
                  <div className="flex flex-col gap-(--space-sm)">
                    <div className="relative aspect-video w-full overflow-hidden rounded-(--radius-inner) border border-(--rule-glass) bg-black">
                      {project.status === "downloading" ? (
                        <div className="flex h-full w-full flex-col items-center justify-center gap-(--space-xs) px-(--space-md) text-center text-sm text-(--color-ink-3)">
                          <StatusWord tone="owed" busy>
                            {project.stage || "Downloading VOD…"}
                          </StatusWord>
                          <span>Video player will load automatically once download finishes.</span>
                        </div>
                      ) : (
                      <video
                        ref={videoRef}
                        key={`${project.id}-ready`}
                        src={getStudioVideoUrl(project.id)}
                        playsInline
                        preload="metadata"
                        onClick={togglePlay}
                        onLoadedMetadata={(e) => {
                          const d = e.currentTarget.duration;
                          if (Number.isFinite(d) && d > 0) setVideoDuration(d);
                          e.currentTarget.playbackRate = playbackRate;
                        }}
                        onPlay={() => setPlaying(true)}
                        onPause={() => setPlaying(false)}
                        onEnded={() => setPlaying(false)}
                        onTimeUpdate={(e) => {
                          const t = e.currentTarget.currentTime;
                          setCurrentTime(t);
                          if (
                            loopSelected &&
                            selectedCandidate &&
                            !e.currentTarget.paused &&
                            t >= selectedCandidate.end
                          ) {
                            e.currentTarget.currentTime = selectedCandidate.start;
                          }
                        }}
                        className="h-full w-full cursor-pointer object-contain"
                      />
                      )}

                      {verticalPreview && (
                        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
                          {/* Center 9:16 frame out of 16:9 (9/16 / 16/9 = 31.64% width) */}
                          <div
                            className="relative h-full border-x-2 border-(--color-signal) shadow-[0_0_0_9999px_rgba(8,10,18,0.58)]"
                            style={{ width: "31.64%" }}
                          >
                            <span className="absolute top-2 left-1/2 -translate-x-1/2 rounded-full bg-(--color-paper)/85 px-2 py-0.5 text-[10px] font-medium text-(--color-signal)">
                              9:16 Shorts
                            </span>
                          </div>
                          {camBox && options.layout !== "blur" && (
                            <div
                              style={{
                                left: `${camBox.x * 100}%`,
                                top: `${camBox.y * 100}%`,
                                width: `${camBox.w * 100}%`,
                                height: `${camBox.h * 100}%`,
                              }}
                              className="absolute border border-(--color-owed) bg-(--color-owed)/10"
                            >
                              <span className="absolute -top-4 left-0 rounded bg-(--color-paper)/85 px-1 text-[9px] text-(--color-owed)">
                                Cam
                              </span>
                            </div>
                          )}
                        </div>
                      )}
                    </div>

                    {/* Custom Dusk transport bar */}
                    <div className="flex flex-wrap items-center justify-between gap-(--space-xs) rounded-(--radius-inner) border border-(--rule-glass) bg-(--color-paper-2)/65 px-(--space-sm) py-(--space-xs)">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Button
                          type="button"
                          variant="default"
                          size="sm"
                          onClick={togglePlay}
                        >
                          {playing ? <Pause className="size-3.5" /> : <Play className="size-3.5" />}
                          {playing ? "Pause" : "Play"}
                        </Button>
                        <Button
                          type="button"
                          variant="outline"
                          size="xs"
                          title="Back 5 seconds (J)"
                          onClick={() => seekTo(currentTime - 5)}
                        >
                          <RotateCcw className="size-3" />
                          -5s
                        </Button>
                        <Button
                          type="button"
                          variant="outline"
                          size="xs"
                          title="Forward 5 seconds (L)"
                          onClick={() => seekTo(currentTime + 5)}
                        >
                          <RotateCw className="size-3" />
                          +5s
                        </Button>
                        <Button
                          type="button"
                          variant={loopSelected ? "default" : "outline"}
                          size="xs"
                          disabled={!selectedCandidate}
                          aria-pressed={loopSelected}
                          onClick={() => setLoopSelected((v) => !v)}
                        >
                          <Repeat className="size-3" />
                          Loop clip
                        </Button>
                      </div>

                      <div className="flex items-center gap-(--space-sm)">
                        <div className="inline-flex items-center rounded-full border border-(--rule-glass) bg-(--row-hover) p-0.5">
                          {([1, 1.5, 2] as const).map((r) => (
                            <button
                              key={r}
                              type="button"
                              onClick={() => handleRateChange(r)}
                              className={`rounded-full px-2 py-0.5 text-xs font-medium tabular-nums transition-colors ${
                                playbackRate === r
                                  ? "bg-(--color-signal) text-(--color-signal-ink)"
                                  : "text-(--color-ink-3) hover:text-foreground"
                              }`}
                            >
                              {r}×
                            </button>
                          ))}
                        </div>
                        <div className="text-xs font-medium text-foreground tabular-nums">
                          {formatTimecode(currentTime, true)}{" "}
                          <span className="text-(--color-ink-3)">
                            / {formatTimecode(effectiveDuration)}
                          </span>
                        </div>
                      </div>
                    </div>
                  </div>
                </Panel>

                <div className="lg:col-span-5">
                  <ClipInspector
                    candidate={selectedCandidate}
                    candidateIndex={selectedIndex}
                    duration={effectiveDuration}
                    currentTime={currentTime}
                    looping={loopSelected}
                    onChange={handleUpdateSelectedCandidate}
                    onSeek={seekTo}
                    onPlayClip={handlePlaySelectedClip}
                    onDelete={handleDeleteCandidate}
                  />
                </div>
              </div>

              <VodTimelineEditor
                duration={effectiveDuration}
                currentTime={currentTime}
                candidates={candidates}
                selectedId={selectedId}
                timelineCurve={project.timeline_curve}
                onSeek={seekTo}
                onSelect={(id) => setSelectedId(id)}
                onChangeCandidates={handleCandidatesChange}
                onAddClipAtPlayhead={handleAddClipAtPlayhead}
                disabled={isBusy}
              />

              <Panel
                title="Clip regions"
                count={candidates.length}
                flush
                index={4}
                action={
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={isBusy}
                    onClick={handleAddClipAtPlayhead}
                  >
                    <Plus className="size-3.5" />
                    Add manual clip
                  </Button>
                }
              >
                {candidates.length === 0 ? (
                  <EmptyState icon={<Scissors />}>
                    No clip regions yet. Run Analyze VOD from New / Switch VOD, or click Add manual
                    clip to mark a highlight yourself.
                  </EmptyState>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow className="hover:bg-transparent">
                        <TableHead className="pl-(--space-lg)">#</TableHead>
                        <TableHead>Title & Hook</TableHead>
                        <TableHead>Range</TableHead>
                        <TableHead className="text-right">Length</TableHead>
                        <TableHead className="text-right">Score</TableHead>
                        <TableHead>Category</TableHead>
                        <TableHead>Export</TableHead>
                        <TableHead className="pr-(--space-lg) text-right">Action</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody className="tabular-nums">
                      {candidates.map((cand, i) => {
                        const active = cand.id === selectedId;
                        const clipLen = Math.max(0, cand.end - cand.start);
                        return (
                          <TableRow
                            key={cand.id}
                            data-state={active ? "selected" : undefined}
                            onClick={() => {
                              setSelectedId(cand.id);
                              seekTo(cand.start);
                            }}
                            className="rise-row cursor-pointer"
                            style={rise(i)}
                          >
                            <TableCell className="pl-(--space-lg) font-medium text-(--color-ink-3)">
                              #{i + 1}
                            </TableCell>
                            <TableCell className="max-w-[320px]">
                              <div className="truncate font-medium text-foreground">
                                {cand.title || "Untitled region"}
                              </div>
                              {cand.hook && (
                                <div className="truncate text-xs text-(--color-ink-3)">
                                  {cand.hook}
                                </div>
                              )}
                            </TableCell>
                            <TableCell className="text-(--color-ink-2)">
                              {formatTimecode(cand.start)} – {formatTimecode(cand.end)}
                            </TableCell>
                            <TableCell className="text-right text-(--color-ink-2)">
                              {clipLen.toFixed(1)}s
                            </TableCell>
                            <TableCell className="text-right">
                              {typeof cand.score === "number" ? (
                                <span className="font-medium text-(--color-signal)">
                                  {cand.score.toFixed(1)}
                                </span>
                              ) : (
                                <span className="text-(--color-ink-3)">--</span>
                              )}
                            </TableCell>
                            <TableCell>
                              {cand.category ? (
                                <Badge variant="secondary">{cand.category}</Badge>
                              ) : (
                                <span className="text-(--color-ink-3)">--</span>
                              )}
                            </TableCell>
                            <TableCell onClick={(e) => e.stopPropagation()}>
                              <Switch
                                checked={cand.selected !== false}
                                label={`Include clip ${i + 1} in export`}
                                onChange={() =>
                                  handleCandidatesChange(
                                    candidates.map((c) =>
                                      c.id === cand.id
                                        ? { ...c, selected: !(c.selected !== false) }
                                        : c
                                    )
                                  )
                                }
                              />
                            </TableCell>
                            <TableCell
                              className="pr-(--space-lg) text-right"
                              onClick={(e) => e.stopPropagation()}
                            >
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon-xs"
                                aria-label={`Delete clip ${i + 1}`}
                                onClick={() => handleDeleteCandidate(cand.id)}
                              >
                                <Trash2 className="size-3.5 text-(--color-error)" />
                              </Button>
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                )}
              </Panel>
            </>
          )}
        </>
      )}
    </div>
  );
}

export default function StudioPage() {
  return (
    <Suspense
      fallback={
        <div className="flex flex-col gap-(--space-lg)">
          <PageHeader title="VOD Studio" meta="Loading Studio…" />
          <Skeleton className="h-80 rounded-(--radius-card)" />
        </div>
      }
    >
      <StudioPageInner />
    </Suspense>
  );
}
