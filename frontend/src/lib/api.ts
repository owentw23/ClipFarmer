// Same-origin requests are proxied to the local FastAPI backend via next.config.ts rewrites.
export const API_BASE = "";

export interface HealthCheckDatabase {
  ok: boolean;
  detail: string;
}

export interface HealthCheckStorage {
  ok: boolean;
  free_gb: number;
  root: string;
}

export interface HealthCheckFfmpeg {
  ok: boolean;
  nvenc: boolean;
  detail: string;
}

export interface HealthCheckNeonclipping {
  ok: boolean;
  url: string;
  detail: string;
}

export interface HealthCheckLlm {
  ok: boolean;
  configured: boolean;
  base_url: string;
  model: string;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  checks: {
    database: HealthCheckDatabase;
    storage: HealthCheckStorage;
    ffmpeg: HealthCheckFfmpeg;
    neonclipping: HealthCheckNeonclipping;
    llm: HealthCheckLlm;
  };
}

export type StreamerPlatform = "youtube" | "twitch";

export interface CamBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface ClipOpts {
  min_len?: number;
  max_len?: number;
  cam_auto?: boolean;
  cam_box?: CamBox | null;
  layout?: string;
  fallback_layout?: string;
  caption_style?: string;
  language?: string | null;
  /** Clips the processor cuts from each recorded hour (1 to 10). */
  clip_count?: number;
  /** Added under every draft's description, e.g. ["#gta6", "#shorts"]. */
  hashtags?: string[];
  [key: string]: unknown;
}

export interface Streamer {
  id: number;
  name: string;
  slug: string;
  platform: StreamerPlatform;
  channel_url: string;
  channel_ref?: string;
  auto_watch: boolean;
  clip_opts?: ClipOpts;
  is_live?: boolean;
  /** Recording was stopped by hand; it stays off until this stream ends or is resumed. */
  recording_held?: boolean;
  has_preview?: boolean;
  preview_updated_at?: string | null;
  last_checked_at?: string | null;
  created_at: string;
  updated_at?: string;
}

export interface CreateStreamerInput {
  name: string;
  platform: StreamerPlatform;
  channel_url: string;
  auto_watch?: boolean;
  clip_opts?: Record<string, unknown>;
}

export interface UpdateStreamerInput {
  name?: string;
  platform?: StreamerPlatform;
  channel_url?: string;
  auto_watch?: boolean;
  clip_opts?: Record<string, unknown>;
}

export interface ClipDraft {
  id: number;
  streamer_id: number;
  streamer_name?: string;
  session_id?: number | null;
  chunk_id?: number | null;
  draft_number?: number;
  title: string;
  description?: string;
  tags?: string[];
  file_path: string;
  thumbnail_path?: string | null;
  drive_file_id?: string | null;
  drive_url?: string | null;
  tg_chat_id?: string | null;
  tg_message_id?: number | null;
  start_sec?: number | null;
  end_sec?: number | null;
  duration_sec?: number | null;
  score?: number | null;
  status: string;
  scheduled_publish_at?: string | null;
  scheduled_privacy_status?: TargetPrivacy | null;
  created_at: string;
  updated_at?: string;
}

export interface UpdateClipInput {
  title?: string;
  description?: string;
  tags?: string[];
  status?: string;
}

