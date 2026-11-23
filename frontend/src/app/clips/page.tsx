"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CalendarClock,
  CalendarX2,
  ChevronDown,
  ExternalLink,
  Film,
  HardDriveUpload,
  MoreHorizontal,
  Pencil,
  Play,
  RefreshCw,
  Send,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import {
  Checkbox,
  EmptyState,
  FieldLabel,
  Notice,
  PageHeader,
  Panel,
  StatusWord,
  formatDateTime,
  rise,
  statusLabel,
  toneForStatus,
} from "@/components/page-kit";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  ClipDraft,
  ClipPublication,
  Streamer,
  TargetPrivacy,
  approveClip,
  bulkApproveClips,
  bulkDeleteClips,
  bulkRejectClips,
  bulkScheduleClips,
  cancelClipSchedule,
  deleteClip,
  getClipPublications,
  getClipThumbnailUrl,
  getClipVideoUrl,
  getClips,
  getStreamers,
  rejectClip,
  scheduleClip,
  updateClip,
  uploadClipToDrive,
} from "@/lib/api";

const STATUS_OPTIONS = [
  { value: "all", label: "Every status" },
  { value: "draft", label: "Draft" },
  { value: "notified", label: "Notified" },
  { value: "scheduled", label: "Scheduled" },
  { value: "approved", label: "Approved" },
  { value: "kept_local", label: "Kept local" },
  { value: "posted", label: "Posted" },
  { value: "failed", label: "Failed" },
  { value: "archived", label: "Archived" },
  { value: "deleted", label: "Deleted" },
];

const SCHEDULE_INTERVAL_OPTIONS = [
  { value: "0", label: "Post all at the same time" },
  { value: "60", label: "Space 1 hour apart" },
  { value: "180", label: "Space 3 hours apart" },
  { value: "360", label: "Space 6 hours apart" },
  { value: "720", label: "Space 12 hours apart" },
  { value: "1440", label: "Space 1 day apart" },
];

