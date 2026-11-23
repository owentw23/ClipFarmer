"use client";

import { useState } from "react";
import { ExternalLink, Film } from "lucide-react";

import { DotTabs, EmptyState, Ticker } from "@/components/page-kit";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { DashboardClipItem, getClipThumbnailUrl, getClipVideoUrl } from "@/lib/api";

interface BestClipCardProps {
  clip: DashboardClipItem | null;
  isLoading?: boolean;
}

function formatPublishedDate(iso?: string | null): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

type Source = "youtube" | "local";

export function BestClipCard({ clip, isLoading = false }: BestClipCardProps) {
  const [sourceMode, setSourceMode] = useState<Source>("youtube");

  if (isLoading) {
    return (
      <Card className="h-full">
        <CardHeader>
          <CardTitle>Best clip this week</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-(--space-lg) sm:grid-cols-[176px_minmax(0,1fr)]">
          <Skeleton className="mx-auto aspect-[9/16] w-[176px] rounded-(--radius-inner)" />
          <div className="flex flex-col gap-(--space-sm)">
            <Skeleton className="h-5 w-4/5" />
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="mt-(--space-md) h-32 w-full rounded-(--radius-inner)" />
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!clip) {
    return (
      <Card className="h-full">
        <CardHeader>
          <CardTitle>Best clip this week</CardTitle>
        </CardHeader>
        <CardContent className="flex min-h-[280px] items-center justify-center">
          <EmptyState icon={<Film />}>No Shorts published this week</EmptyState>
        </CardContent>
      </Card>
    );
  }

  const hasYoutube = Boolean(clip.youtube_video_id);
  const hasLocal = clip.has_local_video;
  const activeMode: Source | null =
    sourceMode === "local" && hasLocal ? "local" : hasYoutube ? "youtube" : hasLocal ? "local" : null;

  const figures: Array<[string, number | null, (n: number) => string]> = [
    ["Views", clip.views, (n) => Math.round(n).toLocaleString()],
    ["Average view", clip.analytics_pending ? null : clip.avg_view_percentage, (n) => `${n.toFixed(1)}%`],
    ["Watch hours", clip.analytics_pending ? null : clip.watch_hours, (n) => `${n.toFixed(1)}h`],
    ["Likes", clip.likes, (n) => Math.round(n).toLocaleString()],
  ];

  return (
    <Card className="h-full">
      <CardHeader className="items-center">
        <CardTitle>Best clip this week</CardTitle>
        {hasYoutube && hasLocal && (
          <CardAction>
            <DotTabs
              label="Video source"
              value={activeMode ?? "youtube"}
              onChange={setSourceMode}
              options={[
                { value: "youtube", label: "YouTube" },
                { value: "local", label: "Local file" },
              ]}
            />
          </CardAction>
        )}
      </CardHeader>

      <CardContent className="grid gap-(--space-lg) sm:grid-cols-[176px_minmax(0,1fr)]">
        <div className="mx-auto aspect-[9/16] w-[176px] overflow-hidden rounded-(--radius-inner) bg-(--color-shade) shadow-[inset_0_0_0_1px_var(--rule-glass)]">
          {activeMode === "youtube" && clip.youtube_video_id ? (
            <iframe
              key={clip.youtube_video_id}
              src={`https://www.youtube-nocookie.com/embed/${encodeURIComponent(clip.youtube_video_id)}?rel=0&modestbranding=1&playsinline=1`}
              title={clip.title}
              allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
              allowFullScreen
              className="fade-in h-full w-full border-0"
            />
          ) : activeMode === "local" ? (
            <video
              key={clip.clip_draft_id}
              controls
              playsInline
              preload="metadata"
              src={getClipVideoUrl(clip.clip_draft_id)}
              poster={getClipThumbnailUrl(clip.clip_draft_id)}
              className="fade-in h-full w-full object-cover"
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center">
              <EmptyState icon={<Film />}>Video unavailable</EmptyState>
            </div>
          )}
        </div>

        <div className="flex min-w-0 flex-col justify-between gap-(--space-md)">
          <div className="flex flex-col gap-(--space-2xs)">
            <div className="flex items-start justify-between gap-(--space-xs)">
              <h3 className="line-clamp-2 text-base leading-snug text-foreground">{clip.title}</h3>
              {clip.youtube_url && (
                <a
                  href={clip.youtube_url}
                  target="_blank"
                  rel="noreferrer"
                  aria-label="Open clip on YouTube"
                  className="press shrink-0 rounded-full p-(--space-2xs) text-(--color-ink-3) hover:bg-(--row-hover) hover:text-foreground"
                >
                  <ExternalLink className="size-3.5" />
                </a>
              )}
            </div>
            <p className="text-sm text-(--color-ink-3)">
              {clip.streamer_name} on {clip.channel_title}
              <br />
              Posted {formatPublishedDate(clip.published_at)}
            </p>
          </div>

          <dl className="flex flex-col">
            {figures.map(([label, value, format]) => (
              <div
                key={label}
                className="flex items-baseline justify-between gap-(--space-sm) border-t border-(--rule-glass) py-(--space-xs) text-sm tabular-nums"
              >
                <dt className="text-(--color-ink-3)">{label}</dt>
                <dd className="m-0 text-base font-medium text-foreground">
                  {value === null ? <span className="text-(--color-owed)">pending</span> : <Ticker value={value} format={format} />}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      </CardContent>
    </Card>
  );
}
