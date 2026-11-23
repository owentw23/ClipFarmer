"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { ExternalLink, MonitorPlay, MoreHorizontal, Play, Plus, Radio, RefreshCw, Settings2, Square, Trash2 } from "lucide-react";
import { toast } from "sonner";

import {
  Checkbox,
  EmptyState,
  FieldLabel,
  Initial,
  Notice,
  PageHeader,
  Panel,
  StatusWord,
  Switch,
  Tally,
  formatDateTime,
  rise,
} from "@/components/page-kit";
import { CAPTION_LABELS, StreamerSettingsDialog, parseHashtags } from "@/components/StreamerSettingsDialog";
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
  Account,
  Streamer,
  StreamerPlatform,
  TargetPrivacy,
  checkStreamerNow,
  createStreamer,
  deleteStreamer,
  getAccounts,
  getStreamerTargets,
  getStreamers,
  resumeStreamerRecording,
  stopStreamerRecording,
  updateStreamer,
  updateStreamerTargets,
} from "@/lib/api";

const DEFAULT_CLIP_OPTS = {
  min_len: 30,
  max_len: 60,
  cam_auto: true,
  layout: "stack",
  fallback_layout: "blur",
  caption_style: "hormozi",
  language: null as string | null,
};

const PLATFORM_LABELS: Record<StreamerPlatform, string> = { twitch: "Twitch", youtube: "YouTube" };
const PRIVACY_LABELS: Record<TargetPrivacy, string> = { public: "Public", unlisted: "Unlisted", private: "Private" };

interface TargetDraft {
  selected: boolean;
  default_privacy: TargetPrivacy;
}

