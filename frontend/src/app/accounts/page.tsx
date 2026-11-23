"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { HardDrive, Link2, MonitorPlay, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import {
  EmptyState,
  Initial,
  Notice,
  PageHeader,
  Panel,
  StatusWord,
  Tally,
  formatDateTime,
  rise,
  toneForStatus,
} from "@/components/page-kit";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Account, deleteAccount, getAccounts, startGoogleAuth, startGoogleDriveAuth } from "@/lib/api";

function formatScopes(scopes?: string | null): string[] {
  if (!scopes) return [];
  return scopes
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => s.replace("https://www.googleapis.com/auth/", ""));
}

function isDriveAccount(acc: Account): boolean {
  return acc.provider === "google_drive" || acc.oauth_provider === "google_drive" || Boolean(acc.channel_id?.startsWith("drive-only:"));
}

function ConnectedBanner() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const [dismissed, setDismissed] = useState(false);
  const connected = searchParams.get("connected");
  const count = searchParams.get("count");

  if (dismissed || (connected !== "youtube" && connected !== "google_drive")) return null;

  const parsed = count ? Number(count) : null;
  const label =
    connected === "google_drive"
      ? "the Google Drive storage account"
      : parsed !== null && !Number.isNaN(parsed)
        ? `${parsed} YouTube channel${parsed === 1 ? "" : "s"}`
        : "a YouTube channel";

  return (
    <Notice
      tone="success"
      action={
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            setDismissed(true);
            router.replace("/accounts");
          }}
        >
          Dismiss
        </Button>
      }
    >
      Connected {label}.
    </Notice>
  );
}

export default function AccountsPage() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(true);
  const [connecting, setConnecting] = useState<"youtube" | "drive" | null>(null);
  const [disconnectingId, setDisconnectingId] = useState<string | number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadAccounts = useCallback(async () => {
    try {
      const data = await getAccounts();
      setAccounts(data.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load connected accounts");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    getAccounts()
      .then((data) => {
        if (!active) return;
        setAccounts(data.items);
        setError(null);
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : "Failed to load connected accounts");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const startAuth = async (kind: "youtube" | "drive") => {
    setConnecting(kind);
    try {
      const res = kind === "youtube" ? await startGoogleAuth() : await startGoogleDriveAuth();
      if (!res.configured || !res.auth_url) {
        toast.error(
          kind === "youtube"
            ? "Google sign-in is not set up. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env."
            : "Drive sign-in is not set up. Add GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET to .env."
        );
        return;
      }
      window.location.assign(res.auth_url);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to start Google sign-in");
    } finally {
      setConnecting(null);
    }
  };

  const handleDisconnect = async (acc: Account) => {
    const numericId = Number(acc.id);
    if (!Number.isFinite(numericId)) return;
    setDisconnectingId(acc.id);
    try {
      await deleteAccount(numericId);
      setAccounts((prev) => prev.filter((item) => item.id !== acc.id));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to disconnect account");
    } finally {
      setDisconnectingId(null);
    }
  };

  const driveAccounts = accounts.filter(isDriveAccount);
  const youtubeAccounts = accounts.filter((a) => !isDriveAccount(a));
  const activeCount = accounts.filter((a) => a.status === "active").length;
  const firstLoad = loading && accounts.length === 0;

  return (
    <div className="flex flex-col gap-(--space-lg)">
      <PageHeader
        title="Accounts"
        meta="Tokens are encrypted on this machine"
        actions={
          <div className="glass flex flex-wrap items-center gap-(--space-xs) rounded-full p-(--space-2xs)">
            <Button disabled={connecting !== null} aria-busy={connecting === "youtube"} onClick={() => void startAuth("youtube")}>
              <MonitorPlay className="size-3.5" />
              {connecting === "youtube" ? "Opening Google" : "Connect YouTube"}
            </Button>
            <Button variant="outline" disabled={connecting !== null} aria-busy={connecting === "drive"} onClick={() => void startAuth("drive")}>
              <HardDrive className="size-3.5" />
              {connecting === "drive" ? "Opening Google" : "Connect Drive"}
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Refresh accounts"
              disabled={loading}
              onClick={() => {
                setLoading(true);
                void loadAccounts();
              }}
            >
              <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />
            </Button>
          </div>
        }
      />

      <Suspense fallback={null}>
        <ConnectedBanner />
      </Suspense>

      {error && <Notice tone="error">{error}</Notice>}

      <Panel title="Connections" index={1}>
        <Tally
          columns={3}
          loading={firstLoad}
          items={[
            { label: "YouTube channels", value: youtubeAccounts.length, note: "Where Shorts are posted" },
            { label: "Drive storage", value: driveAccounts.length, note: driveAccounts[0]?.email || "Where drafts wait for review" },
            { label: "Tokens active", value: activeCount, note: `of ${accounts.length} connected` },
          ]}
        />
      </Panel>

      <Panel title="Connected accounts" count={accounts.length} flush index={2}>
        {firstLoad ? (
          <EmptyState>Loading accounts</EmptyState>
        ) : accounts.length === 0 ? (
          <EmptyState icon={<Link2 />}>Nothing connected. Connect a YouTube channel to post Shorts.</EmptyState>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="pl-(--space-lg)">Account</TableHead>
                <TableHead className="hidden md:table-cell">Used for</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="hidden md:table-cell">Token expires</TableHead>
                <TableHead className="hidden lg:table-cell">Permissions</TableHead>
                <TableHead className="pr-(--space-lg) text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="tabular-nums">
              {accounts.map((acc, i) => {
                const title = acc.channel_title || acc.name || acc.channel_id || `Account ${acc.id}`;
                const drive = isDriveAccount(acc);
                const scopes = formatScopes(acc.scopes);
                return (
                  <TableRow key={acc.id} className="rise-row" style={rise(i)}>
                    <TableCell className="max-w-[300px] pl-(--space-lg)">
                      <div className="flex items-center gap-(--space-sm)">
                        <Initial name={title} />
                        <div className="min-w-0">
                          <div className="truncate font-medium text-foreground">{title}</div>
                          <div className="truncate text-sm text-(--color-ink-3)">
                            {acc.email || acc.chat_id || acc.channel_id || "Connected"}
                          </div>
                        </div>
                      </div>
                    </TableCell>
                    <TableCell className="hidden text-(--color-ink-2) md:table-cell">{drive ? "Drive storage" : "Posting Shorts"}</TableCell>
                    <TableCell>
                      <StatusWord tone={toneForStatus(acc.status)}>{acc.status}</StatusWord>
                    </TableCell>
                    <TableCell className="hidden text-(--color-ink-2) md:table-cell">
                      <div>{formatDateTime(acc.token_expires_at)}</div>
                      {typeof acc.has_refresh_token === "boolean" && (
                        <div className="text-sm text-(--color-ink-3)">
                          {acc.has_refresh_token ? "Renews itself" : "No refresh token"}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="hidden max-w-[280px] text-sm text-(--color-ink-3) lg:table-cell">
                      {scopes.length === 0 ? "none listed" : scopes.join(", ")}
                    </TableCell>
                    <TableCell className="pr-(--space-lg) text-right">
                      {Number.isFinite(Number(acc.id)) && (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-(--color-error) hover:text-(--color-error)"
                          disabled={disconnectingId === acc.id}
                          aria-busy={disconnectingId === acc.id}
                          onClick={() => void handleDisconnect(acc)}
                        >
                          {disconnectingId === acc.id ? "Disconnecting" : "Disconnect"}
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </Panel>
    </div>
  );
}