export interface ClipPublication {
  id: number;
  clip_draft_id: number;
  youtube_account_id?: number | null;
  channel_id?: string | null;
  channel_title?: string | null;
  youtube_video_id?: string | null;
  youtube_url?: string | null;
  privacy_status: TargetPrivacy | string;
  status: string;
  published_at?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ApproveClipInput {
  privacy_status?: TargetPrivacy | null;
  publish_now?: boolean;
  youtube_account_ids?: number[];
}

export interface PublishClipInput {
  privacy_status?: TargetPrivacy | null;
  youtube_account_ids?: number[];
}

export interface ClipPublishResponse {
  clip: ClipDraft;
  publications: ClipPublication[];
}

export interface ClipPublicationsResponse {
  clip_id: number;
  items: ClipPublication[];
}

export interface DeleteClipResponse {
  ok: boolean;
  deleted_local?: boolean;
  deleted_drive?: boolean;
}

export interface BulkApproveClipsInput {
  clip_ids: number[];
  privacy_status?: TargetPrivacy | null;
  publish_now?: boolean;
  youtube_account_ids?: number[];
}

export interface BulkClipFailure {
  clip_id: number;
  detail: string;
}

export interface BulkApproveClipsResponse {
  succeeded: ClipPublishResponse[];
  failed: BulkClipFailure[];
}

export interface BulkRejectClipsResponse {
  updated: ClipDraft[];
  skipped: number[];
  missing: number[];
}

export interface BulkDeleteClipsResponse {
  deleted_ids: number[];
  failed: BulkClipFailure[];
  drive_failed_ids?: number[];
}

export interface ScheduleClipInput {
  scheduled_at: string;
  privacy_status?: TargetPrivacy | null;
}

export interface BulkScheduleClipsInput {
  clip_ids: number[];
  scheduled_at: string;
  interval_minutes?: number;
  privacy_status?: TargetPrivacy | null;
}

export interface BulkScheduleClipsResponse {
  updated: ClipDraft[];
  skipped: number[];
  missing: number[];
}

export interface QueueChunkItem {
  id: number;
  session_id?: number;
  streamer_id: number;
  streamer_name?: string | null;
  streamer_slug?: string | null;
  chunk_index: number;
  file_path: string;
  duration_sec?: number | null;
  status: string;
  last_error?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  delete_after?: string | null;
  created_at?: string;
}

export interface ProcessingQueueStatus {
  running_chunk_id: number | null;
  paused: boolean;
  queued_count: number;
  chunks: QueueChunkItem[];
}

export interface Account {
  id: number | string;
  provider?: "youtube" | "google_drive" | "telegram" | string;
  oauth_provider?: string;
  channel_id?: string;
  channel_title?: string;
  name?: string;
  email?: string | null;
  chat_id?: string | null;
  status: string;
  scopes?: string | null;
  token_expires_at?: string | null;
  has_refresh_token?: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface GoogleAuthStartResponse {
  auth_url: string;
  state: string;
  configured: boolean;
}

export interface DeleteAccountResponse {
  ok: boolean;
  id: number;
}

export type TargetPrivacy = "public" | "unlisted" | "private";

export interface StreamerTarget {
  id: number;
  streamer_id: number;
  youtube_account_id: number;
  channel_id: string;
  channel_title: string;
  default_privacy: TargetPrivacy;
  enabled: boolean;
  created_at?: string;
}

export interface StreamerTargetInput {
  youtube_account_id: number;
  default_privacy?: TargetPrivacy;
  enabled?: boolean;
}

export interface StreamerTargetsResponse {
  streamer_id: number;
  items: StreamerTarget[];
}

export interface CheckStreamerResponse {
  streamer_id: number;
  is_live: boolean;
  recording_started: boolean;
  stream: Record<string, unknown> | null;
  checked_at: string;
}

export interface StopRecordingResponse {
  ok: boolean;
  streamer_id: number;
  stopped: boolean;
}

export interface ActiveRecording {
  streamer_id: number;
  streamer_name?: string;
  streamer_slug?: string;
  platform?: StreamerPlatform | string;
  session_id?: number | null;
  external_stream_id?: string | null;
  title?: string | null;
  stream_url?: string | null;
  started_at?: string | null;
  chunk_index?: number | null;
  file_path?: string | null;
  status?: string;
  [key: string]: unknown;
}

export interface RecordingChunk {
  id: number;
  session_id: number;
  streamer_id: number;
  streamer_name?: string | null;
  streamer_slug?: string | null;
  chunk_index: number;
  file_path: string;
  duration_sec?: number | null;
  merged_with_prev: boolean | number;
  merged_from_chunk_id?: number | null;
  status: string;
  started_at?: string | null;
  completed_at?: string | null;
  delete_after?: string | null;
  created_at: string;
}

export interface SystemRecordingsResponse {
  active: ActiveRecording[];
  recent_chunks: RecordingChunk[];
}

export interface RetentionGcResponse {
  deleted_chunk_ids: number[];
  skipped_locked: number;
}

export interface MetricItem {
  publication_id?: number;
  clip_draft_id?: number;
  clip_title?: string;
  title?: string;
  streamer_id?: number;
  streamer_name?: string;
  youtube_video_id?: string | null;
  youtube_url?: string | null;
  privacy_status?: string;
  publication_status?: string;
  status?: string;
  views: number;
  likes: number;
  comments?: number;
  estimated_minutes_watched: number;
  published_at?: string | null;
  captured_at?: string | null;
}

export interface MetricsSummaryResponse {
  total_clips: number;
  total_posted: number;
  total_views: number;
  total_likes: number;
  total_watch_minutes: number;
  items: MetricItem[];
}

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError(0, "Can't reach the backend. Is it running?");
  }