function toLocalDateInputValue(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function toLocalTimeInputValue(d: Date): string {
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function defaultScheduleDate(): Date {
  const next = new Date();
  next.setMinutes(0, 0, 0);
  next.setHours(next.getHours() + 1);
  return next;
}

const PRIVACY_OPTIONS: Array<{ value: "default" | TargetPrivacy; label: string }> = [
  { value: "default", label: "Channel default" },
  { value: "public", label: "Public" },
  { value: "unlisted", label: "Unlisted" },
  { value: "private", label: "Private" },
];

export default function ClipsPage() {
  const [clips, setClips] = useState<ClipDraft[]>([]);
  const [streamers, setStreamers] = useState<Streamer[]>([]);
  const [publicationsMap, setPublicationsMap] = useState<Record<number, ClipPublication[]>>({});
  const [privacyMap, setPrivacyMap] = useState<Record<number, "default" | TargetPrivacy>>({});
  const [statusFilter, setStatusFilter] = useState("all");
  const [streamerFilter, setStreamerFilter] = useState("all");
  const [uploadingId, setUploadingId] = useState<number | null>(null);
  const [publishingId, setPublishingId] = useState<number | null>(null);
  const [rejectingId, setRejectingId] = useState<number | null>(null);
  const [previewClip, setPreviewClip] = useState<ClipDraft | null>(null);
  const [editingClip, setEditingClip] = useState<ClipDraft | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [savingEdit, setSavingEdit] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [deletingClip, setDeletingClip] = useState<ClipDraft | null>(null);
  const [deleteDrive, setDeleteDrive] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(() => new Set());
  const [lastSelectedIndex, setLastSelectedIndex] = useState<number | null>(null);
  const [bulkPrivacy, setBulkPrivacy] = useState<"default" | TargetPrivacy>("default");
  const [bulkAction, setBulkAction] = useState<"post" | "reject" | "delete" | "schedule" | null>(null);
  const [confirmBulkPostOpen, setConfirmBulkPostOpen] = useState(false);
  const [confirmBulkDeleteOpen, setConfirmBulkDeleteOpen] = useState(false);
  const [bulkDeleteDrive, setBulkDeleteDrive] = useState(false);
  const [scheduleTarget, setScheduleTarget] = useState<{ mode: "single"; clip: ClipDraft } | { mode: "bulk"; clips: ClipDraft[] } | null>(null);
  const [scheduleDate, setScheduleDate] = useState("");
  const [scheduleMinDate, setScheduleMinDate] = useState("");
  const [scheduleTime, setScheduleTime] = useState("");
  const [schedulePrivacy, setSchedulePrivacy] = useState<"default" | TargetPrivacy>("default");
  const [scheduleIntervalMin, setScheduleIntervalMin] = useState("0");
  const [scheduling, setScheduling] = useState(false);
  const [scheduleError, setScheduleError] = useState<string | null>(null);
  const [cancelingScheduleId, setCancelingScheduleId] = useState<number | null>(null);
  const selectAllRef = useRef<HTMLInputElement>(null);
  const shiftHeldRef = useRef(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const pruneSelection = useCallback((nextClips: ClipDraft[]) => {
    const valid = new Set(nextClips.filter((c) => c.status !== "deleted").map((c) => c.id));
    setSelectedIds((prev) => {
      if (prev.size === 0) return prev;
      let changed = false;
      const next = new Set<number>();
      for (const id of prev) {
        if (valid.has(id)) {
          next.add(id);
        } else {
          changed = true;
        }
      }
      return changed ? next : prev;
    });
    setLastSelectedIndex((prev) => (prev !== null && prev >= nextClips.length ? null : prev));
  }, []);

  const hydratePublications = useCallback(async (items: ClipDraft[]) => {
    const targets = items.filter((c) => ["posted", "failed", "approved"].includes(c.status));
    if (targets.length === 0) return;
    const results = await Promise.allSettled(
      targets.map(async (c) => ({ clipId: c.id, res: await getClipPublications(c.id) }))
    );
    const next: Record<number, ClipPublication[]> = {};
    for (const entry of results) {
      if (entry.status === "fulfilled" && entry.value.res.items.length > 0) {
        next[entry.value.clipId] = entry.value.res.items;
      }
    }
    if (Object.keys(next).length > 0) setPublicationsMap((prev) => ({ ...prev, ...next }));
  }, []);

  const fetchAll = useCallback(
    () =>
      Promise.all([
        getClips({
          status: statusFilter === "all" ? undefined : statusFilter,
          streamer_id: streamerFilter === "all" ? undefined : streamerFilter,
        }),
        getStreamers(),
      ]),
    [statusFilter, streamerFilter]
  );

  const loadData = useCallback(async () => {
    try {
      const [clipsRes, streamersRes] = await fetchAll();
      setClips(clipsRes.items);
      pruneSelection(clipsRes.items);
      setStreamers(streamersRes.items);
      setError(null);
      void hydratePublications(clipsRes.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load clip drafts");
    } finally {
      setLoading(false);
    }
  }, [fetchAll, hydratePublications, pruneSelection]);

  useEffect(() => {
    let active = true;
    fetchAll()
      .then(([clipsRes, streamersRes]) => {
        if (!active) return;
        setClips(clipsRes.items);
        pruneSelection(clipsRes.items);
        setStreamers(streamersRes.items);
        setError(null);
        void hydratePublications(clipsRes.items);
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : "Failed to load clip drafts");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [fetchAll, hydratePublications, pruneSelection]);

  const streamerNameById = new Map<number, string>(streamers.map((s) => [s.id, s.name]));
  const replaceClip = (updated: ClipDraft) => {
    setClips((prev) => {
      const next = prev.map((item) => (item.id === updated.id ? updated : item));
      pruneSelection(next);
      return next;
    });
    if (previewClip?.id === updated.id) setPreviewClip(updated);
  };

  const handleUploadDrive = async (clip: ClipDraft) => {
    setUploadingId(clip.id);
    try {
      replaceClip(await uploadClipToDrive(clip.id));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to upload clip to Drive");
    } finally {
      setUploadingId(null);
    }
  };

  const handlePostNow = async (clip: ClipDraft) => {
    const privacy = privacyMap[clip.id];
    setPublishingId(clip.id);
    try {
      const res = await approveClip(clip.id, {
        privacy_status: privacy && privacy !== "default" ? privacy : undefined,
        publish_now: true,
      });
      replaceClip(res.clip);
      setPublicationsMap((prev) => ({ ...prev, [res.clip.id]: res.publications }));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to publish clip to YouTube");
      try {
        const pubRes = await getClipPublications(clip.id);
        setPublicationsMap((prev) => ({ ...prev, [clip.id]: pubRes.items }));
      } catch {
        // ignore follow-up fetch errors
      }
    } finally {
      setPublishingId(null);
    }
  };

  const handleReject = async (clip: ClipDraft) => {
    setRejectingId(clip.id);
    try {
      replaceClip(await rejectClip(clip.id));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to reject clip");
    } finally {
      setRejectingId(null);
    }
  };

  const openEdit = (clip: ClipDraft) => {
    setEditingClip(clip);
    setEditTitle(clip.title || "");
    setEditDescription(clip.description || "");
    setEditError(null);
  };

  const handleSaveEdit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!editingClip) return;
    setSavingEdit(true);
    setEditError(null);
    try {
      replaceClip(await updateClip(editingClip.id, { title: editTitle.trim(), description: editDescription.trim() }));
      setEditingClip(null);
    } catch (err) {
      setEditError(err instanceof Error ? err.message : "Failed to update clip");
    } finally {
      setSavingEdit(false);
    }
  };

  const handleConfirmDelete = async () => {
    if (!deletingClip) return;
    setDeleting(true);
    try {
      await deleteClip(deletingClip.id, deleteDrive);
      setClips((prev) => {
        const next = prev.filter((item) => item.id !== deletingClip.id);
        pruneSelection(next);
        return next;
      });
      setDeletingClip(null);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete clip");
    } finally {
      setDeleting(false);
    }
  };

  const selectableClips = useMemo(
    () => clips.filter((c) => c.status !== "deleted"),
    [clips]
  );
  const selectedClips = useMemo(
    () => selectableClips.filter((c) => selectedIds.has(c.id)),
    [selectableClips, selectedIds]
  );
  const activeSelectedIds = useMemo(
    () => new Set(selectedClips.map((c) => c.id)),
    [selectedClips]
  );
  const selectedCount = selectedClips.length;
  const allSelected =
    selectableClips.length > 0 && selectedCount === selectableClips.length;
  const isIndeterminate = selectedCount > 0 && !allSelected;
  const bulkBusy = bulkAction !== null;
  const anySelectedHasDrive = selectedClips.some((c) => Boolean(c.drive_url));

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = isIndeterminate;
    }
  }, [isIndeterminate]);

  const clearSelection = () => {
    setSelectedIds(new Set());
    setLastSelectedIndex(null);
  };

  const handleToggleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedIds(new Set(selectableClips.map((c) => c.id)));
    } else {
      setSelectedIds(new Set());
    }
    setLastSelectedIndex(null);
  };

  const handleRowCheckboxChange = (clip: ClipDraft, index: number, checked: boolean) => {
    const shiftHeld = shiftHeldRef.current;
    shiftHeldRef.current = false;

    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (shiftHeld && lastSelectedIndex !== null && lastSelectedIndex < clips.length) {
        const start = Math.min(lastSelectedIndex, index);
        const end = Math.max(lastSelectedIndex, index);
        for (let k = start; k <= end; k++) {
          const item = clips[k];
          if (!item || item.status === "deleted") continue;
          if (checked) {
            next.add(item.id);
          } else {
            next.delete(item.id);
          }
        }
      } else if (checked) {
        next.add(clip.id);
      } else {
        next.delete(clip.id);
      }
      return next;
    });
    setLastSelectedIndex(index);
  };

  const executeBulkPost = async () => {
    const ids = selectedClips.map((c) => c.id);
    if (ids.length === 0) return;
    setBulkAction("post");
    try {
      const res = await bulkApproveClips({
        clip_ids: ids,
        privacy_status: bulkPrivacy !== "default" ? bulkPrivacy : undefined,
        publish_now: true,
      });

      const succeededById = new Map<number, ClipDraft>(
        res.succeeded.map((entry) => [entry.clip.id, entry.clip])
      );
      if (succeededById.size > 0) {
        setClips((prev) =>
          prev.map((item) => succeededById.get(item.id) ?? item)
        );
        setPublicationsMap((prev) => {
          const next = { ...prev };
          for (const entry of res.succeeded) {
            next[entry.clip.id] = entry.publications;
          }
          return next;
        });
        if (previewClip && succeededById.has(previewClip.id)) {
          setPreviewClip(succeededById.get(previewClip.id) ?? null);
        }
      }

      const failedIds = new Set(res.failed.map((f) => f.clip_id));
      setSelectedIds(failedIds);
      if (failedIds.size === 0) {
        setLastSelectedIndex(null);
      }

      if (res.failed.length === 0) {
        toast.success(
          `Posted ${res.succeeded.length} ${res.succeeded.length === 1 ? "clip" : "clips"} to YouTube`
        );
      } else if (res.succeeded.length > 0) {
        toast.error(
          `Posted ${res.succeeded.length}, failed ${res.failed.length}: ${res.failed[0].detail}`
        );
      } else {
        toast.error(
          `Failed to post ${res.failed.length} ${res.failed.length === 1 ? "clip" : "clips"}: ${res.failed[0].detail}`
        );
      }
      setConfirmBulkPostOpen(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to publish selected clips");
    } finally {
      setBulkAction(null);
    }
  };

  const handleBulkPostClick = () => {
    if (selectedCount > 1) {
      setConfirmBulkPostOpen(true);
    } else {
      void executeBulkPost();
    }
  };

  const executeBulkReject = async () => {
    const ids = selectedClips.map((c) => c.id);
    if (ids.length === 0) return;
    setBulkAction("reject");
    try {
      const res = await bulkRejectClips(ids);
      const updatedById = new Map<number, ClipDraft>(
        res.updated.map((item) => [item.id, item])
      );
      if (updatedById.size > 0) {
        setClips((prev) =>
          prev.map((item) => updatedById.get(item.id) ?? item)
        );
        if (previewClip && updatedById.has(previewClip.id)) {
          setPreviewClip(updatedById.get(previewClip.id) ?? null);
        }
      }
      clearSelection();
      const parts = [
        `Rejected ${res.updated.length} ${res.updated.length === 1 ? "clip" : "clips"}`,
      ];
      if (res.skipped.length > 0) {
        parts.push(`${res.skipped.length} skipped`);
      }
      if (res.missing.length > 0) {
        parts.push(`${res.missing.length} missing`);
      }
      if (res.updated.length > 0) {
        toast.success(parts.join(" · "));
      } else {
        toast.info(parts.join(" · "));
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to reject selected clips");
    } finally {
      setBulkAction(null);
    }
  };

  const openSingleSchedule = (clip: ClipDraft) => {
    const existing = clip.scheduled_publish_at ? new Date(clip.scheduled_publish_at) : null;
    const now = new Date();
    const base = existing && !Number.isNaN(existing.getTime()) && existing.getTime() > now.getTime()
      ? existing
      : defaultScheduleDate();
    setScheduleMinDate(toLocalDateInputValue(now));
    setScheduleDate(toLocalDateInputValue(base));
    setScheduleTime(toLocalTimeInputValue(base));
    setSchedulePrivacy(
      (clip.scheduled_privacy_status as TargetPrivacy | null) ??
        privacyMap[clip.id] ??
        "default"
    );
    setScheduleIntervalMin("0");
    setScheduleError(null);
    setScheduleTarget({ mode: "single", clip });
  };

  const openBulkSchedule = () => {
    const eligible = selectedClips.filter((c) => c.status !== "posted" && c.status !== "deleted");
    if (eligible.length === 0) {
      toast.error("Selected clips are already posted or deleted.");
      return;
    }
    const base = defaultScheduleDate();
    setScheduleMinDate(toLocalDateInputValue(new Date()));
    setScheduleDate(toLocalDateInputValue(base));
    setScheduleTime(toLocalTimeInputValue(base));
    setSchedulePrivacy(bulkPrivacy);
    setScheduleIntervalMin(eligible.length > 1 ? "180" : "0");
    setScheduleError(null);
    setScheduleTarget({ mode: "bulk", clips: eligible });
  };

  const applySchedulePreset = (preset: "plus1h" | "tonight8" | "tomorrow12" | "tomorrow18") => {
    const d = new Date();
    if (preset === "plus1h") {
      d.setTime(d.getTime() + 60 * 60 * 1000);
      d.setSeconds(0, 0);
    } else if (preset === "tonight8") {
      d.setHours(20, 0, 0, 0);
      if (d.getTime() <= Date.now() + 60_000) {
        d.setDate(d.getDate() + 1);
      }
    } else if (preset === "tomorrow12") {
      d.setDate(d.getDate() + 1);
      d.setHours(12, 0, 0, 0);
    } else {
      d.setDate(d.getDate() + 1);
      d.setHours(18, 0, 0, 0);
    }
    setScheduleDate(toLocalDateInputValue(d));
    setScheduleTime(toLocalTimeInputValue(d));
    setScheduleError(null);
  };

  const parsedScheduleDate = useMemo(() => {
    if (!scheduleDate || !scheduleTime) return null;
    const d = new Date(`${scheduleDate}T${scheduleTime}:00`);
    return Number.isNaN(d.getTime()) ? null : d;
  }, [scheduleDate, scheduleTime]);

  const handleConfirmSchedule = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!scheduleTarget) return;
    if (!parsedScheduleDate) {
      setScheduleError("Choose both a valid date and time.");
      return;
    }
    if (parsedScheduleDate.getTime() < Date.now() - 30_000) {
      setScheduleError("Scheduled time must be in the future.");
      return;
    }
    const isoUtc = parsedScheduleDate.toISOString();
    const priv = schedulePrivacy !== "default" ? schedulePrivacy : null;
    setScheduling(true);
    setScheduleError(null);
    if (scheduleTarget.mode === "bulk") setBulkAction("schedule");
    try {
      if (scheduleTarget.mode === "single") {
        const updated = await scheduleClip(scheduleTarget.clip.id, {
          scheduled_at: isoUtc,
          privacy_status: priv,
        });
        replaceClip(updated);
        toast.success(`Scheduled for ${formatDateTime(updated.scheduled_publish_at)}`);
      } else {
        const res = await bulkScheduleClips({
          clip_ids: scheduleTarget.clips.map((c) => c.id),
          scheduled_at: isoUtc,
          interval_minutes: Number(scheduleIntervalMin) || 0,
          privacy_status: priv,
        });
        const updatedById = new Map<number, ClipDraft>(res.updated.map((item) => [item.id, item]));
        if (updatedById.size > 0) {
          setClips((prev) => prev.map((item) => updatedById.get(item.id) ?? item));
        }
        clearSelection();
        toast.success(
          `Scheduled ${res.updated.length} ${res.updated.length === 1 ? "clip" : "clips"} starting ${formatDateTime(isoUtc)}`
        );
      }
      setScheduleTarget(null);
    } catch (err) {
      setScheduleError(err instanceof Error ? err.message : "Failed to schedule post");
    } finally {
      setScheduling(false);
      setBulkAction(null);
    }
  };

  const handleCancelSchedule = async (clip: ClipDraft) => {
    setCancelingScheduleId(clip.id);
    try {
      replaceClip(await cancelClipSchedule(clip.id));
      toast.success("Scheduled post canceled");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to cancel schedule");
    } finally {
      setCancelingScheduleId(null);
    }
  };

  const openBulkDeleteDialog = () => {
    setBulkDeleteDrive(false);
    setConfirmBulkDeleteOpen(true);
  };

  const executeBulkDelete = async () => {
    const ids = selectedClips.map((c) => c.id);
    if (ids.length === 0) return;
    setBulkAction("delete");
    try {
      const res = await bulkDeleteClips(ids, bulkDeleteDrive);
      const deletedSet = new Set(res.deleted_ids);
      if (deletedSet.size > 0) {
        setClips((prev) => prev.filter((item) => !deletedSet.has(item.id)));
        if (previewClip && deletedSet.has(previewClip.id)) {
          setPreviewClip(null);
        }
      }
      const driveFailedCount = res.drive_failed_ids?.length ?? 0;
      if (res.failed.length > 0) {
        setSelectedIds(new Set(res.failed.map((f) => f.clip_id)));
        const driveSuffix =
          driveFailedCount > 0 ? ` (${driveFailedCount} Drive copies failed)` : "";
        toast.error(
          `Deleted ${res.deleted_ids.length}, failed ${res.failed.length}: ${res.failed[0].detail}${driveSuffix}`
        );
      } else if (driveFailedCount > 0) {
        clearSelection();
        toast.warning(
          `Deleted ${res.deleted_ids.length} locally; ${driveFailedCount} Google Drive ${driveFailedCount === 1 ? "copy" : "copies"} could not be deleted`
        );
      } else {
        clearSelection();
        toast.success(
          `Deleted ${res.deleted_ids.length} ${res.deleted_ids.length === 1 ? "clip" : "clips"}`
        );
      }
      setConfirmBulkDeleteOpen(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete selected clips");
    } finally {
      setBulkAction(null);
    }
  };

  const statusFilterLabel = STATUS_OPTIONS.find((o) => o.value === statusFilter)?.label ?? "Every status";
  const streamerLabel =
    streamerFilter === "all" ? "All streamers" : streamers.find((s) => String(s.id) === streamerFilter)?.name ?? "Streamer";

  return (
    <div className="flex flex-col gap-(--space-lg)">
      <PageHeader
        title="Clips"
        meta="Review, post, or clear generated clip drafts"
        actions={
          <div className="glass flex flex-wrap items-center gap-(--space-xs) rounded-full p-(--space-2xs)">
            <Select
              value={statusFilter}
              onValueChange={(val) => {
                setLoading(true);
                setStatusFilter(val ?? "all");
              }}
            >
              <SelectTrigger className="min-w-[136px] rounded-full border-(--rule-glass)" aria-label="Status">
                <SelectValue>{statusFilterLabel}</SelectValue>
              </SelectTrigger>
              <SelectContent align="end">
                {STATUS_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    {opt.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={streamerFilter}
              onValueChange={(val) => {
                setLoading(true);
                setStreamerFilter(val ?? "all");
              }}
            >
              <SelectTrigger className="min-w-[136px] rounded-full border-(--rule-glass)" aria-label="Streamer">
                <SelectValue>{streamerLabel}</SelectValue>
              </SelectTrigger>
              <SelectContent align="end">
                <SelectItem value="all">All streamers</SelectItem>
                {streamers.map((s) => (
                  <SelectItem key={s.id} value={String(s.id)}>
                    {s.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              variant="outline"
              disabled={loading}
              onClick={() => {
                setLoading(true);
                void loadData();
              }}
            >
              <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
              Refresh
            </Button>
          </div>
        }
      />

      {error && <Notice tone="error">{error}</Notice>}

      <Panel title="Drafts" count={clips.length} flush index={1}>
        {loading && clips.length === 0 ? (
          <div className="flex flex-col gap-(--space-xs) px-(--space-lg)">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-16 animate-pulse rounded-(--radius-inner) bg-(--rule-glass)" />
            ))}
          </div>
        ) : clips.length === 0 ? (
          <EmptyState icon={<Film />}>
            No clips match these filters. Drafts arrive here after each recorded hour is clipped.
          </EmptyState>
        ) : (
          <>
            {selectedCount > 0 && (
              <div
                role="region"
                aria-label="Bulk clip actions"
                className="fade-in flex flex-wrap items-center justify-between gap-(--space-sm) border-y border-(--rule-glass) bg-[color-mix(in_oklab,var(--color-signal)_8%,transparent)] px-(--space-lg) py-(--space-sm)"
              >
                <div className="flex items-center gap-(--space-xs)">
                  <span className="text-sm font-medium text-foreground tabular-nums">
                    {selectedCount} selected
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={bulkBusy}
                    onClick={clearSelection}
                  >
                    Clear
                  </Button>
                </div>
                <div className="flex flex-wrap items-center gap-(--space-xs)">
                  <Select
                    value={bulkPrivacy}
                    disabled={bulkBusy}
                    onValueChange={(val) =>
                      setBulkPrivacy((val as "default" | TargetPrivacy) ?? "default")
                    }
                  >
                    <SelectTrigger
                      size="sm"
                      className="min-w-[136px] rounded-full"
                      aria-label="Visibility for selected clips"
                    >
                      <SelectValue>
                        {PRIVACY_OPTIONS.find((o) => o.value === bulkPrivacy)?.label}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent align="end">
                      {PRIVACY_OPTIONS.map((opt) => (
                        <SelectItem key={opt.value} value={opt.value}>
                          {opt.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <div className="inline-flex items-center">
                    <Button
                      type="button"
                      size="sm"
                      disabled={bulkBusy}
                      aria-busy={bulkAction === "post"}
                      onClick={handleBulkPostClick}
                      className="rounded-r-none pr-2"
                    >
                      {bulkAction === "post"
                        ? `Posting (${selectedCount})`
                        : bulkAction === "schedule"
                          ? `Scheduling (${selectedCount})`
                          : `Post (${selectedCount})`}
                    </Button>
                    <DropdownMenu>
                      <DropdownMenuTrigger
                        render={
                          <Button
                            type="button"
                            size="sm"
                            disabled={bulkBusy}
                            aria-label={`Post or schedule options for ${selectedCount} selected clips`}
                            className="rounded-l-none border-l border-(--color-signal-ink)/20 px-1.5"
                          />
                        }
                      >
                        <ChevronDown className="size-3.5" />
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="w-48">
                        <DropdownMenuItem onClick={handleBulkPostClick}>
                          <Send /> Post all now ({selectedCount})
                        </DropdownMenuItem>
                        <DropdownMenuItem onClick={openBulkSchedule}>
                          <CalendarClock /> Schedule posts…
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={bulkBusy}
                    aria-busy={bulkAction === "reject"}
                    onClick={() => void executeBulkReject()}
                  >
                    {bulkAction === "reject" ? `Rejecting (${selectedCount})` : `Reject (${selectedCount})`}
                  </Button>
                  <Button
                    type="button"
                    variant="destructive"
                    size="sm"
                    disabled={bulkBusy}
                    aria-busy={bulkAction === "delete"}
                    onClick={openBulkDeleteDialog}
                  >
                    <Trash2 className="size-3.5" />
                    {bulkAction === "delete" ? `Deleting (${selectedCount})` : `Delete (${selectedCount})`}
                  </Button>
                </div>
              </div>
            )}
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead className="w-10 pl-(--space-lg)">
                    <input
                      ref={selectAllRef}
                      type="checkbox"
                      checked={allSelected}
                      aria-checked={allSelected ? true : isIndeterminate ? "mixed" : false}
                      aria-label={allSelected ? "Deselect all visible clips" : "Select all visible clips"}
                      disabled={bulkBusy || selectableClips.length === 0}
                      onChange={(e) => handleToggleSelectAll(e.target.checked)}
                      className="size-4 cursor-pointer accent-(--color-signal) disabled:cursor-not-allowed disabled:opacity-55"
                    />
                  </TableHead>
                  <TableHead>Clip</TableHead>
                  <TableHead className="hidden md:table-cell">Streamer</TableHead>
                  <TableHead className="hidden text-right md:table-cell">Length</TableHead>
                  <TableHead className="hidden text-right lg:table-cell">Score</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="pr-(--space-lg) text-right">Publish</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className="tabular-nums">
                {clips.map((clip, i) => {
                  const pubs = publicationsMap[clip.id] ?? [];
                  const isPublishing = publishingId === clip.id;
                  const isRejecting = rejectingId === clip.id;
                  const busy = isPublishing || isRejecting;
                  const gone = clip.status === "deleted";
                  const isSelected = !gone && activeSelectedIds.has(clip.id);
                  const privacy = privacyMap[clip.id] ?? "default";
                  const title = clip.title || `Draft ${clip.draft_number ?? clip.id}`;
                  return (
                    <TableRow
                      key={clip.id}
                      data-state={isSelected ? "selected" : undefined}
                      className={`rise-row ${isSelected ? "bg-[color-mix(in_oklab,var(--color-signal)_10%,transparent)] hover:bg-[color-mix(in_oklab,var(--color-signal)_14%,transparent)]" : ""}`}
                      style={rise(i)}
                    >
                      <TableCell className="w-10 pl-(--space-lg)">
                        <input
                          type="checkbox"
                          checked={isSelected}
                          disabled={gone || bulkBusy}
                          aria-label={`Select ${title}`}
                          onClick={(e) => {
                            shiftHeldRef.current = e.shiftKey;
                          }}
                          onChange={(e) => handleRowCheckboxChange(clip, i, e.target.checked)}
                          className="size-4 cursor-pointer accent-(--color-signal) disabled:cursor-not-allowed disabled:opacity-55"
                        />
                      </TableCell>
                      <TableCell className="max-w-[380px]">
                      <div className="flex items-center gap-(--space-sm)">
                        <button
                          type="button"
                          onClick={() => setPreviewClip(clip)}
                          aria-label={`Preview ${title}`}
                          className="press group relative aspect-[9/16] w-10 shrink-0 overflow-hidden rounded-(--radius-inner) bg-(--color-shade) shadow-[inset_0_0_0_1px_var(--rule-glass)]"
                        >
                          {clip.thumbnail_path ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img
                              src={getClipThumbnailUrl(clip.id)}
                              alt=""
                              width={40}
                              height={71}
                              className="size-full object-cover"
                              loading={i < 4 ? "eager" : "lazy"}
                            />
                          ) : (
                            <Film className="absolute inset-0 m-auto size-3.5 text-(--color-ink-3)" />
                          )}
                          <span className="absolute inset-0 flex items-center justify-center bg-[color-mix(in_oklab,var(--color-shade)_55%,transparent)] opacity-0 transition-opacity duration-(--dur-short) group-hover:opacity-100 group-focus-visible:opacity-100">
                            <Play className="size-3.5 text-foreground" />
                          </span>
                        </button>
                        <div className="min-w-0">
                          <div className="truncate font-medium text-foreground" title={title}>
                            {title}
                          </div>
                          {clip.description && (
                            <div className="line-clamp-2 text-sm whitespace-pre-line text-(--color-ink-3)" title={clip.description}>
                              {clip.description}
                            </div>
                          )}
                          {clip.drive_url && (
                            <a
                              href={clip.drive_url}
                              target="_blank"
                              rel="noreferrer"
                              className="inline-flex items-center gap-(--space-2xs) text-sm text-(--color-ink-3) hover:text-foreground"
                            >
                              On Drive <ExternalLink className="size-3" />
                            </a>
                          )}
                        </div>
                      </div>
                    </TableCell>
                    <TableCell className="hidden text-(--color-ink-2) md:table-cell">
                      {clip.streamer_name || streamerNameById.get(clip.streamer_id) || `Streamer ${clip.streamer_id}`}
                    </TableCell>
                    <TableCell className="hidden text-right md:table-cell">
                      {typeof clip.duration_sec === "number" ? `${clip.duration_sec.toFixed(0)}s` : ""}
                    </TableCell>
                    <TableCell className="hidden text-right text-(--color-ink-3) lg:table-cell">
                      {typeof clip.score === "number" ? clip.score.toFixed(0) : ""}
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-col gap-(--space-3xs)">
                        <StatusWord tone={toneForStatus(clip.status)} busy={isPublishing || cancelingScheduleId === clip.id}>
                          {isPublishing ? "posting" : statusLabel(clip.status)}
                        </StatusWord>
                        {clip.status === "scheduled" && clip.scheduled_publish_at && (
                          <span className="inline-flex items-center gap-(--space-2xs) text-xs text-(--color-owed) tabular-nums">
                            <CalendarClock className="size-3 shrink-0" />
                            {formatDateTime(clip.scheduled_publish_at)}
                            {clip.scheduled_privacy_status ? ` · ${clip.scheduled_privacy_status}` : ""}
                          </span>
                        )}
                        {pubs.map((pub) =>
                          pub.youtube_url ? (
                            <a
                              key={pub.id}
                              href={pub.youtube_url}
                              target="_blank"
                              rel="noreferrer"
                              className="inline-flex items-center gap-(--space-2xs) text-sm text-(--color-signal) hover:underline"
                            >
                              {pub.channel_title || "YouTube"}, {pub.privacy_status}
                              <ExternalLink className="size-3" />
                            </a>
                          ) : (
                            <span
                              key={pub.id}
                              className={`max-w-[220px] truncate text-sm ${pub.status === "failed" ? "text-(--color-error)" : "text-(--color-ink-3)"}`}
                              title={pub.error_message || undefined}
                            >
                              {pub.channel_title || "YouTube"}: {pub.status}
                              {pub.error_message ? ` (${pub.error_message})` : ""}
                            </span>
                          )
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="pr-(--space-lg)">
                      <div className="flex items-center justify-end gap-(--space-2xs)">
                        <Select
                          value={privacy}
                          disabled={busy || gone || bulkBusy}
                          onValueChange={(val) =>
                            setPrivacyMap((prev) => ({ ...prev, [clip.id]: (val as "default" | TargetPrivacy) ?? "default" }))
                          }
                        >
                          <SelectTrigger size="sm" className="min-w-[124px] rounded-full" aria-label={`Visibility for ${title}`}>
                            <SelectValue>{PRIVACY_OPTIONS.find((o) => o.value === privacy)?.label}</SelectValue>
                          </SelectTrigger>
                          <SelectContent align="end">
                            {PRIVACY_OPTIONS.map((opt) => (
                              <SelectItem key={opt.value} value={opt.value}>
                                {opt.label}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        <div className="inline-flex items-center">
                          <Button
                            size="sm"
                            disabled={busy || gone || bulkBusy}
                            aria-busy={isPublishing}
                            onClick={() => void handlePostNow(clip)}
                            className="rounded-r-none pr-2"
                          >
                            {isPublishing ? "Posting" : "Post"}
                          </Button>
                          <DropdownMenu>
                            <DropdownMenuTrigger
                              render={
                                <Button
                                  type="button"
                                  size="sm"
                                  disabled={busy || gone || bulkBusy}
                                  aria-label={`Post or schedule options for ${title}`}
                                  className="rounded-l-none border-l border-(--color-signal-ink)/20 px-1.5"
                                />
                              }
                            >
                              <ChevronDown className="size-3.5" />
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end" className="w-48">
                              <DropdownMenuItem onClick={() => void handlePostNow(clip)}>
                                <Send /> Post now
                              </DropdownMenuItem>
                              {clip.status !== "posted" && (
                                <DropdownMenuItem onClick={() => openSingleSchedule(clip)}>
                                  <CalendarClock />
                                  {clip.status === "scheduled" ? "Reschedule…" : "Schedule post…"}
                                </DropdownMenuItem>
                              )}
                              {clip.status === "scheduled" && (
                                <>
                                  <DropdownMenuSeparator />
                                  <DropdownMenuItem
                                    disabled={cancelingScheduleId === clip.id}
                                    onClick={() => void handleCancelSchedule(clip)}
                                  >
                                    <CalendarX2 /> Cancel schedule
                                  </DropdownMenuItem>
                                </>
                              )}
                            </DropdownMenuContent>
                          </DropdownMenu>
                        </div>
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={busy || gone || bulkBusy || clip.status === "posted"}
                          aria-busy={isRejecting}
                          onClick={() => void handleReject(clip)}
                        >
                          {isRejecting ? "Rejecting" : "Reject"}
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={busy || gone || bulkBusy || clip.status === "posted"}
                          onClick={() => openEdit(clip)}
                          aria-label={`Edit the post for ${title}`}
                        >
                          <Pencil className="size-3.5" />
                          Edit post
                        </Button>
                        <DropdownMenu>
                          <DropdownMenuTrigger
                            render={
                              <Button
                                variant="ghost"
                                size="icon-sm"
                                disabled={busy || bulkBusy}
                                aria-label={`More for ${title}`}
                              />
                            }
                          >
                            <MoreHorizontal />
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end" className="w-48">
                            <DropdownMenuItem onClick={() => setPreviewClip(clip)}>
                              <Play /> Preview
                            </DropdownMenuItem>
                            {!clip.drive_url && (
                              <DropdownMenuItem disabled={uploadingId === clip.id || bulkBusy} onClick={() => void handleUploadDrive(clip)}>
                                <HardDriveUpload />
                                {uploadingId === clip.id ? "Uploading" : "Upload to Drive"}
                              </DropdownMenuItem>
                            )}
                            <DropdownMenuSeparator />
                            <DropdownMenuItem
                              variant="destructive"
                              disabled={bulkBusy}
                              onClick={() => {
                                setDeleteDrive(false);
                                setDeletingClip(clip);
                              }}
                            >
                              <Trash2 /> Delete
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
            </Table>
          </>
        )}
      </Panel>

      <Dialog open={previewClip !== null} onOpenChange={(open) => !open && setPreviewClip(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="truncate pr-(--space-xl)">
              {previewClip?.title || `Draft ${previewClip?.draft_number ?? previewClip?.id}`}
            </DialogTitle>
            {previewClip?.description && <DialogDescription>{previewClip.description}</DialogDescription>}
          </DialogHeader>
          {previewClip && (
            <video
              controls
              playsInline
              preload="metadata"
              src={getClipVideoUrl(previewClip.id)}
              poster={previewClip.thumbnail_path ? getClipThumbnailUrl(previewClip.id) : undefined}
              className="mx-auto aspect-[9/16] max-h-[70vh] rounded-(--radius-inner) bg-(--color-shade)"
            />
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={editingClip !== null} onOpenChange={(open) => !open && setEditingClip(null)}>
        <DialogContent className="sm:max-w-lg">
          <form onSubmit={handleSaveEdit} className="grid gap-(--space-md)">
            <DialogHeader>
              <DialogTitle>Edit post</DialogTitle>
              <DialogDescription>
                This is exactly what goes to YouTube, whether you post here or approve in Telegram.
              </DialogDescription>
            </DialogHeader>
            {editError && <StatusWord tone="error">{editError}</StatusWord>}
            <div>
              <div className="flex items-baseline justify-between">
                <FieldLabel htmlFor="edit-clip-title">Title</FieldLabel>
                <span className={`text-sm tabular-nums ${editTitle.length > 90 ? "text-(--color-owed)" : "text-(--color-ink-3)"}`}>
                  {editTitle.length}/100
                </span>
              </div>
              <Input id="edit-clip-title" name="title" required maxLength={100} value={editTitle} onChange={(e) => setEditTitle(e.target.value)} />
            </div>
            <div>
              <div className="flex items-baseline justify-between">
                <FieldLabel htmlFor="edit-clip-description">Description</FieldLabel>
                <span className="text-sm text-(--color-ink-3) tabular-nums">{editDescription.length}/5000</span>
              </div>
              <textarea
                id="edit-clip-description"
                name="description"
                rows={6}
                maxLength={5000}
                value={editDescription}
                onChange={(e) => setEditDescription(e.target.value)}
                className="w-full resize-y rounded-(--radius-inner) border border-input bg-(--row-hover) px-(--space-sm) py-(--space-xs) text-sm leading-relaxed outline-2 outline-transparent -outline-offset-2 focus-visible:outline-(--color-focus)"
              />
              <p className="mt-(--space-xs) text-sm text-(--color-ink-3)">
                The streamer&apos;s hashtags sit on the last line; edit or remove them here.
              </p>
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setEditingClip(null)}>
                Cancel
              </Button>
              <Button type="submit" disabled={savingEdit} aria-busy={savingEdit}>
                {savingEdit ? "Saving" : "Save"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={deletingClip !== null} onOpenChange={(open) => !open && setDeletingClip(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete this clip?</DialogTitle>
            <DialogDescription className="truncate">
              {deletingClip?.title || `Draft ${deletingClip?.id}`}
            </DialogDescription>
          </DialogHeader>
          {deletingClip?.drive_url && (
            <Checkbox checked={deleteDrive} onChange={setDeleteDrive}>
              Also delete the copy on Google Drive
            </Checkbox>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeletingClip(null)}>
              Keep it
            </Button>
            <Button variant="destructive" disabled={deleting} aria-busy={deleting} onClick={() => void handleConfirmDelete()}>
              {deleting ? "Deleting" : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={confirmBulkPostOpen}
        onOpenChange={(open) => {
          if (!open && bulkAction !== "post") setConfirmBulkPostOpen(false);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Post {selectedCount} {selectedCount === 1 ? "clip" : "clips"} to YouTube?
            </DialogTitle>
            <DialogDescription>
              The selected clips will be uploaded sequentially to YouTube using{" "}
              {PRIVACY_OPTIONS.find((o) => o.value === bulkPrivacy)?.label.toLowerCase() ?? "channel default"} visibility.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={bulkAction === "post"}
              onClick={() => setConfirmBulkPostOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="button"
              disabled={bulkAction === "post"}
              aria-busy={bulkAction === "post"}
              onClick={() => void executeBulkPost()}
            >
              {bulkAction === "post" ? `Posting (${selectedCount})` : `Post (${selectedCount})`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={scheduleTarget !== null}
        onOpenChange={(open) => {
          if (!open && !scheduling) setScheduleTarget(null);
        }}
      >
        <DialogContent className="sm:max-w-md">
          <form onSubmit={handleConfirmSchedule} className="grid gap-(--space-md)">
            <DialogHeader>
              <DialogTitle>
                {scheduleTarget?.mode === "bulk"
                  ? `Schedule ${scheduleTarget.clips.length} posts`
                  : "Schedule post"}
              </DialogTitle>
              <DialogDescription className="truncate">
                {scheduleTarget?.mode === "single"
                  ? scheduleTarget.clip.title || `Draft ${scheduleTarget.clip.id}`
                  : "Pick the start date and time; uploads run automatically in the background."}
              </DialogDescription>
            </DialogHeader>

            {scheduleError && <StatusWord tone="error">{scheduleError}</StatusWord>}

            <div className="flex flex-wrap gap-(--space-2xs)">
              <Button type="button" variant="outline" size="xs" onClick={() => applySchedulePreset("plus1h")}>
                +1 hour
              </Button>
              <Button type="button" variant="outline" size="xs" onClick={() => applySchedulePreset("tonight8")}>
                Tonight 8:00 PM
              </Button>
              <Button type="button" variant="outline" size="xs" onClick={() => applySchedulePreset("tomorrow12")}>
                Tomorrow 12:00 PM
              </Button>
              <Button type="button" variant="outline" size="xs" onClick={() => applySchedulePreset("tomorrow18")}>
                Tomorrow 6:00 PM
              </Button>
            </div>

            <div className="grid grid-cols-1 gap-(--space-sm) sm:grid-cols-2">
              <div>
                <FieldLabel htmlFor="schedule-post-date">Date</FieldLabel>
                <Input
                  id="schedule-post-date"
                  type="date"
                  required
                  value={scheduleDate}
                  min={scheduleMinDate || undefined}
                  onChange={(e) => {
                    setScheduleDate(e.target.value);
                    setScheduleError(null);
                  }}
                  className="tabular-nums"
                />
              </div>
              <div>
                <FieldLabel htmlFor="schedule-post-time">Time</FieldLabel>
                <Input
                  id="schedule-post-time"
                  type="time"
                  required
                  value={scheduleTime}
                  onChange={(e) => {
                    setScheduleTime(e.target.value);
                    setScheduleError(null);
                  }}
                  className="tabular-nums"
                />
              </div>
            </div>

            <div className="grid grid-cols-1 gap-(--space-sm) sm:grid-cols-2">
              <div>
                <FieldLabel>Visibility</FieldLabel>
                <Select
                  value={schedulePrivacy}
                  onValueChange={(val) => setSchedulePrivacy((val as "default" | TargetPrivacy) ?? "default")}
                >
                  <SelectTrigger className="w-full" aria-label="Scheduled visibility">
                    <SelectValue>{PRIVACY_OPTIONS.find((o) => o.value === schedulePrivacy)?.label}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {PRIVACY_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              {scheduleTarget?.mode === "bulk" && scheduleTarget.clips.length > 1 && (
                <div>
                  <FieldLabel>Spacing</FieldLabel>
                  <Select value={scheduleIntervalMin} onValueChange={(val) => setScheduleIntervalMin(val ?? "0")}>
                    <SelectTrigger className="w-full" aria-label="Schedule spacing">
                      <SelectValue>
                        {SCHEDULE_INTERVAL_OPTIONS.find((o) => o.value === scheduleIntervalMin)?.label}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      {SCHEDULE_INTERVAL_OPTIONS.map((opt) => (
                        <SelectItem key={opt.value} value={opt.value}>
                          {opt.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
            </div>

            {parsedScheduleDate && (
              <p className="rounded-(--radius-inner) border border-(--rule-glass) bg-(--color-paper-2)/60 px-(--space-sm) py-(--space-xs) text-xs text-(--color-ink-2) tabular-nums">
                {scheduleTarget?.mode === "bulk" &&
                scheduleTarget.clips.length > 1 &&
                Number(scheduleIntervalMin) > 0 ? (
                  <>
                    First post: <strong className="text-foreground">{formatDateTime(parsedScheduleDate.toISOString())}</strong>
                    {" · "}
                    Last post:{" "}
                    <strong className="text-foreground">
                      {formatDateTime(
                        new Date(
                          parsedScheduleDate.getTime() +
                            Number(scheduleIntervalMin) * 60_000 * (scheduleTarget.clips.length - 1)
                        ).toISOString()
                      )}
                    </strong>
                  </>
                ) : (
                  <>
                    Will publish on{" "}
                    <strong className="text-foreground">{formatDateTime(parsedScheduleDate.toISOString())}</strong>{" "}
                    (your local time).
                  </>
                )}
              </p>
            )}

            <DialogFooter>
              <Button type="button" variant="outline" disabled={scheduling} onClick={() => setScheduleTarget(null)}>
                Cancel
              </Button>
              <Button type="submit" disabled={scheduling} aria-busy={scheduling}>
                <CalendarClock className="size-3.5" />
                {scheduling
                  ? "Scheduling…"
                  : scheduleTarget?.mode === "bulk"
                    ? `Schedule (${scheduleTarget.clips.length})`
                    : "Schedule"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={confirmBulkDeleteOpen}
        onOpenChange={(open) => {
          if (!open && bulkAction !== "delete") setConfirmBulkDeleteOpen(false);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Delete {selectedCount} {selectedCount === 1 ? "clip" : "clips"}?
            </DialogTitle>
            <DialogDescription>
              Local video files and thumbnails for {selectedCount} selected{" "}
              {selectedCount === 1 ? "draft" : "drafts"} will be permanently removed.
            </DialogDescription>
          </DialogHeader>
          {anySelectedHasDrive && (
            <Checkbox
              checked={bulkDeleteDrive}
              disabled={bulkAction === "delete"}
              onChange={setBulkDeleteDrive}
            >
              Also delete copies on Google Drive
            </Checkbox>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={bulkAction === "delete"}
              onClick={() => setConfirmBulkDeleteOpen(false)}
            >
              Keep them
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={bulkAction === "delete"}
              aria-busy={bulkAction === "delete"}
              onClick={() => void executeBulkDelete()}
            >
              {bulkAction === "delete" ? `Deleting (${selectedCount})` : `Delete (${selectedCount})`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
