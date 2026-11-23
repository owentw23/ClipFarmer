"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowUp, ExternalLink, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { BestClipCard } from "@/components/metrics/BestClipCard";
import { LiveNowCard } from "@/components/metrics/LiveNowCard";
import {
  DurationCohortChartCard,
  GrowthChartCard,
  PipelineBreakdownCard,
} from "@/components/metrics/MetricsCharts";
import {
  DotTabs,
  EmptyState,
  Notice,
  PageHeader,
  Panel,
  Tally,
  rise,
} from "@/components/page-kit";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  DashboardClipItem,
  MetricsDashboardResponse,
  MetricsWindow,
  getMetricsDashboard,
  syncMetricsNow,
} from "@/lib/api";

type SortKey =
  | "views"
  | "engaged_views"
  | "avg_view_percentage"
  | "watch_hours"
  | "likes"
  | "net_subscribers"
  | "published_at";

const WINDOWS: Array<{ value: MetricsWindow; label: string }> = [
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "all", label: "All time" },
];

function formatSyncedAt(iso?: string | null): string {
  if (!iso) return "Metrics never synced";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return `Synced ${parsed.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}

function formatDate(iso?: string | null): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const signed = (n: number) => `${n >= 0 ? "+" : ""}${Math.round(n).toLocaleString()}`;

export default function DashboardPage() {
  const [windowVal, setWindowVal] = useState<MetricsWindow>("7");
  const [streamerId, setStreamerId] = useState<string>("all");
  const [accountId, setAccountId] = useState<string>("all");
  const [sortKey, setSortKey] = useState<SortKey>("views");
  const [sortDesc, setSortDesc] = useState<boolean>(true);

  const [data, setData] = useState<MetricsDashboardResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [syncing, setSyncing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const fetchDashboard = useCallback(async () => {
    try {
      const res = await getMetricsDashboard({
        days: windowVal,
        streamerId: streamerId === "all" ? null : Number(streamerId),
        youtubeAccountId: accountId === "all" ? null : Number(accountId),
      });
      setData(res);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load metrics");
    } finally {
      setLoading(false);
    }
  }, [windowVal, streamerId, accountId]);

  useEffect(() => {
    let active = true;
    getMetricsDashboard({
      days: windowVal,
      streamerId: streamerId === "all" ? null : Number(streamerId),
      youtubeAccountId: accountId === "all" ? null : Number(accountId),
    })
      .then((res) => {
        if (!active) return;
        setData(res);
        setError(null);
      })
      .catch((err) => {
        if (!active) return;
        setError(err instanceof Error ? err.message : "Failed to load metrics");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [windowVal, streamerId, accountId]);

  const handleSyncNow = async () => {
    setSyncing(true);
    try {
      const res = await syncMetricsNow();
      toast.success(`Synced ${res.synced_publications} clips, ${res.analytics_synced} with analytics`);
      await fetchDashboard();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Metrics sync failed");
    } finally {
      setSyncing(false);
    }
  };

  const handleSort = (nextKey: SortKey) => {
    if (sortKey === nextKey) {
      setSortDesc((prev) => !prev);
    } else {
      setSortKey(nextKey);
      setSortDesc(true);
    }
  };

  const sortedClips = useMemo(() => {
    const items = [...(data?.clips ?? [])];
    items.sort((a: DashboardClipItem, b: DashboardClipItem) => {
      if (sortKey === "published_at") {
        const av = a.published_at ?? "";
        const bv = b.published_at ?? "";
        return sortDesc ? bv.localeCompare(av) : av.localeCompare(bv);
      }
      const av = Number(a[sortKey] ?? 0);
      const bv = Number(b[sortKey] ?? 0);
      return sortDesc ? bv - av : av - bv;
    });
    return items;
  }, [data?.clips, sortKey, sortDesc]);

  const kpis = data?.kpis;
  const streamerOptions = data?.filters.streamers ?? [];
  const accountOptions = data?.filters.accounts ?? [];
  const firstLoad = loading && !data;

  const tally = [
    {
      label: "Views",
      value: kpis?.total_views ?? 0,
      note: `${(kpis?.total_engaged_views ?? 0).toLocaleString()} engaged`,
    },
    {
      label: "Watch hours",
      value: kpis?.watch_hours ?? 0,
      format: (n: number) => `${n.toFixed(1)}h`,
      note: `${(kpis?.avg_view_duration_sec ?? 0).toFixed(1)}s a view`,
    },
    {
      label: "Average view",
      value: kpis?.avg_view_percentage ?? 0,
      format: (n: number) => `${n.toFixed(1)}%`,
      note: `${(kpis?.shorts_feed_share ?? 0).toFixed(1)}% Shorts feed`,
    },
    {
      label: "Engagement",
      value: kpis?.engagement_rate ?? 0,
      format: (n: number) => `${n.toFixed(2)}%`,
      note: `${((kpis?.total_likes ?? 0) + (kpis?.total_comments ?? 0) + (kpis?.total_shares ?? 0)).toLocaleString()} reactions`,
    },
    {
      label: "Net subscribers",
      value: kpis?.net_subscribers ?? 0,
      format: signed,
      note: `${kpis?.subscribers_gained ?? 0} gained, ${kpis?.subscribers_lost ?? 0} lost`,
    },
    {
      label: "Shorts posted",
      value: kpis?.posted_count ?? 0,
      note: `${(kpis?.approval_rate ?? 0).toFixed(0)}% approved`,
    },
  ];

  const selectedStreamerLabel =
    streamerId === "all"
      ? "All streamers"
      : streamerOptions.find((s) => String(s.id) === streamerId)?.name ?? "Streamer";
  const selectedAccountLabel =
    accountId === "all"
      ? "All channels"
      : accountOptions.find((a) => String(a.id) === accountId)?.channel_title ?? "Channel";

  const sortHead = (key: SortKey, label: string, className = "text-right") => (
    <TableHead className={className} aria-sort={sortKey === key ? (sortDesc ? "descending" : "ascending") : "none"}>
      <button
        type="button"
        onClick={() => handleSort(key)}
        className={`press inline-flex items-center gap-(--space-2xs) rounded-full hover:text-foreground ${
          sortKey === key ? "text-foreground" : ""
        }`}
      >
        {label}
        {sortKey === key &&
          (sortDesc ? <ArrowDown className="size-3" aria-hidden="true" /> : <ArrowUp className="size-3" aria-hidden="true" />)}
      </button>
    </TableHead>
  );

  return (
    <div className="flex flex-col gap-(--space-lg)">
      <PageHeader
        title="Dashboard"
        meta={formatSyncedAt(data?.filters.last_synced_at)}
        actions={
          <div className="glass flex flex-wrap items-center gap-(--space-xs) rounded-full p-(--space-2xs)">
            <DotTabs label="Time window" value={windowVal} onChange={setWindowVal} options={WINDOWS} />
            <Select value={streamerId} onValueChange={(val) => setStreamerId(val ?? "all")}>
              <SelectTrigger className="min-w-[136px] rounded-full border-(--rule-glass)" aria-label="Streamer">
                <SelectValue>{selectedStreamerLabel}</SelectValue>
              </SelectTrigger>
              <SelectContent align="end">
                <SelectItem value="all">All streamers</SelectItem>
                {streamerOptions.map((s) => (
                  <SelectItem key={s.id} value={String(s.id)}>
                    {s.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={accountId} onValueChange={(val) => setAccountId(val ?? "all")}>
              <SelectTrigger className="min-w-[136px] rounded-full border-(--rule-glass)" aria-label="Channel">
                <SelectValue>{selectedAccountLabel}</SelectValue>
              </SelectTrigger>
              <SelectContent align="end">
                <SelectItem value="all">All channels</SelectItem>
                {accountOptions.map((a) => (
                  <SelectItem key={a.id} value={String(a.id)}>
                    {a.channel_title}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button onClick={() => void handleSyncNow()} disabled={syncing} aria-busy={syncing}>
              <RefreshCw className={`size-3.5 ${syncing ? "animate-spin" : ""}`} />
              {syncing ? "Syncing" : "Sync metrics"}
            </Button>
          </div>
        }
      />

      {error && <Notice tone="error">{error}</Notice>}

      <div className="grid grid-cols-[minmax(0,1fr)] gap-(--space-lg) lg:grid-cols-12">
        <div className="lg:col-span-5">
          <LiveNowCard index={1} />
        </div>
        <Panel
          title={WINDOWS.find((w) => w.value === windowVal)?.label ?? "This window"}
          index={2}
          className="lg:col-span-7"
        >
          <Tally items={tally} loading={firstLoad} columns={3} />
        </Panel>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)] gap-(--space-lg) lg:grid-cols-12">
        <div className="rise lg:col-span-5" style={rise(3)}>
          <BestClipCard clip={data?.best_clip_this_week ?? null} isLoading={firstLoad} />
        </div>
        <div className="rise lg:col-span-7" style={rise(4)}>
          <GrowthChartCard data={data?.timeseries ?? []} isLoading={firstLoad} />
        </div>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)] gap-(--space-lg) lg:grid-cols-2">
        <div className="rise" style={rise(5)}>
          <DurationCohortChartCard cohorts={data?.cohorts ?? []} isLoading={firstLoad} />
        </div>
        <div className="rise" style={rise(6)}>
          <PipelineBreakdownCard
            stages={data?.funnel.stages ?? []}
            outcomes={data?.funnel.outcomes ?? null}
            isLoading={firstLoad}
          />
        </div>
      </div>

      {(data?.streamers.length ?? 0) > 0 && (
        <Panel title="By streamer" count={data?.streamers.length} flush index={7}>
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="pl-(--space-lg)">Streamer</TableHead>
                <TableHead className="text-right">Clips</TableHead>
                <TableHead className="text-right">Views</TableHead>
                <TableHead className="text-right">Engaged</TableHead>
                <TableHead className="text-right">Average view</TableHead>
                <TableHead className="text-right">Watch hours</TableHead>
                <TableHead className="pr-(--space-lg) text-right">Net subscribers</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="tabular-nums">
              {data?.streamers.map((row, i) => (
                <TableRow key={row.streamer_id} className="rise-row" style={rise(i)}>
                  <TableCell className="pl-(--space-lg) font-medium">{row.streamer_name}</TableCell>
                  <TableCell className="text-right">{row.clips_posted}</TableCell>
                  <TableCell className="text-right">{row.views.toLocaleString()}</TableCell>
                  <TableCell className="text-right">{row.engaged_views.toLocaleString()}</TableCell>
                  <TableCell className="text-right">{row.avg_view_percentage.toFixed(1)}%</TableCell>
                  <TableCell className="text-right">{row.watch_hours.toFixed(1)}h</TableCell>
                  <TableCell className="pr-(--space-lg) text-right">{signed(row.net_subscribers)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Panel>
      )}

      <Panel title="Published Shorts" count={sortedClips.length} flush index={8}>
        {sortedClips.length === 0 ? (
          <EmptyState>Nothing published in this window. Approve a draft in Clips or from Telegram.</EmptyState>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="pl-(--space-lg)">Clip</TableHead>
                <TableHead>Streamer</TableHead>
                <TableHead>Channel</TableHead>
                {sortHead("views", "Views")}
                <TableHead className="text-right">Last 24h</TableHead>
                {sortHead("avg_view_percentage", "Average view")}
                {sortHead("watch_hours", "Watch")}
                {sortHead("likes", "Likes")}
                {sortHead("net_subscribers", "Subscribers")}
                {sortHead("published_at", "Posted", "pr-(--space-lg) text-right")}
              </TableRow>
            </TableHeader>
            <TableBody className="tabular-nums">
              {sortedClips.map((item, i) => (
                <TableRow key={item.publication_id} className="rise-row" style={rise(i)}>
                  <TableCell className="max-w-[260px] pl-(--space-lg)">
                    <div className="flex items-center gap-(--space-xs)">
                      <span className="truncate font-medium text-foreground">{item.title}</span>
                      {item.youtube_url && (
                        <a
                          href={item.youtube_url}
                          target="_blank"
                          rel="noreferrer"
                          aria-label={`Open ${item.title} on YouTube`}
                          className="press shrink-0 rounded-full text-(--color-ink-3) hover:text-foreground"
                        >
                          <ExternalLink className="size-3.5" />
                        </a>
                      )}
                    </div>
                  </TableCell>
                  <TableCell className="text-(--color-ink-2)">{item.streamer_name}</TableCell>
                  <TableCell className="text-(--color-ink-2)">{item.channel_title}</TableCell>
                  <TableCell className="text-right font-medium">{item.views.toLocaleString()}</TableCell>
                  <TableCell className="text-right text-(--color-ink-3)">
                    {item.views_delta_24h > 0 ? `+${item.views_delta_24h.toLocaleString()}` : "none"}
                  </TableCell>
                  <TableCell className="text-right">
                    {item.analytics_pending ? <span className="text-(--color-owed)">pending</span> : `${item.avg_view_percentage.toFixed(1)}%`}
                  </TableCell>
                  <TableCell className="text-right">
                    {item.analytics_pending ? <span className="text-(--color-owed)">pending</span> : `${item.watch_hours.toFixed(1)}h`}
                  </TableCell>
                  <TableCell className="text-right">{item.likes.toLocaleString()}</TableCell>
                  <TableCell className="text-right">
                    {item.analytics_pending ? <span className="text-(--color-owed)">pending</span> : signed(item.net_subscribers)}
                  </TableCell>
                  <TableCell className="pr-(--space-lg) text-right text-(--color-ink-3)">
                    {formatDate(item.published_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Panel>
    </div>
  );
}