export default function StreamersPage() {
  const [streamers, setStreamers] = useState<Streamer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [checkingId, setCheckingId] = useState<number | null>(null);
  const [stoppingId, setStoppingId] = useState<number | null>(null);

  const [name, setName] = useState("");
  const [platform, setPlatform] = useState<StreamerPlatform>("twitch");
  const [channelUrl, setChannelUrl] = useState("");
  const [autoWatch, setAutoWatch] = useState(true);
  const [minLen, setMinLen] = useState(DEFAULT_CLIP_OPTS.min_len);
  const [maxLen, setMaxLen] = useState(DEFAULT_CLIP_OPTS.max_len);
  const [camAuto, setCamAuto] = useState(DEFAULT_CLIP_OPTS.cam_auto);
  const [captionStyle, setCaptionStyle] = useState(DEFAULT_CLIP_OPTS.caption_style);
  const [clipCount, setClipCount] = useState(3);
  const [hashtagText, setHashtagText] = useState("");
  const [settingsStreamer, setSettingsStreamer] = useState<Streamer | null>(null);
  const [resumingId, setResumingId] = useState<number | null>(null);

  const [targetStreamer, setTargetStreamer] = useState<Streamer | null>(null);
  const [targetAccounts, setTargetAccounts] = useState<Account[]>([]);
  const [targetDrafts, setTargetDrafts] = useState<Record<number, TargetDraft>>({});
  const [targetsLoading, setTargetsLoading] = useState(false);
  const [targetsSaving, setTargetsSaving] = useState(false);
  const [targetsError, setTargetsError] = useState<string | null>(null);

  const loadStreamers = useCallback(async () => {
    try {
      const data = await getStreamers();
      setStreamers(data.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load streamers");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    getStreamers()
      .then((data) => {
        if (!active) return;
        setStreamers(data.items);
        setError(null);
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : "Failed to load streamers");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const handleAddStreamer = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!name.trim() || !channelUrl.trim()) return;
    setSubmitting(true);
    try {
      const created = await createStreamer({
        name: name.trim(),
        platform,
        channel_url: channelUrl.trim(),
        auto_watch: autoWatch,
        clip_opts: {
          ...DEFAULT_CLIP_OPTS,
          min_len: Number(minLen) || 30,
          max_len: Number(maxLen) || 60,
          cam_auto: camAuto,
          caption_style: captionStyle || "hormozi",
          clip_count: clipCount,
          hashtags: parseHashtags(hashtagText),
        },
      });
      setStreamers((prev) => [created, ...prev]);
      setName("");
      setChannelUrl("");
      setHashtagText("");
      toast.success(`Watching ${created.name}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to add streamer");
    } finally {
      setSubmitting(false);
    }
  };

  const handleToggleAutoWatch = async (streamer: Streamer) => {
    // Optimistic: flip at once, roll back if the request fails.
    setStreamers((prev) => prev.map((s) => (s.id === streamer.id ? { ...s, auto_watch: !s.auto_watch } : s)));
    try {
      const updated = await updateStreamer(streamer.id, { auto_watch: !streamer.auto_watch });
      setStreamers((prev) => prev.map((item) => (item.id === streamer.id ? updated : item)));
    } catch (err) {
      setStreamers((prev) => prev.map((s) => (s.id === streamer.id ? streamer : s)));
      toast.error(err instanceof Error ? err.message : "Failed to update streamer");
    }
  };

  const handleCheckNow = async (streamer: Streamer) => {
    setCheckingId(streamer.id);
    try {
      const res = await checkStreamerNow(streamer.id);
      setStreamers((prev) =>
        prev.map((item) =>
          item.id === streamer.id ? { ...item, is_live: res.is_live, last_checked_at: res.checked_at } : item
        )
      );
      if (res.is_live) {
        toast.success(`${streamer.name} is live${res.recording_started ? ", recording started" : ""}`);
      } else {
        toast(`${streamer.name} is offline`);
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to check streamer status");
    } finally {
      setCheckingId(null);
    }
  };

  const handleStopRecording = async (streamer: Streamer) => {
    setStoppingId(streamer.id);
    try {
      await stopStreamerRecording(streamer.id);
      setStreamers((prev) => prev.map((s) => (s.id === streamer.id ? { ...s, recording_held: true } : s)));
      toast(`Stopped ${streamer.name}. It stays off until this stream ends.`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to stop recording");
    } finally {
      setStoppingId(null);
    }
  };

  const handleResume = async (streamer: Streamer) => {
    setResumingId(streamer.id);
    try {
      const res = await resumeStreamerRecording(streamer.id);
      setStreamers((prev) =>
        prev.map((s) =>
          s.id === streamer.id ? { ...s, recording_held: false, is_live: res.is_live, last_checked_at: res.checked_at } : s
        )
      );
      toast(res.recording_started ? `Recording ${streamer.name} again` : `${streamer.name} will record when live`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to resume recording");
    } finally {
      setResumingId(null);
    }
  };

  const handleDelete = async (streamer: Streamer) => {
    try {
      await deleteStreamer(streamer.id);
      setStreamers((prev) => prev.filter((item) => item.id !== streamer.id));
      toast(`Removed ${streamer.name}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete streamer");
    }
  };

  const openTargets = async (streamer: Streamer) => {
    setTargetStreamer(streamer);
    setTargetsLoading(true);
    setTargetsError(null);
    try {
      const [targetsRes, accountsRes] = await Promise.all([getStreamerTargets(streamer.id), getAccounts()]);
      const youtubeAccounts = accountsRes.items.filter(
        (acc) =>
          (acc.provider === undefined || acc.provider === "youtube") &&
          !acc.channel_id?.startsWith("drive-only:") &&
          Number.isFinite(Number(acc.id))
      );
      setTargetAccounts(youtubeAccounts);
      const existing = new Map(targetsRes.items.map((t) => [t.youtube_account_id, t]));
      const drafts: Record<number, TargetDraft> = {};
      for (const acc of youtubeAccounts) {
        const accId = Number(acc.id);
        const found = existing.get(accId);
        drafts[accId] = {
          selected: found ? Boolean(found.enabled) : false,
          default_privacy: found?.default_privacy ?? "public",
        };
      }
      setTargetDrafts(drafts);
    } catch (err) {
      setTargetsError(err instanceof Error ? err.message : "Failed to load YouTube channel targets");
    } finally {
      setTargetsLoading(false);
    }
  };

  const handleSaveTargets = async () => {
    if (!targetStreamer) return;
    setTargetsSaving(true);
    setTargetsError(null);
    try {
      const payload = targetAccounts
        .map((acc) => {
          const accId = Number(acc.id);
          const draft = targetDrafts[accId];
          if (!draft?.selected) return null;
          return { youtube_account_id: accId, default_privacy: draft.default_privacy, enabled: true };
        })
        .filter((item): item is NonNullable<typeof item> => item !== null);
      await updateStreamerTargets(targetStreamer.id, payload);
      toast.success(`Saved ${payload.length} channel${payload.length === 1 ? "" : "s"} for ${targetStreamer.name}`);
      setTargetStreamer(null);
    } catch (err) {
      setTargetsError(err instanceof Error ? err.message : "Failed to save YouTube channel targets");
    } finally {
      setTargetsSaving(false);
    }
  };

  const liveCount = streamers.filter((s) => s.is_live).length;
  const watchingCount = streamers.filter((s) => s.auto_watch).length;
  const twitchCount = streamers.filter((s) => s.platform === "twitch").length;
  const firstLoad = loading && streamers.length === 0;

  return (
    <div className="flex flex-col gap-(--space-lg)">
      <PageHeader
        title="Streamers"
        meta="Checked every minute while the backend runs"
        actions={
          <Button
            variant="outline"
            className="glass"
            disabled={loading}
            onClick={() => {
              setLoading(true);
              void loadStreamers();
            }}
          >
            <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
        }
      />

      {error && <Notice tone="error">{error}</Notice>}

      <div className="grid grid-cols-[minmax(0,1fr)] gap-(--space-lg) lg:grid-cols-12">
        <Panel title="Channels" index={1} className="lg:col-span-4 lg:self-start">
          <Tally
            columns={3}
            loading={firstLoad}
            items={[
              { label: "Watched", value: streamers.length, note: `${twitchCount} Twitch, ${streamers.length - twitchCount} YouTube` },
              { label: "Live now", value: liveCount, note: liveCount ? "Recorded if auto-watched" : "Nobody on air" },
              { label: "Auto-watch on", value: watchingCount, note: `${streamers.length - watchingCount} paused` },
            ]}
          />
        </Panel>

        <Panel title="Add a streamer" index={2} className="lg:col-span-8">
          <form onSubmit={handleAddStreamer} className="flex flex-col gap-(--space-md)">
            <div className="grid gap-(--space-sm) sm:grid-cols-[minmax(0,1fr)_minmax(0,9rem)]">
              <div>
                <FieldLabel htmlFor="streamer-name">Name</FieldLabel>
                <Input id="streamer-name" name="name" autoComplete="off" required placeholder="CaseOh" value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              <div>
                <FieldLabel>Platform</FieldLabel>
                <Select value={platform} onValueChange={(val) => setPlatform((val as StreamerPlatform) ?? "twitch")}>
                  <SelectTrigger className="w-full" aria-label="Platform">
                    <SelectValue>{PLATFORM_LABELS[platform]}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="twitch">Twitch</SelectItem>
                    <SelectItem value="youtube">YouTube</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <div>
              <FieldLabel htmlFor="streamer-url">Channel address</FieldLabel>
              <Input id="streamer-url" name="channel_url" type="url" inputMode="url" required placeholder="https://www.twitch.tv/channel" value={channelUrl} onChange={(e) => setChannelUrl(e.target.value)} />
            </div>
            <div className="grid items-end gap-(--space-sm) sm:grid-cols-4">
              <div>
                <FieldLabel htmlFor="clip-count">Clips per hour</FieldLabel>
                <Input id="clip-count" name="clip_count" type="number" inputMode="numeric" min={1} max={10} value={clipCount} onChange={(e) => setClipCount(Math.max(1, Math.min(10, Number(e.target.value) || 1)))} className="tabular-nums" />
              </div>
              <div>
                <FieldLabel htmlFor="clip-min-len">Min seconds</FieldLabel>
                <Input id="clip-min-len" name="min_len" type="number" inputMode="numeric" min={5} max={180} value={minLen} onChange={(e) => setMinLen(Number(e.target.value))} className="tabular-nums" />
              </div>
              <div>
                <FieldLabel htmlFor="clip-max-len">Max seconds</FieldLabel>
                <Input id="clip-max-len" name="max_len" type="number" inputMode="numeric" min={10} max={300} value={maxLen} onChange={(e) => setMaxLen(Number(e.target.value))} className="tabular-nums" />
              </div>
              <div>
                <FieldLabel>Captions</FieldLabel>
                <Select value={captionStyle} onValueChange={(val) => setCaptionStyle(val ?? "hormozi")}>
                  <SelectTrigger className="w-full" aria-label="Captions">
                    <SelectValue>{CAPTION_LABELS[captionStyle] ?? captionStyle}</SelectValue>
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
              <FieldLabel htmlFor="streamer-tags">Hashtags for the description</FieldLabel>
              <Input id="streamer-tags" name="hashtags" autoComplete="off" placeholder="#gta6 #shorts" value={hashtagText} onChange={(e) => setHashtagText(e.target.value)} />
            </div>
            <div className="flex flex-wrap items-center justify-between gap-(--space-sm) border-t border-(--rule-glass) pt-(--space-md)">
              <div className="flex flex-wrap gap-(--space-lg)">
                <Checkbox checked={camAuto} onChange={setCamAuto}>
                  Find the facecam
                </Checkbox>
                <Checkbox checked={autoWatch} onChange={setAutoWatch}>
                  Record when live
                </Checkbox>
              </div>
              <Button type="submit" disabled={submitting} aria-busy={submitting}>
                <Plus className="size-3.5" />
                {submitting ? "Adding" : "Add streamer"}
              </Button>
            </div>
          </form>
        </Panel>
      </div>

      <Panel title="Watched channels" count={streamers.length} flush index={3}>
        {firstLoad ? (
          <EmptyState>Loading streamers</EmptyState>
        ) : streamers.length === 0 ? (
          <EmptyState icon={<Radio />}>No streamers yet. Add a Twitch or YouTube channel above.</EmptyState>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="pl-(--space-lg)">Channel</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="hidden lg:table-cell">Clip settings</TableHead>
                <TableHead className="hidden md:table-cell">Record when live</TableHead>
                <TableHead className="pr-(--space-lg) text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {streamers.map((s, i) => {
                const opts = s.clip_opts ?? {};
                const clipMin = typeof opts.min_len === "number" ? opts.min_len : 30;
                const clipMax = typeof opts.max_len === "number" ? opts.max_len : 60;
                const cam = opts.cam_auto !== false;
                const captions = typeof opts.caption_style === "string" ? opts.caption_style : "hormozi";
                const perHour = typeof opts.clip_count === "number" ? opts.clip_count : 3;
                const tags = opts.hashtags ?? [];
                const held = Boolean(s.recording_held && s.is_live);
                return (
                  <TableRow key={s.id} className="rise-row" style={rise(i)}>
                    <TableCell className="pl-(--space-lg)">
                      <div className="flex items-center gap-(--space-sm)">
                        <Initial name={s.name} />
                        <div className="min-w-0">
                          <div className="flex items-center gap-(--space-xs)">
                            <span className="font-medium text-foreground">{s.name}</span>
                            <a
                              href={s.channel_url}
                              target="_blank"
                              rel="noreferrer"
                              aria-label={`Open ${s.name}'s channel`}
                              className="press rounded-full text-(--color-ink-3) hover:text-foreground"
                            >
                              <ExternalLink className="size-3.5" />
                            </a>
                          </div>
                          <div className="text-sm text-(--color-ink-3)">{PLATFORM_LABELS[s.platform] ?? s.platform}</div>
                        </div>
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-col gap-(--space-3xs)">
                        {held ? (
                          <StatusWord tone="owed" icon={Square}>Live, stopped by you</StatusWord>
                        ) : s.is_live ? (
                          <StatusWord tone="done" icon={Radio}>Live</StatusWord>
                        ) : (
                          <StatusWord tone="quiet">Offline</StatusWord>
                        )}
                        <span className="text-sm text-(--color-ink-3) tabular-nums">
                          {held ? "Stays off until this stream ends" : `Checked ${formatDateTime(s.last_checked_at)}`}
                        </span>
                      </div>
                    </TableCell>
                    <TableCell className="hidden text-sm tabular-nums lg:table-cell">
                      <div className="text-foreground">
                        {perHour} clips an hour, {clipMin} to {clipMax}s
                      </div>
                      <div className="text-(--color-ink-3)">
                        {cam ? "Facecam found" : "No facecam"}, {CAPTION_LABELS[captions]?.toLowerCase() ?? captions} captions
                      </div>
                      {tags.length > 0 && (
                        <div className="max-w-[260px] truncate text-(--color-ink-3)" title={tags.join(" ")}>
                          {tags.join(" ")}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="hidden md:table-cell">
                      <Switch
                        checked={s.auto_watch}
                        onChange={() => void handleToggleAutoWatch(s)}
                        label={`Record ${s.name} when live`}
                      />
                    </TableCell>
                    <TableCell className="pr-(--space-lg)">
                      <div className="flex items-center justify-end gap-(--space-2xs)">
                        <Button variant="outline" size="sm" disabled={checkingId === s.id} aria-busy={checkingId === s.id} onClick={() => void handleCheckNow(s)}>
                          {checkingId === s.id ? "Checking" : "Check now"}
                        </Button>
                        <Button variant="outline" size="sm" onClick={() => setSettingsStreamer(s)}>
                          <Settings2 className="size-3.5" />
                          Settings
                        </Button>
                        <DropdownMenu>
                          <DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" aria-label={`More for ${s.name}`} />}>
                            <MoreHorizontal />
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end" className="w-52">
                            <DropdownMenuItem onClick={() => void openTargets(s)}>
                              <MonitorPlay /> YouTube channels
                            </DropdownMenuItem>
                            {held ? (
                              <DropdownMenuItem disabled={resumingId === s.id} onClick={() => void handleResume(s)}>
                                <Play /> {resumingId === s.id ? "Resuming" : "Resume recording"}
                              </DropdownMenuItem>
                            ) : (
                              <DropdownMenuItem disabled={stoppingId === s.id} onClick={() => void handleStopRecording(s)}>
                                <Square /> {stoppingId === s.id ? "Stopping" : "Stop recording"}
                              </DropdownMenuItem>
                            )}
                            <DropdownMenuSeparator />
                            <DropdownMenuItem variant="destructive" onClick={() => void handleDelete(s)}>
                              <Trash2 /> Remove streamer
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
        )}
      </Panel>

      <StreamerSettingsDialog
        streamer={settingsStreamer}
        onClose={() => setSettingsStreamer(null)}
        onSaved={(updated) => {
          setStreamers((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
          toast(`Saved clip settings for ${updated.name}`);
        }}
      />

      <Dialog open={targetStreamer !== null} onOpenChange={(open) => !open && setTargetStreamer(null)}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Channels for {targetStreamer?.name}</DialogTitle>
            <DialogDescription>Which YouTube channels get this streamer&apos;s clips, and how they go up.</DialogDescription>
          </DialogHeader>

          <div className="flex max-h-[55vh] flex-col gap-(--space-xs) overflow-y-auto">
            {targetsError && <StatusWord tone="error">{targetsError}</StatusWord>}
            {targetsLoading ? (
              <div className="flex flex-col gap-(--space-xs)">
                <div className="h-14 animate-pulse rounded-(--radius-inner) bg-(--rule-glass)" />
              </div>
            ) : targetAccounts.length === 0 ? (
              <p className="text-sm text-(--color-ink-3)">
                No YouTube channels connected.{" "}
                <Link href="/accounts" className="text-foreground underline underline-offset-4">
                  Connect one in Accounts
                </Link>
              </p>
            ) : (
              targetAccounts.map((acc, i) => {
                const accId = Number(acc.id);
                const draft = targetDrafts[accId] ?? { selected: false, default_privacy: "public" as TargetPrivacy };
                const label = acc.channel_title || acc.name || acc.channel_id || `Channel ${accId}`;
                return (
                  <div
                    key={acc.id}
                    className="rise-row flex flex-wrap items-center justify-between gap-(--space-sm) border-t border-(--rule-glass) py-(--space-sm) first:border-t-0"
                    style={rise(i)}
                  >
                    <Checkbox
                      checked={draft.selected}
                      onChange={(checked) => setTargetDrafts((prev) => ({ ...prev, [accId]: { ...draft, selected: checked } }))}
                    >
                      <span className="min-w-0">
                        <span className="block truncate font-medium text-foreground">{label}</span>
                        <span className="block truncate text-(--color-ink-3)">{acc.email || acc.channel_id || "YouTube channel"}</span>
                      </span>
                    </Checkbox>
                    <Select
                      value={draft.default_privacy}
                      disabled={!draft.selected}
                      onValueChange={(val) =>
                        setTargetDrafts((prev) => ({
                          ...prev,
                          [accId]: { ...draft, default_privacy: (val as TargetPrivacy) ?? "public" },
                        }))
                      }
                    >
                      <SelectTrigger size="sm" className="min-w-[112px]" aria-label={`Visibility on ${label}`}>
                        <SelectValue>{PRIVACY_LABELS[draft.default_privacy]}</SelectValue>
                      </SelectTrigger>
                      <SelectContent align="end">
                        {(Object.keys(PRIVACY_LABELS) as TargetPrivacy[]).map((p) => (
                          <SelectItem key={p} value={p}>
                            {PRIVACY_LABELS[p]}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                );
              })
            )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setTargetStreamer(null)}>
              Cancel
            </Button>
            <Button
              disabled={targetsLoading || targetsSaving || targetAccounts.length === 0}
              aria-busy={targetsSaving}
              onClick={() => void handleSaveTargets()}
            >
              {targetsSaving ? "Saving" : "Save channels"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
