const ENTRY_URL = "http://127.0.0.1:18801";
const TERMINAL = new Set(["completed", "failed", "cancelled", "canceled"]);
const POLL_INTERVAL_MS = 750;
const TASK_TIMEOUT_MS = 900_000;
const HOOK_TIMEOUT_MS = 930_000;
const ROUTE_CACHE_MS = 1_080_000;
const routes = new Map();

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJson(url, options = {}, timeoutMs = 8_000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const body = await response.json();
    if (!response.ok || body?.ok === false) {
      throw new Error(`HTTP ${response.status}: ${JSON.stringify(body)}`);
    }
    return body;
  } finally {
    clearTimeout(timer);
  }
}

async function waitForTask(taskId) {
  const deadline = Date.now() + TASK_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const body = await fetchJson(`${ENTRY_URL}/task/${encodeURIComponent(taskId)}`);
    const task = body?.task;
    if (!task || typeof task.status !== "string") {
      throw new Error(`AIOS task ${taskId} returned an invalid state`);
    }
    if (task.approval_required === true || task.status.toLowerCase() === "awaiting_approval") {
      return task;
    }
    if (TERMINAL.has(task.status.toLowerCase())) {
      return task;
    }
    await sleep(POLL_INTERVAL_MS);
  }
  throw new Error(`AIOS task ${taskId} timed out after ${TASK_TIMEOUT_MS / 1000}s`);
}

// [FIX] 2026-08-12 entry/feishu-result-push user-facing boundary:
//   The previous ``renderTask`` always prepended ``task_id``, ``executor``
//   and ``status`` to the user reply and then dumped the parent
//   ``result_summary`` verbatim.  For tasks like
//   ``"请只回复: AIOS已收到"`` the orchestrator aggregates the child
//   ``[opencode] AIOS已收到`` answer together with a "Verified Facts"
//   block (HF-001...) into the parent's ``result_summary``; the user must
//   only see their own final answer, never AIOS internals.
//
//   Selection rules (deterministic, in priority order):
//     1. If the task is awaiting owner approval, keep the existing
//        approval_required block — that text is mandatory for the user to
//        act and contains no internal telemetry.
//     2. Otherwise extract the user-facing answer from the parent
//        ``result_summary`` by stripping known internal prefixes and the
//        verification evidence block ("Verified Facts", HF-xxx,
//        Source/host evidence blocks, sandbox prompts).
//     3. If the parent summary is empty, fall back to the task ``error``.
//     4. As a last-resort compatibility fallback, accept the raw
//        ``result_summary``.
//
//   Task-id / executor / status are NEVER prepended unless the task is
//   awaiting approval.  This keeps the user reply free of AIOS internals
//   while preserving all of them inside AIOS for auditing and review.
const _INTERNAL_PREFIXES = [
  "[opencode]",
  "[codex]",
  "[claude]",
  "[hermes]",
];
const _INTERNAL_BLOCK_MARKERS = [
  "## Verified Facts",
  "## Verified Facts (program-generated",
  "## Host Evidence",
  "## Sources",
  "## Source",
  "## Evidence",
  "## Sandboxed",
  "## SANDBOX",
  "## Internal",
  "## Audit",
  "## Acceptance",
  "## AUTHORITATIVE",
];
const _INTERNAL_LINE_PREFIXES = [
  "Original user goal (authoritative):",
  "Assigned node:",
  "Acceptance summary:",
  "Evidence mode:",
  "System metadata:",
  "Bounded repair:",
  "SANDBOX CONSTRAINT",
  "FACT-USE:",
  "AUTHORITATIVE",
  "Trusted correction",
  "Copy requested",
  "[HF-",
  "[host_evidence]",
  "[source_",
];