  if (!res.ok) {
    let detail = `Request failed with status ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: unknown; message?: unknown };
      if (typeof body.detail === "string" && body.detail.trim()) {
        detail = body.detail;
      } else if (Array.isArray(body.detail) && body.detail.length > 0) {
        const first = body.detail[0] as { msg?: unknown; loc?: unknown };
        if (typeof first?.msg === "string" && first.msg.trim()) {
          detail = first.msg;
        }
      } else if (typeof body.message === "string" && body.message.trim()) {
        detail = body.message;
      }
    } catch {
      // Ignore non-JSON error bodies
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) {
    return undefined as T;
  }

  return (await res.json()) as T;
}

export function getHealth(): Promise<HealthResponse> {
  return requestJson<HealthResponse>("/api/health");
}

export function getStreamers(): Promise<{ items: Streamer[] }> {
  return requestJson<{ items: Streamer[] }>("/api/streamers");
}

export function createStreamer(payload: CreateStreamerInput): Promise<Streamer> {
  return requestJson<Streamer>("/api/streamers", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateStreamer(
  id: number,
  payload: UpdateStreamerInput
): Promise<Streamer> {
  return requestJson<Streamer>(`/api/streamers/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteStreamer(id: number): Promise<{ ok: boolean; id: number } | void> {
  return requestJson<{ ok: boolean; id: number } | void>(`/api/streamers/${id}`, {
    method: "DELETE",
  });
}

export function getStreamerPreviewUrl(id: number, version?: string | null): string {
  const qs = version ? `?v=${encodeURIComponent(version)}` : "";
  return `${API_BASE}/api/streamers/${id}/preview${qs}`;
}

export function refreshStreamerPreview(id: number): Promise<Streamer> {
  return requestJson<Streamer>(`/api/streamers/${id}/preview/refresh`, {
    method: "POST",
  });
}

export function getStreamerTargets(
  streamerId: number
): Promise<StreamerTargetsResponse> {
  return requestJson<StreamerTargetsResponse>(
    `/api/streamers/${streamerId}/targets`
  );
}

export function updateStreamerTargets(
  streamerId: number,
  targets: StreamerTargetInput[]
): Promise<StreamerTargetsResponse> {
  return requestJson<StreamerTargetsResponse>(
    `/api/streamers/${streamerId}/targets`,
    {
      method: "PUT",
      body: JSON.stringify({ targets }),
    }
  );
}

export function checkStreamerNow(
  streamerId: number
): Promise<CheckStreamerResponse> {
  return requestJson<CheckStreamerResponse>(
    `/api/streamers/${streamerId}/check`,
    {
      method: "POST",
    }
  );
}

export function stopStreamerRecording(
  streamerId: number
): Promise<StopRecordingResponse> {
  return requestJson<StopRecordingResponse>(
    `/api/streamers/${streamerId}/stop`,
    {
      method: "POST",
    }
  );
}

export function getClips(params?: {
  status?: string;
  streamer_id?: number | string;
}): Promise<{ items: ClipDraft[] }> {
  const search = new URLSearchParams();
  if (params?.status) {
    search.set("status", params.status);
  }
  if (
    params?.streamer_id !== undefined &&
    params?.streamer_id !== null &&
    String(params.streamer_id).trim() !== ""
  ) {
    search.set("streamer_id", String(params.streamer_id));
  }
  const qs = search.toString();
  return requestJson<{ items: ClipDraft[] }>(
    qs ? `/api/clips?${qs}` : "/api/clips"
  );
}

export function getClipVideoUrl(id: number): string {
  return `${API_BASE}/api/clips/${id}/video`;
}

export function getClipThumbnailUrl(id: number): string {
  return `${API_BASE}/api/clips/${id}/thumbnail`;
}

export function updateClip(
  id: number,
  payload: UpdateClipInput
): Promise<ClipDraft> {
  return requestJson<ClipDraft>(`/api/clips/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function uploadClipToDrive(id: number): Promise<ClipDraft> {
  return requestJson<ClipDraft>(`/api/clips/${id}/upload-drive`, {
    method: "POST",
  });
}

export function approveClip(
  id: number,
  payload?: ApproveClipInput
): Promise<ClipPublishResponse> {
  return requestJson<ClipPublishResponse>(`/api/clips/${id}/approve`, {
    method: "POST",
    body: JSON.stringify(payload ?? {}),
  });
}

export function rejectClip(id: number): Promise<ClipDraft> {
  return requestJson<ClipDraft>(`/api/clips/${id}/reject`, {
    method: "POST",
  });
}

export function publishClip(
  id: number,
  payload?: PublishClipInput
): Promise<ClipPublishResponse> {
  return requestJson<ClipPublishResponse>(`/api/clips/${id}/publish`, {
    method: "POST",
    body: JSON.stringify(payload ?? {}),
  });
}

export function getClipPublications(
  id: number
): Promise<ClipPublicationsResponse> {
  return requestJson<ClipPublicationsResponse>(
    `/api/clips/${id}/publications`
  );
}

export function deleteClip(
  id: number,
  deleteDrive = false
): Promise<DeleteClipResponse> {
  const qs = new URLSearchParams({ delete_drive: String(deleteDrive) });
  return requestJson<DeleteClipResponse>(`/api/clips/${id}?${qs.toString()}`, {
    method: "DELETE",
  });
}

export function bulkApproveClips(
  payload: BulkApproveClipsInput
): Promise<BulkApproveClipsResponse> {
  return requestJson<BulkApproveClipsResponse>("/api/clips/bulk/approve", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function bulkRejectClips(
  clipIds: number[]
): Promise<BulkRejectClipsResponse> {
  return requestJson<BulkRejectClipsResponse>("/api/clips/bulk/reject", {
    method: "POST",
    body: JSON.stringify({ clip_ids: clipIds }),
  });
}

export function bulkDeleteClips(
  clipIds: number[],
  deleteDrive = false
): Promise<BulkDeleteClipsResponse> {
  return requestJson<BulkDeleteClipsResponse>("/api/clips/bulk/delete", {
    method: "POST",
    body: JSON.stringify({ clip_ids: clipIds, delete_drive: deleteDrive }),
  });
}

export function scheduleClip(
  id: number,
  payload: ScheduleClipInput
): Promise<ClipDraft> {
  return requestJson<ClipDraft>(`/api/clips/${id}/schedule`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function cancelClipSchedule(id: number): Promise<ClipDraft> {
  return requestJson<ClipDraft>(`/api/clips/${id}/schedule`, {
    method: "DELETE",
  });
}

export function bulkScheduleClips(
  payload: BulkScheduleClipsInput
): Promise<BulkScheduleClipsResponse> {
  return requestJson<BulkScheduleClipsResponse>("/api/clips/bulk/schedule", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getProcessingQueue(): Promise<ProcessingQueueStatus> {
  return requestJson<ProcessingQueueStatus>("/api/system/queue");
}

export function pauseProcessingQueue(): Promise<{ paused: boolean }> {
  return requestJson<{ paused: boolean }>("/api/system/queue/pause", {
    method: "POST",
  });
}

export function resumeProcessingQueue(): Promise<{ paused: boolean }> {
  return requestJson<{ paused: boolean }>("/api/system/queue/resume", {
    method: "POST",
  });
}

export function retryChunkProcessing(
  chunkId: number
): Promise<{ ok: boolean; chunk_id: number; status: string }> {
  return requestJson<{ ok: boolean; chunk_id: number; status: string }>(
    `/api/system/queue/process/${chunkId}`,
    {
      method: "POST",
    }
  );
}

export function getAccounts(): Promise<{ items: Account[] }> {
  return requestJson<{ items: Account[] }>("/api/accounts");
}

export function startGoogleAuth(): Promise<GoogleAuthStartResponse> {
  return requestJson<GoogleAuthStartResponse>("/api/auth/google/start");
}

export function startGoogleDriveAuth(): Promise<GoogleAuthStartResponse> {
  return requestJson<GoogleAuthStartResponse>("/api/auth/google-drive/start");
}

export function deleteAccount(id: number): Promise<DeleteAccountResponse> {
  return requestJson<DeleteAccountResponse>(`/api/accounts/${id}`, {
    method: "DELETE",
  });
}

export function getSystemRecordings(): Promise<SystemRecordingsResponse> {
  return requestJson<SystemRecordingsResponse>("/api/system/recordings");
}

export function runRetentionGc(): Promise<RetentionGcResponse> {
  return requestJson<RetentionGcResponse>("/api/system/gc/run", {
    method: "POST",
  });
}

export function getMetricsSummary(): Promise<MetricsSummaryResponse> {
  return requestJson<MetricsSummaryResponse>("/api/metrics/summary");
}

export type MetricsWindow = "7" | "30" | "all";

export interface DashboardFilterStreamer {
  id: number;
  name: string;
}

export interface DashboardFilterAccount {
  id: number;
  channel_id: string;
  channel_title: string;
}

export interface DashboardFilters {
  window: "7d" | "30d" | "all";
  streamer_id: number | null;
  youtube_account_id: number | null;
  streamers: DashboardFilterStreamer[];
  accounts: DashboardFilterAccount[];
  last_synced_at: string | null;
}

export interface DashboardKpis {
  total_views: number;
  total_engaged_views: number;
  engagement_rate: number;
  watch_hours: number;
  total_watch_minutes: number;
  avg_view_percentage: number;
  avg_view_duration_sec: number;
  net_subscribers: number;
  subscribers_gained: number;
  subscribers_lost: number;
  total_likes: number;
  total_comments: number;
  total_shares: number;
  total_shorts_feed_views: number;
  shorts_feed_share: number;
  posted_count: number;
  drafts_generated: number;
  approval_rate: number;
}

export interface DashboardClipItem {
  publication_id: number;
  clip_draft_id: number;
  title: string;
  streamer_id: number;
  streamer_name: string;
  youtube_account_id: number;
  channel_title: string;
  youtube_video_id: string | null;
  youtube_url: string | null;
  privacy_status: string;
  published_at: string | null;
  duration_sec: number;
  score: number;
  views: number;
  views_delta_24h: number;
  engaged_views: number;
  likes: number;
  comments: number;
  shares: number;
  engagement_rate: number;
  estimated_minutes_watched: number;
  watch_hours: number;
  avg_view_duration_sec: number;
  avg_view_percentage: number;
  subscribers_gained: number;
  subscribers_lost: number;
  net_subscribers: number;
  shorts_feed_views: number;
  analytics_pending: boolean;
  analytics_synced_at: string | null;
  captured_at: string | null;
  has_local_video: boolean;
}

export interface DashboardTimeseriesPoint {
  date: string;
  views: number;
  engaged_views: number;
  likes: number;
  watch_hours: number;
  net_subscribers: number;
}

export interface DashboardFunnelStage {
  stage: string;
  key: string;
  count: number;
}

export interface DashboardCohortPoint {
  bucket: string;
  clip_count: number;
  total_views: number;
  avg_views: number;
  avg_view_percentage: number;
}

export interface DashboardStreamerBreakdown {
  streamer_id: number;
  streamer_name: string;
  clips_posted: number;
  views: number;
  engaged_views: number;
  watch_hours: number;
  avg_view_percentage: number;
  net_subscribers: number;
}

export interface MetricsDashboardResponse {
  filters: DashboardFilters;
  kpis: DashboardKpis;
  best_clip_this_week: DashboardClipItem | null;
  timeseries: DashboardTimeseriesPoint[];
  funnel: {
    stages: DashboardFunnelStage[];
    outcomes: {
      posted_publications: number;
      kept_local: number;
      archived: number;
      failed: number;
    };
  };
  cohorts: DashboardCohortPoint[];
  streamers: DashboardStreamerBreakdown[];
  clips: DashboardClipItem[];
}

export interface MetricsSyncResponse {
  synced_publications: number;
  synced_accounts: number;
  analytics_synced: number;
  captured_at: string;
  errors: string[];
}

export function getMetricsDashboard(params?: {
  days?: MetricsWindow;
  streamerId?: number | null;
  youtubeAccountId?: number | null;
}): Promise<MetricsDashboardResponse> {
  const qs = new URLSearchParams();
  if (params?.days) qs.set("days", params.days);
  if (params?.streamerId != null) qs.set("streamer_id", String(params.streamerId));
  if (params?.youtubeAccountId != null) {
    qs.set("youtube_account_id", String(params.youtubeAccountId));
  }
  const query = qs.toString();
  return requestJson<MetricsDashboardResponse>(
    query ? `/api/metrics/dashboard?${query}` : "/api/metrics/dashboard"
  );
}

export function syncMetricsNow(): Promise<MetricsSyncResponse> {
  return requestJson<MetricsSyncResponse>("/api/metrics/sync", {
    method: "POST",
  });
}


export interface ResumeRecordingResponse extends CheckStreamerResponse {
  held: boolean;
}

export function resumeStreamerRecording(id: number): Promise<ResumeRecordingResponse> {
  return requestJson<ResumeRecordingResponse>(`/api/streamers/${id}/resume`, { method: "POST" });
}

export interface RecordingSession {
  id: number;
  streamer_id: number;
  streamer_name: string;
  platform: string;
  title: string | null;
  started_at: string;
  ended_at: string | null;
  status: "recording" | "completed" | "error" | "interrupted" | "stopped" | string;
  end_reason: string | null;
  last_error: string | null;
  log_path: string | null;
  chunks: number;
}

export function getRecordingSessions(limit = 25): Promise<{ items: RecordingSession[] }> {
  return requestJson<{ items: RecordingSession[] }>(`/api/system/sessions?limit=${limit}`);
}

export interface LearningSummary {
  ratings: number;
  fitted: boolean;
  alpha?: number;
  weights: Record<string, number>;
  defaults: Record<string, number>;
  insight: string[];
  message: string;
  min_ratings: number;
  drafts: { rated: number; liked: number; rejected: number; waiting: number };
}

export function getLearningSummary(): Promise<LearningSummary> {
  return requestJson<LearningSummary>("/api/system/learning");
}

export function syncLearning(): Promise<{ recorded: number; summary: LearningSummary }> {
  return requestJson<{ recorded: number; summary: LearningSummary }>("/api/system/learning/sync", {
    method: "POST",
  });
}

// ============================================================================
// VOD Studio
// ============================================================================

export interface StudioTimelinePoint {
  t: number;
  hype: number;
  audio: number;
  chat: number;
  laugh: number;
}

export interface StudioTimelineCurve {
  duration: number;
  step_sec: number;
  points: StudioTimelinePoint[];
}

export interface StudioCandidateReason {
  code: string;
  value?: number | string;
  t?: number;
}

export interface StudioCandidate {
  id: string;
  start: number;
  end: number;
  peak_sec?: number | null;
  duration?: number;
  score?: number | null;
  llm?: number | null;
  visual_action?: number | null;
  visual_summary?: string;
  category?: string;
  title: string;
  hook?: string;
  transcript?: string;
  reasons?: StudioCandidateReason[];
  boundary_reason?: string;
  features?: Record<string, number | null>;
  selected: boolean;
  orig_start?: number;
  orig_end?: number;
}

export type StudioProfile = "balanced" | "action" | "dialogue";

export interface StudioOptions {
  clip_count?: number;
  min_len?: number;
  max_len?: number;
  min_score?: number;
  profile?: StudioProfile;
  focus_brief?: string;
  cam_auto?: boolean;
  cam_box?: CamBox | null;
  layout?: string;
  fallback_layout?: string;
  caption_style?: string;
  language?: string | null;
  last_exported_clip_ids?: number[];
}

export type StudioProjectStatus =
  | "downloading"
  | "ready"
  | "analyzing"
  | "analyzed"
  | "exporting"
  | "exported"
  | "error"
  | string;

export type StudioVodDownloader = "auto" | "streamlink" | "yt-dlp";
export type StudioVodQuality = "1080p" | "720p";

export interface StudioProject {
  id: number;
  title: string;
  source_type: "upload" | "chunk";
  source_path: string;
  chunk_id?: number | null;
  streamer_id?: number | null;
  streamer_name?: string | null;
  duration_sec?: number | null;
  has_preview?: boolean;
  has_chat?: boolean;
  options: StudioOptions;
  status: StudioProjectStatus;
  progress: number;
  stage: string;
  timeline_curve?: StudioTimelineCurve | null;
  candidates: StudioCandidate[];
  facecam?: Record<string, unknown> | null;
  last_error?: string | null;
  created_at: string;
  updated_at: string;
}

export interface StudioUploadInitResponse {
  upload_id: string;
  chunk_size: number;
}

export interface StudioUploadChunkResponse {
  ok: boolean;
  index: number;
  bytes_written: number;
}

export interface StudioUploadCompleteResponse {
  upload_id: string;
  file_path: string;
  filename: string;
  size_bytes: number;
}

export interface CreateStudioProjectInput {
  title?: string;
  source_type: "upload" | "chunk";
  upload_id?: string;
  chunk_id?: number;
  streamer_id?: number | null;
  chat_upload_id?: string;
  options?: StudioOptions;
}

export interface CreateStudioProjectFromUrlInput {
  url: string;
  title?: string;
  streamer_id?: number | null;
  downloader?: StudioVodDownloader;
  quality?: StudioVodQuality;
  start_time?: string | number | null;
  end_time?: string | number | null;
  fetch_chat?: boolean;
  auto_analyze?: boolean;
  options?: StudioOptions;
}

export function initStudioUpload(payload: {
  filename: string;
  total_size: number;
  kind: "video" | "chat";
}): Promise<StudioUploadInitResponse> {
  return requestJson<StudioUploadInitResponse>("/api/studio/uploads/init", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function uploadStudioChunk(
  uploadId: string,
  index: number,
  chunk: Blob
): Promise<StudioUploadChunkResponse> {
  const url = `${API_BASE}/api/studio/uploads/${encodeURIComponent(uploadId)}/chunk?index=${index}`;
  let res: Response;
  try {
    res = await fetch(url, {
      method: "PUT",
      cache: "no-store",
      headers: {
        "Content-Type": "application/octet-stream",
      },
      body: chunk,
    });
  } catch {
    throw new ApiError(0, "Can't reach the backend during chunk upload.");
  }
  if (!res.ok) {
    let detail = `Chunk ${index} failed with status ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: unknown; message?: unknown };
      if (typeof body.detail === "string" && body.detail.trim()) {
        detail = body.detail;
      } else if (typeof body.message === "string" && body.message.trim()) {
        detail = body.message;
      }
    } catch {
      // Ignore non-JSON error bodies
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as StudioUploadChunkResponse;
}

export function completeStudioUpload(
  uploadId: string
): Promise<StudioUploadCompleteResponse> {
  return requestJson<StudioUploadCompleteResponse>(
    `/api/studio/uploads/${encodeURIComponent(uploadId)}/complete`,
    {
      method: "POST",
    }
  );
}

export async function uploadStudioFileChunked(
  file: File,
  kind: "video" | "chat",
  onProgress?: (pct: number) => void
): Promise<StudioUploadCompleteResponse> {
  if (file.size <= 0) {
    throw new ApiError(400, `Selected ${kind} file is empty.`);
  }
  onProgress?.(0);
  const initRes = await initStudioUpload({
    filename: file.name,
    total_size: file.size,
    kind,
  });
  const chunkSize = initRes.chunk_size > 0 ? initRes.chunk_size : 16 * 1024 * 1024;
  const totalChunks = Math.ceil(file.size / chunkSize);
  let bytesSent = 0;

  for (let i = 0; i < totalChunks; i += 1) {
    const start = i * chunkSize;
    const end = Math.min(file.size, start + chunkSize);
    const slice = file.slice(start, end);
    await uploadStudioChunk(initRes.upload_id, i, slice);
    bytesSent = end;
    const pct = Math.min(99, Math.round((bytesSent / file.size) * 100));
    onProgress?.(pct);
  }

  const completed = await completeStudioUpload(initRes.upload_id);
  onProgress?.(100);
  return completed;
}

export function createStudioProject(
  payload: CreateStudioProjectInput
): Promise<StudioProject> {
  return requestJson<StudioProject>("/api/studio/projects", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function createStudioProjectFromUrl(
  payload: CreateStudioProjectFromUrlInput
): Promise<StudioProject> {
  return requestJson<StudioProject>("/api/studio/projects/from-url", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getStudioProjects(): Promise<{ items: StudioProject[] }> {
  return requestJson<{ items: StudioProject[] }>("/api/studio/projects");
}

export function getStudioProject(id: number): Promise<StudioProject> {
  return requestJson<StudioProject>(`/api/studio/projects/${id}`);
}

export function getStudioVideoUrl(id: number): string {
  return `${API_BASE}/api/studio/projects/${id}/video`;
}

export function getStudioPreviewUrl(
  id: number,
  version?: string | number | null
): string {
  const qs =
    version !== undefined && version !== null && String(version) !== ""
      ? `?v=${encodeURIComponent(String(version))}`
      : "";
  return `${API_BASE}/api/studio/projects/${id}/preview${qs}`;
}

export function deleteStudioProject(
  id: number
): Promise<{ ok: boolean; id: number }> {
  return requestJson<{ ok: boolean; id: number }>(`/api/studio/projects/${id}`, {
    method: "DELETE",
  });
}

export function analyzeStudioProject(
  id: number,
  options?: StudioOptions
): Promise<StudioProject> {
  return requestJson<StudioProject>(`/api/studio/projects/${id}/analyze`, {
    method: "POST",
    body: JSON.stringify(options ? { options } : {}),
  });
}

export function saveStudioCandidates(
  id: number,
  candidates: StudioCandidate[],
  options?: StudioOptions
): Promise<StudioProject> {
  return requestJson<StudioProject>(`/api/studio/projects/${id}/candidates`, {
    method: "PATCH",
    body: JSON.stringify(options ? { candidates, options } : { candidates }),
  });
}

export function exportStudioProject(
  id: number,
  payload?: { candidate_ids?: string[]; options?: StudioOptions }
): Promise<StudioProject> {
  return requestJson<StudioProject>(`/api/studio/projects/${id}/export`, {
    method: "POST",
    body: JSON.stringify(payload ?? {}),
  });
}
