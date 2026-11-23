"use client";

import { useState } from "react";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";

import { DotTabs, EmptyState } from "@/components/page-kit";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartConfig,
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { DashboardCohortPoint, DashboardFunnelStage, DashboardTimeseriesPoint } from "@/lib/api";

// One series argues in the signal colour; its companion is the lighter step on the same hue.
const growthChartConfig = {
  views: { label: "Views", color: "var(--chart-1)" },
  engaged_views: { label: "Engaged views", color: "var(--chart-2)" },
  watch_hours: { label: "Watch hours", color: "var(--chart-1)" },
  net_subscribers: { label: "Net subscribers", color: "var(--chart-1)" },
} satisfies ChartConfig;

const cohortChartConfig = {
  avg_views: { label: "Average views per clip", color: "var(--chart-1)" },
} satisfies ChartConfig;

const pipelineChartConfig = {
  count: { label: "Clips", color: "var(--chart-1)" },
} satisfies ChartConfig;

// Bars and areas draw in once, easing out from their feet.
const DRAW = { isAnimationActive: true, animationDuration: 700, animationEasing: "ease-out" as const };
const GRID = { stroke: "var(--rule-glass)", strokeDasharray: "0" };
const TICK = { fill: "var(--color-ink-3)", fontSize: 12 };

type Series = "views" | "watch" | "subs";
const SERIES: Array<{ value: Series; label: string }> = [
  { value: "views", label: "Views" },
  { value: "watch", label: "Watch" },
  { value: "subs", label: "Subscribers" },
];

function formatShortDate(isoDate: string): string {
  const parsed = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return isoDate;
  return parsed.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

const compact = (val: number) => (val >= 1000 ? `${(val / 1000).toFixed(0)}k` : String(val));

export function GrowthChartCard({
  data,
  isLoading = false,
}: {
  data: DashboardTimeseriesPoint[];
  isLoading?: boolean;
}) {
  const [seriesTab, setSeriesTab] = useState<Series>("views");
  const chartData = data.map((point) => ({ ...point, shortDate: formatShortDate(point.date) }));

  return (
    <Card className="h-full">
      <CardHeader className="items-center">
        <CardTitle>Growth</CardTitle>
        <CardAction>
          <DotTabs label="Growth series" value={seriesTab} onChange={setSeriesTab} options={SERIES} />
        </CardAction>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-[260px] w-full rounded-(--radius-inner)" />
        ) : chartData.length === 0 ? (
          <div className="flex h-[260px] items-center justify-center">
            <EmptyState>No snapshots yet. Metrics sync every 30 minutes once a Short is posted.</EmptyState>
          </div>
        ) : (
          <ChartContainer key={seriesTab} config={growthChartConfig} className="fade-in aspect-auto h-[260px] w-full">
            <AreaChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid vertical={false} {...GRID} />
              <XAxis dataKey="shortDate" tickLine={false} axisLine={false} tickMargin={8} tick={TICK} />
              <YAxis tickLine={false} axisLine={false} width={48} tick={TICK} tickFormatter={compact} />
              <ChartTooltip content={<ChartTooltipContent indicator="line" />} />
              <ChartLegend content={<ChartLegendContent />} />
              {seriesTab === "views" && (
                <>
                  <Area type="monotone" dataKey="views" stroke="var(--color-views)" strokeWidth={2} fill="var(--color-views)" fillOpacity={0.1} {...DRAW} />
                  <Area type="monotone" dataKey="engaged_views" stroke="var(--color-engaged_views)" strokeWidth={2} fill="transparent" {...DRAW} />
                </>
              )}
              {seriesTab === "watch" && (
                <Area type="monotone" dataKey="watch_hours" stroke="var(--color-watch_hours)" strokeWidth={2} fill="var(--color-watch_hours)" fillOpacity={0.1} {...DRAW} />
              )}
              {seriesTab === "subs" && (
                <Area type="monotone" dataKey="net_subscribers" stroke="var(--color-net_subscribers)" strokeWidth={2} fill="var(--color-net_subscribers)" fillOpacity={0.1} {...DRAW} />
              )}
            </AreaChart>
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  );
}

export function DurationCohortChartCard({
  cohorts,
  isLoading = false,
}: {
  cohorts: DashboardCohortPoint[];
  isLoading?: boolean;
}) {
  const hasAny = cohorts.some((c) => c.clip_count > 0);
  return (
    <Card className="h-full">
      <CardHeader>
        <CardTitle>Views by clip length</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-[230px] w-full rounded-(--radius-inner)" />
        ) : !hasAny ? (
          <div className="flex h-[230px] items-center justify-center">
            <EmptyState>Appears once posted Shorts have views.</EmptyState>
          </div>
        ) : (
          <ChartContainer config={cohortChartConfig} className="fade-in aspect-auto h-[230px] w-full">
            <BarChart data={cohorts} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid vertical={false} {...GRID} />
              <XAxis dataKey="bucket" tickLine={false} axisLine={false} tickMargin={8} tick={TICK} />
              <YAxis tickLine={false} axisLine={false} width={48} tick={TICK} tickFormatter={compact} />
              <ChartTooltip content={<ChartTooltipContent indicator="line" />} />
              <Bar dataKey="avg_views" fill="var(--color-avg_views)" radius={[2, 2, 0, 0]} maxBarSize={24} {...DRAW} />
            </BarChart>
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  );
}

export function PipelineBreakdownCard({
  stages,
  outcomes,
  isLoading = false,
}: {
  stages: DashboardFunnelStage[];
  outcomes: { posted_publications: number; kept_local: number; archived: number; failed: number } | null;
  isLoading?: boolean;
}) {
  const hasAny = stages.some((s) => s.count > 0);
  return (
    <Card className="h-full">
      <CardHeader>
        <CardTitle>From draft to posted</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-(--space-md)">
        {isLoading ? (
          <Skeleton className="h-[195px] w-full rounded-(--radius-inner)" />
        ) : !hasAny ? (
          <div className="flex h-[195px] items-center justify-center">
            <EmptyState>Appears once the first chunk has been clipped.</EmptyState>
          </div>
        ) : (
          <ChartContainer config={pipelineChartConfig} className="fade-in aspect-auto h-[195px] w-full">
            <BarChart data={stages} layout="vertical" margin={{ top: 4, right: 16, left: 4, bottom: 4 }}>
              <CartesianGrid horizontal={false} {...GRID} />
              <XAxis type="number" hide />
              <YAxis dataKey="stage" type="category" tickLine={false} axisLine={false} width={76} tick={TICK} />
              <ChartTooltip content={<ChartTooltipContent indicator="line" />} />
              <Bar dataKey="count" fill="var(--color-count)" radius={[0, 2, 2, 0]} maxBarSize={24} {...DRAW} />
            </BarChart>
          </ChartContainer>
        )}

        {outcomes && (
          <dl className="grid grid-cols-3 border-t border-(--rule-glass) pt-(--space-sm) text-sm tabular-nums">
            {[
              ["Kept local", outcomes.kept_local],
              ["Archived", outcomes.archived],
              ["Failed", outcomes.failed],
            ].map(([label, value]) => (
              <div key={label} className="flex flex-col gap-(--space-3xs) border-l border-(--rule-glass) px-(--space-sm) first:border-l-0 first:pl-0">
                <dt className="text-(--color-ink-3)">{label}</dt>
                <dd className="m-0 font-medium text-foreground">{value}</dd>
              </div>
            ))}
          </dl>
        )}
      </CardContent>
    </Card>
  );
}
