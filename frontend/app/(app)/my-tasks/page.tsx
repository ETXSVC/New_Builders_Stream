"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { TASK_STATUSES, labelFor } from "@/lib/state-machines";
import { formatDate } from "@/lib/format";
import { DailyLogForm, type DailyLogDraft } from "@/components/tasks/DailyLogForm";
import { useConnectionEvidence, useOfflineIdentity, useRetryTicker } from "@/lib/offline/hooks";
import { fetchMyTasks, primeCrewOffline, saveTasksForOffline, sendQueueItem } from "@/lib/offline/crew";
import { clearOfflineCaches } from "@/lib/offline/reset";
import {
  deleteQueueItem,
  listQueueItems,
  readCrewReference,
  saveQueueItem,
  type CachedTask,
  type CrewReferenceData,
  type QueueItem,
  type QueuedWrite,
} from "@/lib/offline/store";

/**
 * The field crew's entire product, and the second screen that works with no
 * network.
 *
 * A crew member can make exactly two writes anywhere in this application —
 * the status of a task assigned to them, and a daily log on a project they
 * hold a task on — and both are here. Design and decisions:
 * `docs/superpowers/specs/2026-08-04-field-crew-offline-queue-design.md`.
 *
 * ## Every write goes through the queue, online or not
 *
 * Not "post it, and fall back to the queue if that fails". One path, always:
 * the write is recorded locally, then sent. Online that is invisible — it
 * lands in the same instant — and it removes the class of bug where the
 * offline path is the one nobody exercises until somebody is standing in a
 * basement.
 */

const OFFLINE_TASK_STATUSES = TASK_STATUSES;