function _extractUserAnswer(summary, status) {
  if (!summary) return "";
  const text = String(summary);
  // [FIX] 2026-08-12 entry/feishu-result-push user-facing boundary:
  //   On a failed parent the orchestrator stores a single telemetry line
  //   like ``"AIOS workflow failed: node 1: no_healthy_executor"``.  Strip
  //   the ``AIOS workflow failed:`` header and only surface the colon
  //   tail to the user.
  if (status === "failed") {
    const idx = text.indexOf(":");
    if (idx !== -1) return text.slice(idx + 1).trim().slice(0, 500);
    return text.trim().slice(0, 500);
  }
  // 1. Cut at any "## " block heading that is internal.
  let earliestCut = text.length;
  for (const marker of _INTERNAL_BLOCK_MARKERS) {
    const idx = text.indexOf(marker);
    if (idx !== -1 && idx < earliestCut) earliestCut = idx;
  }
  let head = text.slice(0, earliestCut);
  // 2. Split into lines and drop telemetry/prompt lines.
  const lines = head.split(/\r?\n/);
  // Drop leading boilerplate preamble.
  while (lines.length && _INTERNAL_PREFIXES.some((p) => lines[0].startsWith(p))) {
    lines.shift();
  }
  const cleaned = [];
  for (const ln of lines) {
    const s = ln.trimStart();
    if (!s) continue;
    if (_INTERNAL_LINE_PREFIXES.some((p) => s.startsWith(p))) continue;
    if (_INTERNAL_PREFIXES.some((p) => s.startsWith(p))) continue;
    cleaned.push(ln);
  }
  const out = cleaned.join("\n").trim();
  return out.slice(0, 1500);
}

function renderTask(task) {
  const approval =
    task.approval_required === true || task.status === "awaiting_approval"
      ? [
          "approval_required: true",
          `approval_id: ${task.approval_id || "unknown"}`,
          `risk_action: ${task.risk_action || "unknown"}`,
          "AIOS has not executed this task. Explicit owner approval is required.",
        ].join("\n")
      : "";

  if (approval) {
    return [
      approval,
      `task_id: ${task.task_id}`,
      `status: ${task.status}`,
    ].join("\n");
  }

  const userText =
    _extractUserAnswer(task.result_summary, task.status) ||
    (task.error ? String(task.error) : "") ||
    "(no result returned)";

  return userText;
}

async function routeToAios(text, metadata, api) {
  try {
    const created = await fetchJson(`${ENTRY_URL}/task`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        text,
        source: "openclaw",
        sender_id: metadata.senderId || "unknown",
        session_key: metadata.sessionKey || "",
      }),
    });
    const taskIds = Array.isArray(created.task_ids) ? created.task_ids : [];
    if (taskIds.length === 0) {
      throw new Error("AIOS accepted the request but returned no task_id");
    }

    // A child result is not deliverable until the AIOS-owned parent workflow
    // has planned, verified and aggregated it.
    const tasks = created.parent_id
      ? [await waitForTask(created.parent_id)]
      : await Promise.all(taskIds.map(waitForTask));
    const failed = tasks.filter((task) => task.status !== "completed");
    return {
      handled: true,
      reply: {
        text: tasks.map(renderTask).join("\n\n---\n\n"),
        isError: failed.length > 0,
      },
    };
  } catch (error) {
    api.logger.error(`AIOS bridge failed: ${error instanceof Error ? error.message : String(error)}`);
    return {
      handled: true,
      reply: {
        text: `AIOS 任务入口失败：${error instanceof Error ? error.message : String(error)}`,
        isError: true,
      },
    };
  }
}

function routeOnce(text, metadata, api) {
  const key = metadata.runId || `${metadata.sessionKey || "unknown"}\u0000${text}`;
  const existing = routes.get(key);
  if (existing) {
    return existing;
  }
  const routed = routeToAios(text, metadata, api);
  routes.set(key, routed);
  const timer = setTimeout(() => routes.delete(key), ROUTE_CACHE_MS);
  timer.unref?.();
  return routed;
}

export default {
  id: "aios-bridge",
  name: "AIOS Bridge",
  description: "Deterministic OpenClaw-to-AIOS routing and result aggregation",
  register(api) {
    api.on(
      "inbound_claim",
      async (event, context) => {
        const text = String(event.content || event.bodyForAgent || event.body || "").trim();
        if (!text || text.startsWith("/")) {
          return;
        }
        return routeOnce(text, {
          runId: context.runId,
          senderId: event.senderId || context.senderId,
          sessionKey: context.sessionKey,
        }, api);
      },
      { priority: 1000, timeoutMs: HOOK_TIMEOUT_MS },
    );

    api.on(
      "before_agent_reply",
      async (event, context) => {
        const text = String(event.cleanedBody || "").trim();
        if (!text || text.startsWith("/")) {
          return;
        }
        return routeOnce(text, {
          runId: context.runId,
          senderId: context.senderId,
          sessionKey: context.sessionKey,
        }, api);
      },
      { priority: 1000, timeoutMs: HOOK_TIMEOUT_MS },
    );
  },
};