export default function MyTasksPage() {
  const { accessToken } = useAuth();
  const { identity, resolved } = useOfflineIdentity();
  const { reachable, noteAttempt } = useConnectionEvidence();

  const [tasks, setTasks] = React.useState<CachedTask[]>([]);
  const [offlineData, setOfflineData] = React.useState<CrewReferenceData | null>(null);
  const [queue, setQueue] = React.useState<QueueItem[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [priming, setPriming] = React.useState(false);
  const [savedLogs, setSavedLogs] = React.useState(0);

  const waiting = queue.some((item) => item.status !== "needs_attention");
  const { tick, retryNow } = useRetryTicker(waiting && !!accessToken);

  // Which items have been auto-attempted, and on which tick. Keyed by tick
  // rather than cleared on a timer: an item is tried at most once per tick,
  // so a deferred send cannot spin against a dead connection, and the next
  // tick retries it without any bookkeeping to reset.
  const attemptedRef = React.useRef<Map<string, number>>(new Map());
  const sendingRef = React.useRef(false);

  const refreshQueue = React.useCallback(async () => {
    if (!identity) return;
    setQueue(await listQueueItems(identity));
  }, [identity]);

  // --- Loading -----------------------------------------------------------

  React.useEffect(() => {
    if (!resolved || !identity) return;
    void (async () => {
      const stored = await readCrewReference(identity);
      setOfflineData(stored);
      // Cache first so a cold start with no network shows something
      // immediately; the network load below replaces it when it works.
      if (stored) setTasks(stored.tasks);
      await refreshQueue();
      setLoading(false);
    })();
  }, [identity, refreshQueue, resolved]);

  React.useEffect(() => {
    if (!accessToken || !identity) return;
    void (async () => {
      try {
        const fresh = await fetchMyTasks(accessToken);
        setTasks(fresh);
        setError(null);
        noteAttempt(true);
        // Keep the offline copy current, but ONLY for a device that already
        // asked for one. Caching on every visit would put a tenant's task
        // list on any browser that ever loaded this page.
        const stored = await readCrewReference(identity);
        if (stored) setOfflineData(await saveTasksForOffline(identity, fresh));
      } catch {
        noteAttempt(false);
        // Not an error banner when there is a cached list to show: the
        // screen is doing exactly what it was built to do.
        if (!offlineData) setError("Unable to reach the server. Check your connection.");
      } finally {
        setLoading(false);
      }
    })();
    // `offlineData` is deliberately not a dependency: it is read to decide
    // what a failure MEANS, and including it would re-fetch every time the
    // cache is written, which this effect does itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accessToken, identity, noteAttempt, tick]);

  // --- Sending -----------------------------------------------------------

  React.useEffect(() => {
    if (!accessToken || !identity || sendingRef.current) return;
    const pending = queue.filter(
      (item) => item.status !== "needs_attention" && attemptedRef.current.get(item.id) !== tick
    );
    if (pending.length === 0) return;

    void (async () => {
      sendingRef.current = true;
      try {
        // In order, one at a time. Two status changes to the same task must
        // arrive in the order they were made, or the second one's
        // `expected_status` describes a state the first one already replaced
        // — and the crew member would be told their own change conflicts.
        for (const item of pending) {
          attemptedRef.current.set(item.id, tick);
          const outcome = await sendQueueItem(item, accessToken);
          noteAttempt(outcome.kind !== "deferred");
          // Bound before the closure below: narrowing `item.write` does not
          // survive into a callback, because it is a mutable property.
          const write = item.write;
          const current = outcome.kind === "parked" ? outcome.currentStatus : undefined;
          if (current && write.kind === "task_status") {
            // Show what the server says, not what this device hoped.
            setTasks((prev) =>
              prev.map((task) => (task.id === write.task_id ? { ...task, status: current } : task))
            );
          }
        }
        await refreshQueue();
      } finally {
        sendingRef.current = false;
      }
    })();
  }, [accessToken, identity, noteAttempt, queue, refreshQueue, tick]);

  // --- Writing -----------------------------------------------------------

  const enqueue = React.useCallback(
    async (write: QueuedWrite) => {
      if (!identity) return;
      const item: QueueItem = {
        id: crypto.randomUUID(),
        identity: identity.userId + ":" + identity.companyId,
        created_at: new Date().toISOString(),
        status: "queued",
        attention: null,
        write,
      };
      await saveQueueItem(item);
      await refreshQueue();
      retryNow();
    },
    [identity, refreshQueue, retryNow]
  );

  async function handleStatusChange(task: CachedTask, status: string) {
    setError(null);
    // Optimistic, and honest about it: the row shows the new value with the
    // queue below saying it has not landed yet. A crew member who cannot see
    // their own change has no way to tell it registered.
    setTasks((prev) => prev.map((t) => (t.id === task.id ? { ...t, status } : t)));
    await enqueue({
      kind: "task_status",
      task_id: task.id,
      task_name: task.name,
      // The value they were looking at when they changed it — what the
      // server compares against, and refuses on if the task has moved.
      expected_status: task.status,
      status,
    });
  }

  async function handleDailyLog(draft: DailyLogDraft) {
    setError(null);
    setSavedLogs((count) => count + 1);
    await enqueue({
      kind: "daily_log",
      project_id: draft.project_id,
      project_name: draft.project_name,
      // Generated once, here, and repeated on every retry of this log. It
      // is what makes sending it twice harmless — daily logs cannot be
      // updated or deleted by any runtime role, so a duplicate would be
      // permanent.
      client_reference: crypto.randomUUID(),
      log_date: draft.log_date,
      weather: draft.weather,
      notes: draft.notes,
    });
  }

  /** Apply a parked change against what the task says NOW — a human's call. */
  async function handleApplyAnyway(item: QueueItem) {
    if (item.write.kind !== "task_status" || !item.attention?.current_status) return;
    await saveQueueItem({
      ...item,
      status: "queued",
      attention: null,
      write: { ...item.write, expected_status: item.attention.current_status },
    });
    await refreshQueue();
    retryNow();
  }

  async function handleDiscard(item: QueueItem) {
    await deleteQueueItem(item.id);
    const write = item.write;
    const restored = item.attention?.current_status;
    if (write.kind === "task_status" && restored) {
      // Put the row back to what the server actually says — the optimistic
      // value on screen was this change, and it is being thrown away.
      setTasks((prev) =>
        prev.map((task) => (task.id === write.task_id ? { ...task, status: restored } : task))
      );
    }
    await refreshQueue();
  }

  // --- Offline data ------------------------------------------------------

  async function handlePrime() {
    if (!accessToken || !identity || priming) return;
    setPriming(true);
    setError(null);
    try {
      setOfflineData(await primeCrewOffline(accessToken, identity));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not prepare offline data.");
    } finally {
      setPriming(false);
    }
  }

  async function handleRemoveOfflineData() {
    await clearOfflineCaches();
    setOfflineData(null);
  }

  // --- Rendering ---------------------------------------------------------

  const projects = React.useMemo(() => {
    const byId = new Map<string, string>();
    for (const task of tasks) byId.set(task.project_id, task.project_name);
    return Array.from(byId.entries()).map(([id, name]) => ({ id, name }));
  }, [tasks]);

  const parked = queue.filter((item) => item.status === "needs_attention");
  const pending = queue.filter((item) => item.status !== "needs_attention");

  return (
    <main className="p-6 flex flex-col gap-6 max-w-2xl">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">My tasks</h1>
        <span
          className={`text-xs rounded-full px-2 py-1 ${
            reachable ? "bg-slate-100 text-slate-600" : "bg-amber-100 text-amber-800"
          }`}
        >
          {reachable ? "Online" : "Offline — your changes are saved on this device"}
        </span>
      </div>

      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      {parked.length > 0 && (
        <section className="flex flex-col gap-3">
          <h2 className="font-medium">Needs your attention</h2>
          {parked.map((item) => (
            <div
              key={item.id}
              className="rounded-md border border-amber-300 bg-amber-50/40 p-3 flex flex-col gap-2 text-sm"
            >
              <span className="font-medium">
                {item.write.kind === "task_status"
                  ? item.write.task_name
                  : `Daily log — ${item.write.project_name}`}
              </span>
              <p className="text-slate-700">{item.attention?.message}</p>
              <div className="flex flex-wrap gap-2">
                {item.write.kind === "task_status" && item.attention?.current_status && (
                  <Button
                    type="button"
                    size="sm"
                    onClick={() => void handleApplyAnyway(item)}
                    disabled={!accessToken}
                  >
                    Apply {labelFor(item.write.status)} anyway
                  </Button>
                )}
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => void handleDiscard(item)}
                >
                  Discard
                </Button>
              </div>
            </div>
          ))}
        </section>
      )}

      {!loading && tasks.length === 0 && !error && (
        <p className="text-sm text-slate-600">No tasks assigned to you right now.</p>
      )}

      {tasks.length > 0 && (
        <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
          {tasks.map((task) => (
            <li key={task.id} className="flex items-center gap-4 px-4 py-3 text-sm">
              <span className="flex-1">
                <span className="block font-medium">{task.name}</span>
                <Link
                  href={`/projects/${task.project_id}`}
                  className="text-slate-600 hover:underline"
                >
                  {task.project_name}
                </Link>
              </span>
              <span className="text-slate-500">{formatDate(task.due_date)}</span>
              <Select
                aria-label={`Status for ${task.name}`}
                className="w-32 h-8"
                value={task.status}
                onChange={(e) => void handleStatusChange(task, e.target.value)}
              >
                {OFFLINE_TASK_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {labelFor(s)}
                  </option>
                ))}
              </Select>
            </li>
          ))}
        </ul>
      )}

      {pending.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="font-medium">Waiting to send</h2>
          <ul className="flex flex-col gap-1 text-sm text-slate-600">
            {pending.map((item) => (
              <li key={item.id}>
                {item.write.kind === "task_status"
                  ? `${item.write.task_name} → ${labelFor(item.write.status)}`
                  : `Daily log — ${item.write.project_name}, ${formatDate(item.write.log_date)}`}
              </li>
            ))}
          </ul>
        </section>
      )}

      {savedLogs > 0 && pending.length === 0 && parked.length === 0 && (
        <p className="text-sm text-slate-600">
          {savedLogs === 1 ? "Daily log sent." : `${savedLogs} daily logs sent.`}
        </p>
      )}

      <section className="flex flex-col gap-3 rounded-md border border-slate-200 p-4">
        <h2 className="font-medium">Daily log</h2>
        {projects.length === 0 ? (
          <p className="text-sm text-slate-600">
            You can write a daily log for any project you have a task on.
          </p>
        ) : (
          <DailyLogForm projects={projects} onSubmit={(draft) => void handleDailyLog(draft)} />
        )}
      </section>

      <section className="rounded-md border border-slate-200 p-4 flex flex-col gap-2">
        <h2 className="font-medium">Offline data</h2>
        <p className="text-sm text-slate-600">
          Store your task list on this device so this screen works with no signal. Status changes
          and daily logs you make offline are held here and sent when you are back in range.
        </p>
        {offlineData ? (
          <p className="text-sm text-slate-600">
            Stored {new Date(offlineData.cached_at).toLocaleString()} · {offlineData.tasks.length}{" "}
            task{offlineData.tasks.length === 1 ? "" : "s"}
          </p>
        ) : (
          <p className="text-sm text-slate-600">Nothing is stored on this device yet.</p>
        )}
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            onClick={() => void handlePrime()}
            disabled={priming || !reachable || !accessToken}
          >
            {priming ? "Preparing…" : offlineData ? "Refresh offline data" : "Make available offline"}
          </Button>
          {offlineData && (
            <Button type="button" variant="outline" onClick={() => void handleRemoveOfflineData()}>
              Remove offline data
            </Button>
          )}
        </div>
      </section>
    </main>
  );
}
