import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { randomBytes } from "node:crypto";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { SidecarSupervisor, withSidecarShutdown, type RpcResult } from "./sidecar.js";
import { rejectNonControlUiRequest } from "./control-ui-auth.js";
import {
  buildCronAddArgs,
  buildCronEditArgs,
  cronListArgs,
  cronIdentifier,
  inspectCronJob,
  parseCronJobs,
  runOpenClawCommand,
  type OpenClawCommandRunner,
  type ScheduleDefinition,
} from "./schedules.js";

type Api = {
  pluginConfig?: Record<string, unknown>;
  rootDir?: string;
  registrationMode?: string;
  logger?: { info?: (message: string) => void; warn?: (message: string) => void; error?: (message: string) => void };
  registerGatewayMethod?: (
    name: string,
    handler: (context: { params?: Record<string, unknown>; respond: (ok: boolean, payload?: unknown) => void }) => Promise<void>,
    opts?: { scope?: "operator.read" | "operator.write" | "operator.admin" },
  ) => void;
  registerTool?: (tool: Record<string, unknown>) => void;
  registerHook?: (
    events: string | string[],
    handler: (...args: any[]) => Promise<unknown> | unknown,
    opts?: { name?: string; description?: string },
  ) => void;
  on?: (
    event: string,
    handler: (...args: any[]) => Promise<unknown> | unknown,
    opts?: { name?: string; description?: string; priority?: number; timeoutMs?: number },
  ) => void;
  registerHttpRoute?: (route: {
    path: string;
    auth: "gateway" | "plugin";
    gatewayRuntimeScopeSurface?: "write-default" | "trusted-operator";
    handler: (req: any, res: any) => Promise<void>;
  }) => void;
  registerService?: (service: { id: string; start: () => Promise<void>; stop: () => Promise<void> }) => void;
  registerCli?: (registrar: (context: { program: any }) => Promise<void>, opts: Record<string, unknown>) => void;
  registerCommand?: (command: {
    name: string;
    description: string;
    acceptsArgs?: boolean;
    requireAuth?: boolean;
    requiredScopes?: string[];
    exposeSenderIsOwner?: boolean;
    handler: (context: { args?: string; senderId?: string; senderIsOwner?: boolean; gatewayClientScopes?: string[] }) => Promise<{ text: string }>;
  }) => void;
  session?: { controls?: { registerControlUiDescriptor?: (descriptor: Record<string, unknown>) => void } };
  runtime?: {
    system?: { runCommandWithTimeout?: OpenClawCommandRunner };
  };
};

type CommandResult = {
  code?: number;
  stdout?: unknown;
  stderr?: unknown;
};

/** OpenClaw passes toolCallId before the JSON parameters to tool executors. */
export function createToolExecutor(
  request: (method: string, params: Record<string, unknown>) => Promise<RpcResult>,
  method: string,
): (toolCallId: string, params: Record<string, unknown>) => Promise<{
  content: Array<{ type: "text"; text: string }>;
  details: RpcResult;
}> {
  return async (toolCallIdOrParams, params) => {
    // OpenClaw's native tool contract passes (toolCallId, params). Keep the
    // one-argument fallback for older discovery/test hosts that invoke the
    // executor with params only.
    const toolParams = (params && typeof params === "object")
      ? params
      : (toolCallIdOrParams && typeof toolCallIdOrParams === "object"
        ? toolCallIdOrParams as unknown as Record<string, unknown>
        : {});
    const result = await request(method, toolParams);
    return {
      content: [{ type: "text", text: JSON.stringify(result) }],
      details: result,
    };
  };
}

type DiscordPlanningBinding = {
  repository: string;
  route: string;
  sessionKey: string;
};

const PLUGIN_ID = "make-it-so";
// Version the embedded UI URL so a deployment cannot leave an older
// registration flow active in an OpenClaw/browser cache.
const UI_ASSET_VERSION = "20260721-registration-sources-1";
const DEFAULT_NUMBER_ONE_AGENT = "github-captain";
const DEFAULT_NUMBER_ONE_MODEL = "codex/gpt-5.6-sol";
const DEFAULT_NUMBER_ONE_THINKING = "high";
const DEFAULT_DISCORD_BOT_USER_IDS: string[] = [];
// Readiness turns can include a high-effort review plus tool calls. The host
// command timeout must exceed the inbound hook timeout because these turns run
// in the background after the Discord message has been claimed.
const NUMBER_ONE_TURN_TIMEOUT_MS = 600_000;
// The Python readiness review runs inside the same Number One turn. Keep its RPC
// deadline longer than the host command so a valid review cannot finish after
// the plugin has already sent a false failure notice to Discord.
export const READINESS_REVIEW_TIMEOUT_MS = NUMBER_ONE_TURN_TIMEOUT_MS + 60_000;

type SharedSidecarLease = {
  supervisor: SidecarSupervisor;
  references: number;
};

// OpenClaw can register a plugin more than once during pre-warming and reloads.
// Keep one sidecar per config in the process so those registrations cannot race
// over the same SQLite state directory.
const sharedSidecars = new Map<string, SharedSidecarLease>();

function sidecarKey(executable: string, args: string[], configPath: string): string {
  return JSON.stringify([executable, args, configPath]);
}

function acquireSharedSidecar(
  options: { executable: string; args: string[]; configPath: string },
  log: (message: string, error?: unknown) => void,
): { supervisor: SidecarSupervisor; release: () => Promise<void> } {
  const key = sidecarKey(options.executable, options.args, options.configPath);
  let lease = sharedSidecars.get(key);
  if (!lease) {
    lease = {
      supervisor: new SidecarSupervisor(options, log),
      references: 0,
    };
    sharedSidecars.set(key, lease);
  }
  lease.references += 1;
  let released = false;
  return {
    supervisor: lease.supervisor,
    release: async () => {
      if (released) return;
      released = true;
      lease!.references -= 1;
      if (lease!.references <= 0) {
        sharedSidecars.delete(key);
        await lease!.supervisor.stop();
      }
    },
  };
}

const CONFIG_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    configPath: { type: "string" },
    pythonExecutable: { type: "string", default: "python3" },
    sidecarCommand: { type: "array", items: { type: "string" }, default: ["-m", "make_it_so.sidecar"] },
    openclawExecutable: { type: "string", default: "" },
    numberOneAgent: { type: "string", default: DEFAULT_NUMBER_ONE_AGENT },
    numberOneModel: { type: "string", default: DEFAULT_NUMBER_ONE_MODEL },
    numberOneThinking: { type: "string", default: DEFAULT_NUMBER_ONE_THINKING },
    autoPersistDiscordAnswers: { type: "boolean", default: true },
    readinessReviewHarness: { type: "string", default: "openclaw" },
    discordBotUserIds: { type: "array", items: { type: "string" }, default: DEFAULT_DISCORD_BOT_USER_IDS },
    discordRouteAliases: { type: "object", additionalProperties: { type: "string" }, default: {} },
    installSchedules: { type: "boolean", default: false },
  },
};

function configString(config: Record<string, unknown>, key: string, fallback: string): string {
  const value = config[key];
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function expandPath(value: string): string {
  return value.startsWith("~/") ? join(homedir(), value.slice(2)) : value;
}

export function resolveOpenClawExecutable(config: Record<string, unknown>): string {
  const configured = config["openclawExecutable"];
  if (typeof configured === "string" && configured.trim()) return expandPath(configured.trim());
  const candidates = process.platform === "win32"
    ? [
        join(homedir(), "AppData", "Roaming", "npm", "openclaw.cmd"),
        join(homedir(), "AppData", "Roaming", "npm", "openclaw"),
        "openclaw.cmd",
        "openclaw",
      ]
    : [
        join(homedir(), ".npm-global", "bin", "openclaw"),
        "/usr/local/bin/openclaw",
        "/usr/bin/openclaw",
        "openclaw",
      ];
  return candidates.find((candidate) => candidate === "openclaw" || candidate === "openclaw.cmd" || existsSync(candidate)) ?? candidates[candidates.length - 1];
}

function configArgs(config: Record<string, unknown>): string[] {
  const value = config.sidecarCommand;
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? [...value]
    : ["-m", "make_it_so.sidecar"];
}

function discordRouteKeys(value: unknown): Set<string> {
  if (typeof value !== "string") return new Set();
  const normalized = value.trim().toLowerCase();
  if (!normalized) return new Set();
  const withoutPrefix = normalized.startsWith("channel:") ? normalized.slice("channel:".length) : normalized;
  return new Set([normalized, withoutPrefix, `channel:${withoutPrefix}`]);
}

export function discordPlanningRouteMatches(route: string, values: unknown[]): boolean {
  const routeKeys = discordRouteKeys(route);
  return values.some((value) => {
    const candidateKeys = discordRouteKeys(value);
    return [...candidateKeys].some((key) => routeKeys.has(key));
  });
}

/**
 * Build a per-event deduplication key without treating identical user replies
 * as the same event when the host omits a Discord message id.
 */
export function discordPlanningEventKey(
  event: Record<string, unknown>,
  context: Record<string, unknown>,
  content: string,
): string | undefined {
  const identity = [
    event.messageId,
    event.eventId,
    event.id,
    event.timestamp,
    event.createdAt,
    context.eventId,
    context.turnId,
    context.requestId,
    context.sessionId,
  ].find((value) => typeof value === "string" && value.trim());
  if (typeof identity !== "string") return undefined;
  return `${identity}:${content}`;
}

export function isDiscordPlanningCourseStatus(status: string): boolean {
  return [
    // A newly registered repository can remain in baseline_review while the
    // course charter and readiness answers are being completed. It is still a
    // valid Number One conversation state, so Discord approvals must map to it.
    "baseline_review",
    "awaiting_approval",
    "ready",
    "readiness_review",
    "engaged",
    "planning",
    "executing",
    "pr_open",
    "reviewing",
    "repairing",
    "completion_ready",
    "post_merge_verification",
    "blocked",
    "degraded",
  ].includes(status.trim().toLowerCase());
}

/** Recognize only an explicit approval; ordinary planning answers stay conversational. */
export function parseDiscordCourseApproval(content: string): "approve" | undefined {
  const firstLine = content.trim().split(/\r?\n/, 1)[0] ?? "";
  if (/^\s*(?:a|b)\s*[^A-Za-z0-9]*approve(?:d)?\b/i.test(firstLine)) return "approve";
  if (/^\s*(?:a|b)\s*(?:[-–—:]\s*)?approve(?:d)?\b/i.test(firstLine)) return "approve";
  if (/^\s*(?:i\s+)?approve(?:d)?\b/i.test(firstLine)) return "approve";
  return undefined;
}

function configBoolean(config: Record<string, unknown>, key: string, fallback: boolean): boolean {
  return typeof config[key] === "boolean" ? config[key] as boolean : fallback;
}

const DISCORD_READINESS_HINTS: Array<[string, RegExp]> = [
  ["secret-references", /\b(secret|credential|api[- ]?key|password)\b/i],
  // Recovery answers often mention owner approval, force-push, or branches;
  // recognize rollback before permissions so those safeguards are not misrouted.
  ["rollback", /\b(rollback|revert|recovery|failed milestone|recovery path)\b/i],
  ["permissions", /\b(permission|authorized|branch|pull request|issue|merge|owner[- ]approved)\b/i],
  ["environments", /\b(environment|workspace|linux|python|operating system|runtime version|development environment)\b/i],
  ["architecture-constraints", /\b(architecture|database|file(?:system)? layout|cli compatibility|language|current stack|data format)\b/i],
  ["non-goals", /\b(out of scope|non[- ]?goal|exclude|excluding)\b/i],
  ["users", /\b(primary user|users?|maintainer|operator)\b/i],
  ["external-access", /\b(external access|github|discord|network|internet|provider)\b/i],
  ["test-data", /\b(test data|fixture|image fixture|jpeg|png|webp|sample)\b/i],
  ["deployment", /\b(deploy|deployment|cloud|production rollout|release)\b/i],
  ["observability", /\b(observability|logging?|monitor(?:ing)?|metrics|status|stats)\b/i],
  ["security", /\b(security|privacy|authentication|retention|destructive)\b/i],
  ["UX-inputs", /\b(usability|\bux\b|user interface|cli output|help|error message|command)\b/i],
  ["token-policy", /\b(model|token usage|quota|spend|budget|economical|expensive)\b/i],
  ["CI", /\b\bci\b|continuous integration|lint|typecheck|pytest|test suite/i],
  ["exit-criteria", /\b(exit criteria|acceptance criteria|definition of done|done criteria)\b/i],
  ["goals", /\b(goal|outcome|complete|success)\b/i],
];

/** Infer the requirement from the answer's topic when Number One asks conversationally. */
export function inferDiscordReadinessKey(content: string, course: unknown): string | undefined {
  if (!content.trim() || !course || typeof course !== "object") return undefined;
  const readiness = (course as Record<string, unknown>).readiness;
  if (!Array.isArray(readiness)) return undefined;
  const available = new Set(
    readiness.filter((item) => {
      if (!item || typeof item !== "object") return false;
      const requirement = item as Record<string, unknown>;
      const status = String(requirement.status ?? "").toLowerCase();
      return requirement.required !== false && !["verified", "waived"].includes(status);
    }).map((item) => String((item as Record<string, unknown>).key ?? "")),
  );
  for (const [key, pattern] of DISCORD_READINESS_HINTS) {
    if (available.has(key) && pattern.test(content)) return key;
  }
  const explicit = [...available].find((key) => key && discordAnswerMentionsRequirement(content, key));
  return explicit;
}

export function pendingDiscordReadinessKey(course: unknown, content = ""): string | undefined {
  if (!course || typeof course !== "object") return undefined;
  const readiness = (course as Record<string, unknown>).readiness;
  if (!Array.isArray(readiness)) return undefined;
  const inferred = inferDiscordReadinessKey(content, course);
  if (inferred) return inferred;
  const pending = readiness.find((item) => {
    if (!item || typeof item !== "object") return false;
    const requirement = item as Record<string, unknown>;
    const status = String(requirement.status ?? "").toLowerCase();
    return requirement.required !== false && (!status || ["unknown", "blocked"].includes(status));
  });
  // An owner answer invalidates the prior independent review. Treat an
  // answered requirement as the next actionable item while that review is
  // stale, so a retried Discord message can safely re-run the review instead
  // of falling through as ordinary conversation.
  const review = (course as Record<string, unknown>).readiness_review;
  const reviewVerdict = review && typeof review === "object"
    ? String((review as Record<string, unknown>).verdict ?? "").toLowerCase()
    : "";
  const reviewNeedsRefresh = reviewVerdict !== "ready";
  const answered = reviewNeedsRefresh
    ? readiness.find((item) => {
        if (!item || typeof item !== "object") return false;
        const requirement = item as Record<string, unknown>;
        return requirement.required !== false && String(requirement.status ?? "").toLowerCase() === "answered";
      })
    : undefined;
  const candidate = pending ?? answered;
  if (!candidate || typeof candidate !== "object") return undefined;
  const key = String((candidate as Record<string, unknown>).key ?? "").trim();
  return key || undefined;
}

/**
 * Number One is instructed to ask one unresolved readiness question at a
 * time. Bind the owner's reply to that durable queue position instead of
 * guessing from words in the answer itself. Answers often mention several
 * concerns (for example local models, users, and tests) and topic inference
 * can silently attach them to the wrong requirement.
 */
export function nextDiscordReadinessKey(course: unknown): string | undefined {
  if (!course || typeof course !== "object") return undefined;
  const readiness = (course as Record<string, unknown>).readiness;
  if (!Array.isArray(readiness)) return undefined;
  const item = readiness.find((value) => {
    if (!value || typeof value !== "object") return false;
    const requirement = value as Record<string, unknown>;
    const status = String(requirement.status ?? "").toLowerCase();
    return requirement.required !== false && !["answered", "verified", "waived"].includes(status);
  });
  if (!item || typeof item !== "object") return undefined;
  const key = String((item as Record<string, unknown>).key ?? "").trim();
  return key || undefined;
}

export type DiscordPendingReadinessQuestion = { key: string; question: string };

/** Return the exact readiness question that was durably delivered to the owner. */
export function discordPendingReadinessQuestion(course: unknown): DiscordPendingReadinessQuestion | undefined {
  if (!course || typeof course !== "object") return undefined;
  const record = course as Record<string, unknown>;
  const key = typeof record.pending_readiness_key === "string" ? record.pending_readiness_key.trim() : "";
  const question = typeof record.pending_readiness_question === "string"
    ? record.pending_readiness_question.trim()
    : "";
  return key && question ? { key, question } : undefined;
}

/** Select one reviewed question and bind it to its readiness requirement. */
export function selectDiscordReadinessQuestion(
  course: unknown,
  readinessReport?: Record<string, unknown>,
): DiscordPendingReadinessQuestion | undefined {
  if (!course || typeof course !== "object") return undefined;
  const record = course as Record<string, unknown>;
  const review = record.readiness_review;
  const nextQuestions = review && typeof review === "object" && Array.isArray((review as Record<string, unknown>).next_questions)
    ? ((review as Record<string, unknown>).next_questions as unknown[])
      .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];
  const reviewedQuestion = nextQuestions[0]?.trim();
  const inferredKey = reviewedQuestion ? inferDiscordReadinessKey(reviewedQuestion, course) : undefined;
  const unresolved = Array.isArray(readinessReport?.unresolved)
    ? readinessReport.unresolved.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];
  const key = inferredKey ?? unresolved[0]?.trim() ?? nextDiscordReadinessKey(course);
  if (!key) return undefined;
  const readiness = Array.isArray(record.readiness) ? record.readiness : [];
  const requirement = readiness.find((item) => item && typeof item === "object" && String((item as Record<string, unknown>).key ?? "") === key);
  const defaultQuestion = requirement && typeof requirement === "object"
    ? String((requirement as Record<string, unknown>).question ?? "").trim()
    : "";
  const question = reviewedQuestion || defaultQuestion;
  return question ? { key, question } : undefined;
}

/** Only persist a Discord answer when it explicitly names the item being answered. */
export function discordAnswerMentionsRequirement(content: string, requirementKey: string): boolean {
  const key = requirementKey.trim();
  if (!key) return false;
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|[^A-Za-z0-9_])${escaped}(?=$|[^A-Za-z0-9_])`, "i").test(content);
}

export function resolveDiscordRoute(route: string, config: Record<string, unknown>): string {
  const normalized = route.trim();
  if (!normalized) return normalized;
  const rawAliases = config.discordRouteAliases;
  if (!rawAliases || typeof rawAliases !== "object" || Array.isArray(rawAliases)) return normalized;
  const aliases = rawAliases as Record<string, unknown>;
  const exact = aliases[normalized];
  if (typeof exact === "string" && exact.trim()) return exact.trim();
  const lower = normalized.toLowerCase();
  const match = Object.entries(aliases).find(([key, value]) => key.toLowerCase() === lower && typeof value === "string" && value.trim());
  return match && typeof match[1] === "string" ? match[1].trim() : normalized;
}

export type DiscordRouteOption = {
  route: string;
  channel_id: string;
  guild_id?: string;
  name: string;
  label: string;
  alias?: string;
};

function commandOutputText(value: unknown): string {
  if (typeof value === "string") return value;
  if (ArrayBuffer.isView(value)) {
    return new TextDecoder().decode(new Uint8Array(value.buffer, value.byteOffset, value.byteLength));
  }
  return "";
}

function parseOpenClawCommandJson(stdout: unknown): Record<string, unknown> {
  const text = commandOutputText(stdout).trim();
  if (!text) throw new Error("OpenClaw returned an empty JSON response");
  const starts = [...text.matchAll(/[\[{]/g)].map((match) => match.index ?? 0);
  for (const start of starts) {
    try {
      const parsed: unknown = JSON.parse(text.slice(start));
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>;
    } catch {
      // OpenClaw may emit migration notices before its JSON payload.
    }
  }
  throw new Error("OpenClaw returned invalid JSON");
}

export function configuredDiscordRouteOptions(config: Record<string, unknown>): DiscordRouteOption[] {
  const aliases = config.discordRouteAliases;
  if (!aliases || typeof aliases !== "object" || Array.isArray(aliases)) return [];
  return Object.entries(aliases as Record<string, unknown>)
    .flatMap(([alias, target]) => {
      if (typeof target !== "string") return [];
      const match = target.trim().match(/^channel:(\d+)$/i);
      if (!match) return [];
      return [{
        route: `channel:${match[1]}`,
        channel_id: match[1],
        name: alias,
        label: `#${alias}`,
        alias,
      }];
    });
}

export function parseDiscordGuildId(stdout: unknown): string {
  const parsed = parseOpenClawCommandJson(stdout);
  const payload = parsed.payload;
  const channel = payload && typeof payload === "object" ? (payload as Record<string, unknown>).channel : undefined;
  const guildId = channel && typeof channel === "object" ? (channel as Record<string, unknown>).guild_id : undefined;
  if (typeof guildId !== "string" || !guildId.trim()) throw new Error("Discord channel info did not include a guild id");
  return guildId.trim();
}

export function parseDiscordChannelOptions(
  stdout: unknown,
  config: Record<string, unknown> = {},
): DiscordRouteOption[] {
  const parsed = parseOpenClawCommandJson(stdout);
  const payload = parsed.payload;
  const channels = payload && typeof payload === "object" ? (payload as Record<string, unknown>).channels : undefined;
  if (!Array.isArray(channels)) throw new Error("Discord channel list did not include channels");
  const aliases = new Map(configuredDiscordRouteOptions(config).map((option) => [option.route, option.alias]));
  return channels.flatMap((channel) => {
    if (!channel || typeof channel !== "object") return [];
    const item = channel as Record<string, unknown>;
    if (item.type !== 0 || typeof item.id !== "string" || typeof item.name !== "string") return [];
    const route = `channel:${item.id}`;
    const alias = aliases.get(route);
    return [{
      route,
      channel_id: item.id,
      guild_id: typeof item.guild_id === "string" ? item.guild_id : undefined,
      name: item.name,
      label: `#${item.name}`,
      ...(alias ? { alias } : {}),
    }];
  }).sort((left, right) => {
    if (left.alias === "notifications") return -1;
    if (right.alias === "notifications") return 1;
    return left.name.localeCompare(right.name);
  });
}

export async function discoverDiscordRouteOptions(
  runCommand: OpenClawCommandRunner | undefined,
  executable: string,
  config: Record<string, unknown>,
): Promise<{ discord_routes: DiscordRouteOption[]; default_discord_route?: string; warnings: string[] }> {
  const configured = configuredDiscordRouteOptions(config);
  const preferred = configured.find((option) => option.alias?.toLowerCase() === "notifications") ?? configured[0];
  if (!runCommand || !preferred) {
    return {
      discord_routes: configured,
      default_discord_route: preferred?.route,
      warnings: [!runCommand ? "OpenClaw channel discovery is unavailable." : "Configure a Discord route alias to discover its guild channels."],
    };
  }
  try {
    const info = await runOpenClawCommand(runCommand, executable, [
      "message", "channel", "info", "--channel", "discord", "--target", preferred.route, "--json",
    ], 30_000);
    if (typeof info.code === "number" && info.code !== 0) throw new Error(commandOutputText(info.stderr) || `exit ${info.code}`);
    const guildId = parseDiscordGuildId(info.stdout);
    const listed = await runOpenClawCommand(runCommand, executable, [
      "message", "channel", "list", "--channel", "discord", "--guild-id", guildId, "--json",
    ], 30_000);
    if (typeof listed.code === "number" && listed.code !== 0) throw new Error(commandOutputText(listed.stderr) || `exit ${listed.code}`);
    const routes = parseDiscordChannelOptions(listed.stdout, config);
    return { discord_routes: routes, default_discord_route: preferred.route, warnings: [] };
  } catch (error) {
    return {
      discord_routes: configured,
      default_discord_route: preferred.route,
      warnings: [`Discord channel discovery failed; showing configured routes only: ${String(error)}`],
    };
  }
}

export async function deliverRegistrationFollowUp(
  result: RpcResult,
  runCommand: OpenClawCommandRunner | undefined,
  executable: string,
  warn: (message: string) => void = () => undefined,
  options: { agent?: string; model?: string; thinking?: string } = {},
): Promise<RpcResult> {
  const message = typeof result.follow_up_message === "string" ? result.follow_up_message : "";
  const planningPrompt = typeof result.number_one_prompt === "string" ? result.number_one_prompt : message;
  const route = typeof result.notification_route === "string" ? result.notification_route : "";
  if (!planningPrompt || !route) return result;
  if (!runCommand) return { ...result, notification_status: "unavailable" };
  const agent = options.agent || DEFAULT_NUMBER_ONE_AGENT;
  const model = options.model || DEFAULT_NUMBER_ONE_MODEL;
  const thinking = options.thinking || DEFAULT_NUMBER_ONE_THINKING;
  const sessionKey = typeof result.number_one_session_key === "string" && result.number_one_session_key.trim()
    ? result.number_one_session_key.trim()
    : "make-it-so:number-one:registration";
  try {
    const delivery = await runOpenClawCommand(runCommand, executable, [
      "agent", "--agent", agent, "--model", model, "--thinking", thinking,
      "--channel", "discord", "--deliver", "--reply-channel", "discord", "--reply-to", route,
      "--session-key", sessionKey, "--message", planningPrompt, "--json",
    ], NUMBER_ONE_TURN_TIMEOUT_MS);
    if (typeof delivery.code === "number" && delivery.code !== 0) {
      throw new Error(String(delivery.stderr ?? `openclaw exited with code ${delivery.code}`));
    }
    return {
      ...result,
      notification_status: "sent",
      notification_delivery: "number_one_agent",
      number_one_agent: agent,
      number_one_model: model,
      number_one_session_key: sessionKey,
    };
  } catch (agentError) {
    warn(
      `Make It So Number One planning turn failed (turn timeout ${NUMBER_ONE_TURN_TIMEOUT_MS}ms); ` +
      `using a direct Discord fallback: ${describeCommandError(agentError)}`,
    );
    try {
      const fallback = await runOpenClawCommand(runCommand, executable, [
        "message", "send", "--channel", "discord", "--target", route, "--message", planningPrompt, "--json",
      ], 90_000);
      if (typeof fallback.code === "number" && fallback.code !== 0) {
        throw new Error(String(fallback.stderr ?? `openclaw exited with code ${fallback.code}`));
      }
      return {
        ...result,
        notification_status: "sent",
        notification_delivery: "message_fallback",
        notification_error: `Number One agent failed: ${String(agentError)}`,
        number_one_session_key: sessionKey,
      };
    } catch (fallbackError) {
      warn(`Make It So registration planning handoff could not be sent: ${String(fallbackError)}`);
      return {
        ...result,
        notification_status: "failed",
        notification_error: `Number One: ${String(agentError)}; fallback: ${String(fallbackError)}`,
        number_one_session_key: sessionKey,
      };
    }
  }
}

export async function deliverNumberOneDiscordTurn(
  content: string,
  binding: DiscordPlanningBinding,
  runCommand: OpenClawCommandRunner | undefined,
  executable: string,
  agent: string,
  model: string,
  thinking: string,
): Promise<void> {
  if (!runCommand) throw new Error("OpenClaw command runtime is unavailable");
  const delivery = await runOpenClawCommand(runCommand, executable, [
    "agent", "--agent", agent, "--model", model, "--thinking", thinking,
    "--channel", "discord", "--deliver", "--reply-channel", "discord", "--reply-to", binding.route,
    "--session-key", binding.sessionKey, "--message", content, "--json",
  ], NUMBER_ONE_TURN_TIMEOUT_MS);
  if (typeof delivery.code === "number" && delivery.code !== 0) {
    throw new Error(String(delivery.stderr ?? `openclaw exited with code ${delivery.code}`));
  }
}

export async function deliverDiscordPlanningStatus(
  message: string,
  route: string,
  runCommand: OpenClawCommandRunner | undefined,
  executable: string,
): Promise<void> {
  if (!runCommand || !message.trim() || !route.trim()) return;
  const delivery = await runOpenClawCommand(runCommand, executable, [
    "message", "send", "--channel", "discord", "--target", route,
    "--message", message, "--json",
  ], 90_000);
  if (typeof delivery.code === "number" && delivery.code !== 0) {
    throw new Error(String(delivery.stderr ?? `openclaw exited with code ${delivery.code}`));
  }
}

function describeCommandError(error: unknown): string {
  if (error instanceof Error) {
    const message = error.message.trim();
    return message ? `${error.name}: ${message}` : error.name || "unknown host command error";
  }
  if (typeof error === "string" && error.trim()) return error.trim();
  try {
    const serialized = JSON.stringify(error);
    return serialized && serialized !== "{}" ? serialized : "unknown host command error";
  } catch {
    return "unknown host command error";
  }
}

export function parseRouteParams(raw: unknown): Record<string, unknown> {
  if (raw && typeof raw === "object" && !ArrayBuffer.isView(raw)) {
    return raw as Record<string, unknown>;
  }
  const text = typeof raw === "string"
    ? raw
    : ArrayBuffer.isView(raw)
      ? new TextDecoder().decode(new Uint8Array(raw.buffer, raw.byteOffset, raw.byteLength))
      : "";
  if (!text.trim()) return {};
  try {
    const parsed: unknown = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

export async function readRouteParams(request: unknown): Promise<Record<string, unknown>> {
  const body = request && typeof request === "object"
    ? (request as { body?: unknown }).body
    : undefined;
  if (body !== undefined) return parseRouteParams(body);

  const stream = request as AsyncIterable<unknown> | null;
  if (!stream || typeof stream[Symbol.asyncIterator] !== "function") return {};

  const chunks: string[] = [];
  for await (const chunk of stream) {
    if (typeof chunk === "string") {
      chunks.push(chunk);
    } else if (ArrayBuffer.isView(chunk)) {
      const view = chunk as ArrayBufferView;
      chunks.push(
        new TextDecoder().decode(new Uint8Array(view.buffer, view.byteOffset, view.byteLength)),
      );
    } else if (
      Object.prototype.toString.call(chunk) === "[object ArrayBuffer]"
      || Object.prototype.toString.call(chunk) === "[object SharedArrayBuffer]"
    ) {
      chunks.push(new TextDecoder().decode(new Uint8Array(chunk as ArrayBufferLike)));
    }
  }
  return parseRouteParams(chunks.join(""));
}

export default definePluginEntry({
  id: PLUGIN_ID,
  name: "Make It So",
  description: "An SDLC control plane that puts the builder in command of an agent crew.",
  configSchema: CONFIG_SCHEMA,
  register(api: Api) {
    const config = api.pluginConfig ?? {};
    const configPath = expandPath(configString(config, "configPath", "~/.config/make-it-so/config.yaml"));
    const sidecarLease = acquireSharedSidecar(
      {
        executable: configString(config, "pythonExecutable", "python3"),
        args: configArgs(config),
        configPath,
      },
      (message, error) => api.logger?.warn?.(`${message}${error ? `: ${String(error)}` : ""}`),
    );
    const sidecar = sidecarLease.supervisor;
    const executable = resolveOpenClawExecutable(config);
    const numberOneAgent = configString(config, "numberOneAgent", DEFAULT_NUMBER_ONE_AGENT);
    const numberOneModel = configString(config, "numberOneModel", DEFAULT_NUMBER_ONE_MODEL);
    const numberOneThinking = configString(config, "numberOneThinking", DEFAULT_NUMBER_ONE_THINKING);
    const autoPersistDiscordAnswers = configBoolean(config, "autoPersistDiscordAnswers", true);
    const readinessReviewHarness = configString(config, "readinessReviewHarness", "openclaw");
    const discordBotUserIds = new Set(
      Array.isArray(config.discordBotUserIds)
        ? config.discordBotUserIds.filter((value): value is string => typeof value === "string" && value.trim().length > 0).map((value) => value.trim())
        : DEFAULT_DISCORD_BOT_USER_IDS,
    );
    const discordPlanningBindings = new Map<string, DiscordPlanningBinding>();
    const handledDiscordMessageIds = new Set<string>();
    let discordBindingsLoadedAt = 0;
    const runCommand = api.runtime?.system?.runCommandWithTimeout;
    let discordRouteCache: { expiresAt: number; value: Awaited<ReturnType<typeof discoverDiscordRouteOptions>> } | undefined;
    let discordRouteRefresh: Promise<Awaited<ReturnType<typeof discoverDiscordRouteOptions>>> | undefined;
    const configuredRouteFallback = (() => {
      const routes = configuredDiscordRouteOptions(config);
      const preferred = routes.find((option) => option.alias?.toLowerCase() === "notifications") ?? routes[0];
      return { discord_routes: routes, default_discord_route: preferred?.route, warnings: [] as string[] };
    })();
    const refreshDiscordRouteCache = (): Promise<Awaited<ReturnType<typeof discoverDiscordRouteOptions>>> => {
      if (discordRouteCache && discordRouteCache.expiresAt > Date.now()) return Promise.resolve(discordRouteCache.value);
      if (discordRouteRefresh) return discordRouteRefresh;
      discordRouteRefresh = discoverDiscordRouteOptions(runCommand, executable, config).then((value) => {
        discordRouteCache = { expiresAt: Date.now() + 300_000, value };
        return value;
      }).finally(() => { discordRouteRefresh = undefined; });
      return discordRouteRefresh;
    };
    // Channel discovery is slow on some OpenClaw installations. Warm it during
    // plugin startup so opening registration never has to pay that full cost.
    void refreshDiscordRouteCache();
    const rememberDiscordPlanningBinding = (value: Record<string, unknown>): void => {
      const route = typeof value.notification_route === "string" ? value.notification_route :
        typeof value.route === "string" ? value.route : "";
      const repository = typeof value.repository === "string" ? value.repository :
        typeof value.full_name === "string" ? value.full_name : "";
      const sessionKey = typeof value.number_one_session_key === "string" ? value.number_one_session_key :
        typeof value.session_key === "string" ? value.session_key :
        repository ? `make-it-so:number-one:${repository.replaceAll("/", "-")}` : "";
      if (!route || !repository || !sessionKey) return;
      const binding = { repository, route, sessionKey };
      for (const key of discordRouteKeys(route)) discordPlanningBindings.set(key, binding);
    };
    const refreshDiscordPlanningBindings = async (): Promise<void> => {
      const now = Date.now();
      if (now - discordBindingsLoadedAt < 10_000) return;
      discordBindingsLoadedAt = now;
      try {
        const result = await sidecar.request("discord.planning_bindings");
        const rows = Array.isArray(result.bindings) ? result.bindings : [];
        for (const row of rows) {
          if (row && typeof row === "object") rememberDiscordPlanningBinding(row as Record<string, unknown>);
        }
      } catch (error) {
        api.logger?.warn?.(`Make It So could not refresh Discord planning bindings: ${String(error)}`);
      }
    };
    const findDiscordPlanningBinding = async (event: Record<string, unknown>, context: Record<string, unknown>): Promise<DiscordPlanningBinding | undefined> => {
      const values = [
        event.conversationId,
        event.parentConversationId,
        event.threadId,
        context.conversationId,
        context.channelId,
        (event.metadata as Record<string, unknown> | undefined)?.conversationId,
        (event.metadata as Record<string, unknown> | undefined)?.channelId,
      ];
      const direct = [...discordPlanningBindings.values()].find((binding) => discordPlanningRouteMatches(binding.route, values));
      if (direct) return direct;
      await refreshDiscordPlanningBindings();
      return [...discordPlanningBindings.values()].find((binding) => discordPlanningRouteMatches(binding.route, values));
    };
    const request = async (method: string, params: Record<string, unknown> = {}): Promise<RpcResult> => {
      const timeoutMs = method === "course.readiness_review" ? READINESS_REVIEW_TIMEOUT_MS : undefined;
      if (method === "registration.options") {
        const base = await sidecar.request(method, params, timeoutMs);
        const channels = discordRouteCache && discordRouteCache.expiresAt > Date.now()
          ? discordRouteCache.value
          : configuredRouteFallback;
        const discordDiscoveryPending = !discordRouteCache || discordRouteCache.expiresAt <= Date.now();
        if (discordDiscoveryPending) void refreshDiscordRouteCache();
        const baseWarnings = Array.isArray(base.warnings) ? base.warnings : [];
        return {
          ...base,
          ...channels,
          discord_discovery_pending: discordDiscoveryPending,
          warnings: [...baseWarnings, ...channels.warnings],
        };
      }
      if (method !== "repo.register") return sidecar.request(method, params, timeoutMs);
      const route = typeof params.notification_route === "string" ? params.notification_route : "";
      return sidecar.request(method, {
        ...params,
        notification_route: resolveDiscordRoute(route, config),
        notification_kind: "openclaw_discord",
        notification_executable: executable,
      }, timeoutMs);
    };
    const persistPendingReadinessQuestion = async (
      repository: string,
      courseKey: string,
      pending?: DiscordPendingReadinessQuestion,
    ): Promise<void> => {
      await request("course.pending_question", {
        full_name: repository,
        course_key: courseKey,
        requirement_key: pending?.key ?? "",
        question: pending?.question ?? "",
      });
    };
    const numberOneQuestionPrompt = (
      pending: DiscordPendingReadinessQuestion,
      context: string,
    ): string => [
      context,
      `The next durable readiness requirement is ${pending.key}.`,
      `Ask exactly this one question and no other question: ${pending.question}`,
      "Keep the course paused. Do not begin implementation.",
    ].join(" ");
    const sendRegistrationFollowUp = async (result: RpcResult): Promise<RpcResult> => {
      // Registering a repository is a control-plane write. Do not make the
      // dashboard wait for a six-minute model turn before returning the
      // durable registration result. Remember the binding first so a reply
      // typed while Number One is starting still has a route to its course.
      rememberDiscordPlanningBinding(result);
      if (!runCommand) return { ...result, notification_status: "unavailable" };
      void deliverRegistrationFollowUp(
        result,
        runCommand,
        executable,
        (message) => api.logger?.warn?.(message),
        { agent: numberOneAgent, model: numberOneModel, thinking: numberOneThinking },
      ).then((delivered) => {
        rememberDiscordPlanningBinding(delivered);
        const repoPayload = result.repo && typeof result.repo === "object"
          ? result.repo as Record<string, unknown>
          : undefined;
        api.logger?.info?.(
          `Make It So completed asynchronous Number One registration delivery for ` +
          `${String(repoPayload?.full_name ?? result.full_name ?? "repository")}: ` +
          `${String(delivered.notification_status ?? "unknown")}.`,
        );
      }).catch((error) => {
        api.logger?.error?.(`Make It So asynchronous Number One registration delivery failed: ${String(error)}`);
      });
      return {
        ...result,
        notification_status: "queued",
        notification_delivery: "number_one_agent_async",
        number_one_agent: numberOneAgent,
        number_one_model: numberOneModel,
      };
    };
    const controlUiToken = randomBytes(32).toString("base64url");

    api.session?.controls?.registerControlUiDescriptor?.({
      surface: "tab",
      id: PLUGIN_ID,
      label: "Make It So",
      description: "Set the course, inspect progress, and engage the crew.",
      icon: "rocket",
      group: "control",
      order: 70,
      path: "/make-it-so/",
      requiredScopes: ["operator.read"],
    });

    const uiRoot = join(api.rootDir ?? process.cwd(), "dist", "ui");
    api.registerHttpRoute?.({
      path: "/make-it-so/",
      auth: "plugin",
      handler: async (req, res) => {
        if (rejectNonControlUiRequest(req, res, { cors: false })) return;
        res.statusCode = 200;
        res.setHeader("content-type", "text/html; charset=utf-8");
        res.setHeader("cache-control", "no-store");
        res.setHeader("content-security-policy", "frame-ancestors 'self'");
        res.setHeader("x-content-type-options", "nosniff");
        res.end(`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="make-it-so-control-token" content="${controlUiToken}"><title>Make It So</title><link rel="stylesheet" crossorigin="anonymous" href="/make-it-so/assets/index.css?v=${UI_ASSET_VERSION}"></head><body><div id="root"></div><script type="module" crossorigin="anonymous" src="/make-it-so/assets/index.js?v=${UI_ASSET_VERSION}"></script></body></html>`);
      },
    });
    api.registerHttpRoute?.({
      path: "/make-it-so/assets/index.css",
      auth: "plugin",
      handler: async (req, res) => {
        if (rejectNonControlUiRequest(req, res)) return;
        try {
          const body = await readFile(join(uiRoot, "assets", "index.css"));
          res.statusCode = 200;
          res.setHeader("content-type", "text/css; charset=utf-8");
          res.setHeader("cache-control", "no-store");
          res.end(body);
        } catch (error) {
          res.statusCode = 503;
          res.end(`Make It So UI is not built: ${String(error)}`);
        }
      },
    });
    api.registerHttpRoute?.({
      path: "/make-it-so/assets/index.js",
      auth: "plugin",
      handler: async (req, res) => {
        if (rejectNonControlUiRequest(req, res)) return;
        try {
          const body = await readFile(join(uiRoot, "assets", "index.js"));
          res.statusCode = 200;
          res.setHeader("content-type", "text/javascript; charset=utf-8");
          res.setHeader("cache-control", "no-store");
          res.end(body);
        } catch (error) {
          res.statusCode = 503;
          res.end(`Make It So UI is not built: ${String(error)}`);
        }
      },
    });

    const apiRoute = (path: string, method: string, afterRequest?: (result: RpcResult) => Promise<RpcResult>) => {
      api.registerHttpRoute?.({
        path,
        auth: "plugin",
        handler: async (req, res) => {
          if (rejectNonControlUiRequest(req, res, { token: controlUiToken })) return;
          try {
            const params = await readRouteParams(req);
            const rawResult = await request(method, params);
            const result = afterRequest ? await afterRequest(rawResult) : rawResult;
            res.statusCode = 200;
            res.setHeader("content-type", "application/json; charset=utf-8");
            res.end(JSON.stringify(result));
          } catch (error) {
            res.statusCode = 500;
            res.setHeader("content-type", "application/json; charset=utf-8");
            res.end(JSON.stringify({ error: String(error) }));
          }
        },
      });
    };
    apiRoute("/make-it-so/api/portfolio/status", "portfolio.status");
    apiRoute("/make-it-so/api/repos/list", "repos.list");
    apiRoute("/make-it-so/api/registration/options", "registration.options");
    apiRoute("/make-it-so/api/repos/inspect", "repo.inspect");
    apiRoute("/make-it-so/api/repos/register", "repo.register", sendRegistrationFollowUp);
    apiRoute("/make-it-so/api/repos/create", "repo.create");
    apiRoute("/make-it-so/api/repos/update", "repo.update");
    apiRoute("/make-it-so/api/models/validate", "models.validate");
    apiRoute("/make-it-so/api/models/config", "models.config");
    apiRoute("/make-it-so/api/models/update", "models.update");
    apiRoute("/make-it-so/api/usage/config", "usage.config");
    apiRoute("/make-it-so/api/usage/update", "usage.update");
    apiRoute("/make-it-so/api/courses/list", "courses.list");
    apiRoute("/make-it-so/api/course/get", "course.get");
    apiRoute("/make-it-so/api/course/milestone-evidence", "course.milestone_evidence");
    apiRoute("/make-it-so/api/course/milestone-changes", "course.milestone_changes");
    apiRoute("/make-it-so/api/course/milestone-change-propose", "course.milestone_change_propose");
    apiRoute("/make-it-so/api/course/milestone-change-approve", "course.milestone_change_approve");
    apiRoute("/make-it-so/api/course/milestone-change-reject", "course.milestone_change_reject");
    apiRoute("/make-it-so/api/course/create", "course.create");
    apiRoute("/make-it-so/api/course/readiness", "course.readiness");
    apiRoute("/make-it-so/api/course/planning-session", "course.planning_session");
    apiRoute("/make-it-so/api/course/models", "course.models");
    apiRoute("/make-it-so/api/course/requirement", "course.requirement");
    apiRoute("/make-it-so/api/course/approve", "course.approve");
    apiRoute("/make-it-so/api/course/ready-work", "course.ready_work");
    apiRoute("/make-it-so/api/course/checkpoint", "course.checkpoint");
    apiRoute("/make-it-so/api/course/pause", "course.pause");
    apiRoute("/make-it-so/api/course/resume", "course.resume");
    apiRoute("/make-it-so/api/schedule/describe", "schedule.describe");
    apiRoute("/make-it-so/api/schedule/configure", "schedule.configure");
    apiRoute("/make-it-so/api/run/start", "run.start");
    apiRoute("/make-it-so/api/attention/ack", "attention.ack");

    api.registerTool?.({
      name: "make_it_so_course_status",
      label: "Make It So course status",
      description: "Read Make It So course readiness and work-package state.",
      parameters: { type: "object", properties: { full_name: { type: "string" }, course_key: { type: "string" } }, required: ["full_name", "course_key"] },
      execute: createToolExecutor(request, "course.get"),
    });
    api.registerTool?.({
      name: "make_it_so_resolve_checkpoint",
      label: "Make It So resolve checkpoint",
      description: "Record a checkpoint decision through Make It So policy.",
      parameters: {
        type: "object",
        properties: {
          full_name: { type: "string" },
          course_key: { type: "string" },
          checkpoint_key: { type: "string" },
          status: { type: "string", enum: ["approved", "blocked", "resolved", "waived"] },
          resolved_by: { type: "string" },
          evidence: { type: "array", items: { type: "string" } },
        },
        required: ["full_name", "course_key", "checkpoint_key", "status"],
      },
      execute: createToolExecutor(request, "course.checkpoint"),
    });
    api.registerTool?.({
      name: "make_it_so_answer_readiness",
      label: "Make It So answer readiness",
      description: "Record or verify a course readiness answer through Make It So.",
      parameters: {
        type: "object",
        properties: {
          full_name: { type: "string" },
          course_key: { type: "string" },
          requirement_key: { type: "string" },
          status: { type: "string", enum: ["answered", "verified", "waived"] },
          answer: { type: "string" },
          evidence: { type: "array", items: { type: "string" } },
          verified_by: { type: "string" },
          verified_at: { type: "string" },
          verification_model: { type: "string" },
        },
        required: ["full_name", "course_key", "requirement_key", "status"],
      },
      execute: createToolExecutor(request, "course.requirement"),
    });
    api.registerTool?.({
      name: "make_it_so_start_planning",
      label: "Make It So start planning",
      description: "Return the durable course context and next questions for a native OpenClaw planning conversation.",
      parameters: {
        type: "object",
        properties: { full_name: { type: "string" }, course_key: { type: "string" } },
        required: ["full_name", "course_key"],
      },
      execute: createToolExecutor(request, "course.planning_session"),
    });
    api.registerTool?.({
      name: "make_it_so_review_readiness",
      label: "Make It So review readiness",
      description: "Run the independent Number One readiness review before course approval.",
      parameters: {
        type: "object",
        properties: {
          full_name: { type: "string" },
          course_key: { type: "string" },
          harness: { type: "string", enum: ["openclaw", "codex"] },
        },
        required: ["full_name", "course_key", "harness"],
      },
      execute: createToolExecutor(request, "course.readiness_review"),
    });
    api.registerTool?.({
      name: "make_it_so_ready_work",
      label: "Make It So ready work",
      description: "List dependency-ready work packages for an approved course.",
      parameters: { type: "object", properties: { full_name: { type: "string" }, course_key: { type: "string" } }, required: ["full_name", "course_key"] },
      execute: createToolExecutor(request, "course.ready_work"),
    });
    api.registerTool?.({
      name: "make_it_so_approve_course",
      label: "Make It So approve course",
      description: "Record explicit owner approval for a course after readiness and planning are complete.",
      parameters: { type: "object", properties: { full_name: { type: "string" }, course_key: { type: "string" }, approved_by: { type: "string" } }, required: ["full_name", "course_key"] },
      execute: createToolExecutor(request, "course.approve"),
    });

    api.registerHook?.(
      ["workboard.card.updated", "workboard.card.completed", "workboard.card.blocked"],
      async () => {
        try {
          await request("run.once", { kind: "reconcile" });
        } catch (error) {
          api.logger?.warn?.(`Workboard event reconciliation failed: ${String(error)}`);
        }
      },
      {
        name: "make-it-so-workboard-reconciliation",
        description: "Reconcile Make It So when an OpenClaw Workboard card changes.",
      },
    );

    const handleDiscordPlanningEvent = async (
      event: Record<string, unknown>,
      context: Record<string, unknown>,
    ): Promise<{ handled: true } | undefined> => {
        const content = typeof event.bodyForAgent === "string" ? event.bodyForAgent :
          typeof event.content === "string" ? event.content : "";
        if (!content.trim()) return;
        const senderId = typeof event.senderId === "string" ? event.senderId : "";
        const senderName = typeof event.senderName === "string" ? event.senderName.trim().toLowerCase() : "";
        if ((senderId && discordBotUserIds.has(senderId)) || senderName === "tars bot") {
          api.logger?.info?.(
            `Make It So ignored bot-authored Discord planning event sender=${senderId || senderName || "unknown"}.`,
          );
          return;
        }
        api.logger?.info?.(
          `Make It So observed planning hook event channel=${String(event.channel ?? "unknown")} ` +
          `conversation=${String(event.conversationId ?? context.conversationId ?? "unknown")}.`,
        );
        const binding = await findDiscordPlanningBinding(event, context);
        if (!binding) return;
        const messageKey = discordPlanningEventKey(event, context, content);
        if (messageKey && handledDiscordMessageIds.has(messageKey)) return { handled: true };
        if (messageKey) {
          handledDiscordMessageIds.add(messageKey);
          if (handledDiscordMessageIds.size > 256) {
            const oldest = handledDiscordMessageIds.values().next().value;
            if (typeof oldest === "string") handledDiscordMessageIds.delete(oldest);
          }
        }
        api.logger?.info?.(
          `Make It So intercepted Discord planning reply for ${binding.repository} ` +
          `(conversation=${String(event.conversationId ?? context.conversationId ?? "unknown")}).`,
        );
        const processDiscordPlanningReply = async (): Promise<void> => {
          try {
          const listed = await request("courses.list");
          const rows = Array.isArray(listed.courses) ? listed.courses : [];
          const matching = rows.find((row) => {
            if (!row || typeof row !== "object") return false;
            const item = row as Record<string, unknown>;
            if (item.repository !== binding.repository) return false;
            const course = item.course;
            if (!course || typeof course !== "object") return false;
            const status = String((course as Record<string, unknown>).status ?? "").toLowerCase();
            return isDiscordPlanningCourseStatus(status);
          });
          const course = matching && typeof matching === "object"
            ? (matching as Record<string, unknown>).course
            : undefined;
          const courseKey = course && typeof course === "object"
            ? String((course as Record<string, unknown>).key ?? "").trim()
            : "";
          let numberOneMessage = content;
          if (!courseKey) {
            api.logger?.warn?.(`Make It So could not map the Discord reply for ${binding.repository} to a pending course.`);
          } else if (parseDiscordCourseApproval(content) === "approve") {
            // Approval is a gate after the independent readiness review, not a
            // substitute for it. Run the review before attempting engagement.
            if (runCommand) {
              await runOpenClawCommand(runCommand, executable, [
                "message", "send", "--channel", "discord", "--target", binding.route,
                "--message", "Number One is checking the readiness gate now. I will ask the next missing decision here before any work begins.", "--json",
              ], 90_000);
            }
            const readiness = await request("course.readiness", {
              full_name: binding.repository,
              course_key: courseKey,
            });
            const report = readiness.readiness as Record<string, unknown> | undefined;
            if (!report || typeof report !== "object" || report.ready !== true) {
              const reviewed = await request("course.readiness_review", {
                full_name: binding.repository,
                course_key: courseKey,
                harness: readinessReviewHarness,
              });
              const reviewedReport = reviewed.readiness as Record<string, unknown> | undefined;
              if (!reviewedReport || reviewedReport.ready !== true) {
                const nextQuestion = selectDiscordReadinessQuestion(reviewed.course, reviewedReport);
                await persistPendingReadinessQuestion(binding.repository, courseKey, nextQuestion);
                numberOneMessage = nextQuestion
                  ? numberOneQuestionPrompt(
                    nextQuestion,
                    "The owner approved the proposed course, but the independent readiness review still found a missing decision.",
                  )
                  : "The independent readiness review is not ready but did not return a usable next question. Keep the course paused and report this planning blocker without asking the owner to repeat prior answers.";
                api.logger?.info?.(`Make It So kept ${binding.repository}/${courseKey} in readiness review after the independent review found unresolved requirements.`);
              } else {
                await persistPendingReadinessQuestion(binding.repository, courseKey);
                await request("course.approve", {
                  full_name: binding.repository,
                  course_key: courseKey,
                  approved_by: senderId || senderName || "discord-owner",
                });
                api.logger?.info?.(`Make It So recorded Discord course approval for ${binding.repository}/${courseKey}.`);
              }
            } else {
              await persistPendingReadinessQuestion(binding.repository, courseKey);
              await request("course.approve", {
                full_name: binding.repository,
                course_key: courseKey,
                approved_by: senderId || senderName || "discord-owner",
              });
              api.logger?.info?.(`Make It So recorded Discord course approval for ${binding.repository}/${courseKey}.`);
            }
            await deliverNumberOneDiscordTurn(
              numberOneMessage,
              binding,
              runCommand,
              executable,
              numberOneAgent,
              numberOneModel,
              numberOneThinking,
            );
            api.logger?.info?.(`Make It So routed Discord approval/planning handoff for ${binding.repository} to Number One.`);
            return;
          } else if (autoPersistDiscordAnswers) {
            // Reload the canonical course after list discovery. The list payload
            // is intentionally optimized for the dashboard and can be stale or
            // omit mutable readiness details during a concurrent migration.
            const detailed = await request("course.get", {
              full_name: binding.repository,
              course_key: courseKey,
            });
            const pendingQuestion = discordPendingReadinessQuestion(detailed.course)
              ?? discordPendingReadinessQuestion(course);
            const requirementKey = pendingQuestion?.key;
            if (requirementKey && content.trim()) {
              await request("course.requirement", {
                full_name: binding.repository,
                course_key: courseKey,
                requirement_key: requirementKey,
                status: "answered",
                answer: content.trim(),
                append_answer: true,
                evidence: ["discord-owner-answer"],
              });
              api.logger?.info?.(
                `Make It So recorded the conversational Discord answer for ` +
                `${binding.repository}/${courseKey}/${requirementKey}.`,
              );

              // A readiness review can take several minutes. Acknowledge the
              // answer before starting it so the conversational flow never
              // appears to have swallowed the owner's reply.
              try {
                await deliverDiscordPlanningStatus(
                  `Number One received your answer for ${requirementKey}. I am checking the readiness gate now; I will ask the next decision here when that review finishes. No implementation has started.`,
                  binding.route,
                  runCommand,
                  executable,
                );
              } catch (statusError) {
                api.logger?.warn?.(
                  `Make It So readiness acknowledgement failed for ${binding.repository}: ${describeCommandError(statusError)}`,
                );
              }

              // Do the expensive independent review in the detached plugin
              // workflow. Calling this from Number One as an agent tool hits
              // OpenClaw's 90-second per-tool watchdog even when the review
              // itself is healthy and eventually completes.
              const reviewed = await request("course.readiness_review", {
                full_name: binding.repository,
                course_key: courseKey,
                harness: readinessReviewHarness,
              });
              const reviewedReport = reviewed.readiness as Record<string, unknown> | undefined;
              if (reviewedReport?.ready === true) {
                await persistPendingReadinessQuestion(binding.repository, courseKey);
                numberOneMessage = [
                  `The owner answered readiness requirement ${requirementKey}.`,
                  "The independent readiness review completed successfully and now reports READY.",
                  "Continue the existing Number One conversation: the owner previously approved this course, so reconcile that approval with the now-ready review, record course approval if it still applies, and then select the sole eligible work package and start the live implementation flow.",
                ].join(" ");
              } else {
                const nextQuestion = selectDiscordReadinessQuestion(reviewed.course, reviewedReport);
                await persistPendingReadinessQuestion(binding.repository, courseKey, nextQuestion);
                numberOneMessage = nextQuestion
                  ? numberOneQuestionPrompt(
                    nextQuestion,
                    `The owner answered readiness requirement ${requirementKey}, but the fresh independent review still needs one decision.`,
                  )
                  : `The owner answered readiness requirement ${requirementKey}, but the independent review returned no usable next question. Keep the course paused and report the planning blocker without reclassifying the owner's answer.`;
              }
            } else {
              api.logger?.info?.(
                `Make It So forwarded a Discord planning turn without persisting it because ` +
                `no pending readiness requirement exists for ${binding.repository}/${courseKey}.`,
              );
              numberOneMessage = "There is no durable pending readiness question for this reply. Keep the course paused, do not classify or apply the owner's message, and explain that the planning conversation must be re-established before another answer can be accepted.";
            }
          }
          await deliverNumberOneDiscordTurn(
            numberOneMessage,
            binding,
            runCommand,
            executable,
            numberOneAgent,
            numberOneModel,
            numberOneThinking,
          );
          api.logger?.info?.(`Make It So routed Discord planning reply for ${binding.repository} to Number One.`);
          } catch (error) {
          api.logger?.error?.(
            `Make It So Number One Discord reply failed for ${binding.repository} ` +
            `(turn timeout ${NUMBER_ONE_TURN_TIMEOUT_MS}ms): ${describeCommandError(error)}`,
          );
          if (runCommand) {
            try {
              await runOpenClawCommand(runCommand, executable, [
                "message", "send", "--channel", "discord", "--target", binding.route,
                "--message", "Number One could not process that planning reply. The course remains paused; I am retrying the planning route.", "--json",
              ], 90_000);
            } catch (fallbackError) {
              api.logger?.error?.(`Make It So Discord planning error notice failed: ${String(fallbackError)}`);
            }
          }
          }
        };
        // Do not hold OpenClaw's inbound hook open for a model turn. The
        // durable answer has already been captured, and this background task
        // owns the eventual Number One response or an explicit error notice.
        void processDiscordPlanningReply();
        return { handled: true };
    };

    api.logger?.info?.(`Make It So typed hook API available: ${typeof api.on}`);
    const discordPlanningHookOptions = {
      name: "make-it-so-discord-number-one-planning",
      description: "Route mapped Discord planning replies to the durable Number One session.",
      priority: 100,
      timeoutMs: 210_000,
    };
    api.on?.(
      "inbound_claim",
      handleDiscordPlanningEvent,
      discordPlanningHookOptions,
    );
    api.on?.(
      "before_dispatch",
      handleDiscordPlanningEvent,
      {
        name: "make-it-so-discord-number-one-planning-before-dispatch",
        description: "Fallback claim for mapped Discord planning replies before ordinary agent dispatch.",
        priority: 100,
        timeoutMs: 210_000,
      },
    );

    const gateway = (
      name: string,
      method: string,
      scope: "operator.read" | "operator.write" | "operator.admin" = "operator.read",
    ) => {
      api.registerGatewayMethod?.(name, async ({ respond, params }) => {
        try {
          const result = await request(method, params ?? {});
          respond(true, result);
        } catch (error) {
          respond(false, { error: String(error) });
        }
      }, { scope });
    };
    gateway("makeItSo.health", "health");
    gateway("makeItSo.portfolio.status", "portfolio.status");
    gateway("makeItSo.repos.list", "repos.list");
    gateway("makeItSo.registration.options", "registration.options");
    gateway("makeItSo.repos.inspect", "repo.inspect");
    gateway("makeItSo.repos.register", "repo.register", "operator.write");
    gateway("makeItSo.repos.create", "repo.create", "operator.write");
    gateway("makeItSo.repos.update", "repo.update", "operator.write");
    gateway("makeItSo.models.validate", "models.validate");
    gateway("makeItSo.models.config", "models.config");
    gateway("makeItSo.models.update", "models.update", "operator.write");
    gateway("makeItSo.usage.config", "usage.config");
    gateway("makeItSo.usage.update", "usage.update", "operator.write");
    gateway("makeItSo.courses.list", "courses.list");
    gateway("makeItSo.course.get", "course.get");
    gateway("makeItSo.course.create", "course.create", "operator.write");
    gateway("makeItSo.course.readiness", "course.readiness");
    gateway("makeItSo.course.planningSession", "course.planning_session");
    gateway("makeItSo.course.models", "course.models", "operator.write");
    gateway("makeItSo.course.requirement", "course.requirement", "operator.write");
    gateway("makeItSo.course.approve", "course.approve", "operator.write");
    gateway("makeItSo.course.readyWork", "course.ready_work");
    gateway("makeItSo.course.checkpoint", "course.checkpoint", "operator.write");
    gateway("makeItSo.course.pause", "course.pause", "operator.write");
    gateway("makeItSo.course.resume", "course.resume", "operator.write");
    gateway("makeItSo.schedule.describe", "schedule.describe");
    gateway("makeItSo.schedule.configure", "schedule.configure", "operator.admin");
    gateway("makeItSo.runNow", "run.start", "operator.admin");
    gateway("makeItSo.attention.ack", "attention.ack", "operator.write");
    const scheduleDefinitions = async (): Promise<ScheduleDefinition[]> => {
      const description = await request("schedule.describe");
      return Array.isArray(description.jobs)
        ? description.jobs.filter((job): job is ScheduleDefinition => {
            return Boolean(
              job &&
                typeof job === "object" &&
                typeof (job as { name?: unknown }).name === "string" &&
                typeof (job as { every?: unknown }).every === "string" &&
                typeof (job as { kind?: unknown }).kind === "string" &&
                Array.isArray((job as { command?: unknown }).command),
            );
          })
        : [];
    };
    const invokeCron = async (args: string[]): Promise<CommandResult> => {
      if (!runCommand) throw new Error("OpenClaw command runtime is unavailable");
      const result = (await runOpenClawCommand(runCommand, executable, args)) as CommandResult;
      if (typeof result?.code === "number" && result.code !== 0) {
        throw new Error(String(result.stderr ?? `openclaw exited with code ${result.code}`));
      }
      return result ?? {};
    };
    const liveCronJobs = async () => {
      const listed = await invokeCron(cronListArgs());
      return parseCronJobs(String(listed.stdout ?? ""));
    };
    const scheduleStatus = async (): Promise<{ status: string; jobs: unknown[] }> => {
      const definitions = await scheduleDefinitions();
      const jobs = await liveCronJobs();
      const pythonExecutable = configString(config, "pythonExecutable", "python3");
      return {
        status: "inspected",
        jobs: definitions.map((definition) => {
          const inspection = inspectCronJob(jobs, definition, pythonExecutable, configPath);
          return {
            name: definition.name,
            every: definition.every,
            id: inspection.primary ? cronIdentifier(inspection.primary) : null,
            enabled: inspection.enabled,
            health: !inspection.primary ? "missing" : inspection.duplicates.length ? "duplicate" : inspection.drift.length ? "drifted" : inspection.enabled ? "healthy" : "paused",
            drift: inspection.drift,
            duplicates: inspection.duplicates.length,
          };
        }),
      };
    };
    const reconcileSchedules = async (): Promise<{ status: string; jobs: unknown[] }> => {
      if (config.installSchedules !== true) throw new Error("schedule management is disabled in plugin configuration");
      const jobs = await scheduleDefinitions();
      const cronJobs = await liveCronJobs();
      const installed: unknown[] = [];
      const pythonExecutable = configString(config, "pythonExecutable", "python3");
      for (const job of jobs) {
        const inspection = inspectCronJob(cronJobs, job, pythonExecutable, configPath);
        for (const duplicate of inspection.duplicates) {
          await invokeCron(["cron", "rm", cronIdentifier(duplicate)]);
        }
        if (!inspection.primary) {
          const result = await invokeCron(buildCronAddArgs(job, pythonExecutable, configPath, dirname(configPath)));
          installed.push({ name: job.name, status: "created", result: result.stdout ?? result });
        } else {
          const id = cronIdentifier(inspection.primary);
          if (inspection.drift.length) {
            await invokeCron(buildCronEditArgs(id, job, pythonExecutable, configPath, dirname(configPath)));
          }
          installed.push({ name: job.name, id, enabled: inspection.enabled, status: inspection.drift.length ? "updated" : "unchanged", removed_duplicates: inspection.duplicates.length });
        }
      }
      return { status: "reconciled", jobs: installed };
    };

    const mutateSchedules = async (action: "pause" | "resume" | "remove", name?: string) => {
      if (config.installSchedules !== true) throw new Error("schedule management is disabled in plugin configuration");
      if (action === "resume") await reconcileSchedules();
      const definitions = await scheduleDefinitions();
      const selected = name ? definitions.filter((item) => item.name === name) : definitions;
      if (!selected.length) throw new Error(`unknown Make It So schedule: ${name}`);
      const jobs = await liveCronJobs();
      const results: unknown[] = [];
      for (const definition of selected) {
        const matches = jobs.filter((job) => job.name === definition.name);
        for (const job of matches) {
          const id = cronIdentifier(job);
          await invokeCron(["cron", action === "remove" ? "rm" : action === "pause" ? "disable" : "enable", id]);
          results.push({ name: definition.name, id, status: action === "pause" ? "paused" : action === "resume" ? "enabled" : "removed" });
        }
      }
      return { status: action, jobs: results };
    };

    api.registerGatewayMethod?.("makeItSo.schedule.install", async ({ respond }) => {
      try {
        respond(true, await reconcileSchedules());
      } catch (error) {
        respond(false, { error: String(error) });
      }
    }, { scope: "operator.admin" });
    api.registerGatewayMethod?.("makeItSo.schedule.status", async ({ respond }) => {
      try { respond(true, await scheduleStatus()); } catch (error) { respond(false, { error: String(error) }); }
    }, { scope: "operator.read" });
    for (const action of ["pause", "resume", "remove"] as const) {
      api.registerGatewayMethod?.(`makeItSo.schedule.${action}`, async ({ respond, params }) => {
        try { respond(true, await mutateSchedules(action, typeof params?.name === "string" ? params.name : undefined)); }
        catch (error) { respond(false, { error: String(error) }); }
      }, { scope: "operator.admin" });
    }

    api.registerHttpRoute?.({
      path: "/make-it-so/api/schedule/install",
      auth: "plugin",
      handler: async (req, res) => {
        if (rejectNonControlUiRequest(req, res, { token: controlUiToken })) return;
        try {
          const result = await reconcileSchedules();
          res.statusCode = 200;
          res.setHeader("content-type", "application/json; charset=utf-8");
          res.end(JSON.stringify(result));
        } catch (error) {
          res.statusCode = 500;
          res.end(JSON.stringify({ error: String(error) }));
        }
      },
    });

    const scheduleRoute = (path: string, operation: (params: Record<string, unknown>) => Promise<unknown>) => {
      api.registerHttpRoute?.({
        path,
        auth: "plugin",
        handler: async (req, res) => {
          if (rejectNonControlUiRequest(req, res, { token: controlUiToken })) return;
          try {
            const params = req?.body && typeof req.body === "object" ? req.body : {};
            const result = await operation(params);
            res.statusCode = 200;
            res.setHeader("content-type", "application/json; charset=utf-8");
            res.end(JSON.stringify(result));
          } catch (error) {
            res.statusCode = 500;
            res.setHeader("content-type", "application/json; charset=utf-8");
            res.end(JSON.stringify({ error: String(error) }));
          }
        },
      });
    };
    scheduleRoute("/make-it-so/api/schedule/status", async () => scheduleStatus());
    scheduleRoute("/make-it-so/api/schedule/pause", async (params) => mutateSchedules("pause", typeof params.name === "string" ? params.name : undefined));
    scheduleRoute("/make-it-so/api/schedule/resume", async (params) => mutateSchedules("resume", typeof params.name === "string" ? params.name : undefined));
    scheduleRoute("/make-it-so/api/schedule/remove", async (params) => mutateSchedules("remove", typeof params.name === "string" ? params.name : undefined));
    scheduleRoute("/make-it-so/api/schedule/edit", async (params) => {
      await request("schedule.configure", params);
      return reconcileSchedules();
    });

    api.registerCommand?.({
      name: "make-it-so",
      description: "Inspect and control Make It So courses",
      acceptsArgs: true,
      requireAuth: true,
      requiredScopes: ["operator.read"],
      exposeSenderIsOwner: true,
      handler: async ({ args, senderId, senderIsOwner, gatewayClientScopes }) => {
        const parts = (args ?? "status").trim().split(/\s+/).filter(Boolean);
        const action = parts.shift()?.toLowerCase() ?? "status";
        const actor = senderId ?? "openclaw-owner";
        let result: RpcResult;
        if (action === "status") {
          result = await request("portfolio.status");
          const repos = Array.isArray(result.repos) ? result.repos : [];
          const selected = parts[0] ? repos.filter((item) => typeof item === "object" && item !== null && (item as { full_name?: unknown }).full_name === parts[0]) : repos;
          const lines = selected.map((item) => {
            const repo = item as Record<string, unknown>;
            return `${repo.full_name}: ${repo.state} | ${repo.operation_mode} | ${repo.active_work ? "work active" : "idle"}`;
          });
          return { text: lines.length ? `Make It So status\n${lines.join("\n")}\nDashboard: /make-it-so/` : "No matching repository is registered." };
        }
        const mayWrite = senderIsOwner === true || gatewayClientScopes?.some((scope) => scope === "operator.write" || scope === "operator.admin") === true;
        if (action !== "plan" && !mayWrite) return { text: "Make It So refused that mutation: owner or operator.write scope is required." };
        const fullName = parts.shift();
        const courseKey = parts.shift();
        if (action === "plan" && fullName && courseKey) result = await request("course.planning_session", { full_name: fullName, course_key: courseKey });
        else if (action === "approve" && fullName && courseKey) result = await request("course.approve", { full_name: fullName, course_key: courseKey, approved_by: actor });
        else if ((action === "pause" || action === "resume") && fullName && courseKey) result = await request(`course.${action}`, { full_name: fullName, course_key: courseKey });
        else if (action === "checkpoint" && fullName && courseKey && parts.length >= 2) result = await request("course.checkpoint", { full_name: fullName, course_key: courseKey, checkpoint_key: parts[0], status: parts[1], resolved_by: actor, evidence: ["openclaw-command"] });
        else if (action === "ack" && fullName && courseKey) result = await request("attention.ack", { full_name: fullName, fingerprint: courseKey, event_type: parts[0] });
        else return { text: "Usage: /make-it-so status [repo] | plan|approve|pause|resume <repo> <course> | checkpoint <repo> <course> <key> <status> | ack <repo> <fingerprint> [event]" };
        const status = String(result.status ?? result.interaction ?? "completed");
        return { text: `Make It So: ${action} ${status}. Dashboard: /make-it-so/` };
      },
    });

    api.registerCli?.(
      async ({ program }) => {
        const command = program.command("make-it-so").description("Set the course and inspect the agent crew");
        const cliAction = <Args extends unknown[]>(action: (...args: Args) => Promise<void>) =>
          withSidecarShutdown(sidecar, action);
        command.command("status").description("Show portfolio status").action(cliAction(async () => {
          const result = await request("portfolio.status");
          process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        }));
        command.command("schedules").description("Describe Make It So schedules").action(cliAction(async () => {
          const result = await scheduleStatus();
          process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        }));
        command.command("setup").description("Validate the sidecar and install managed schedules").action(cliAction(async () => {
          const health = await request("health");
          const schedules = await reconcileSchedules();
          process.stdout.write(`${JSON.stringify({ health, schedules }, null, 2)}\n`);
        }));
        command.command("diagnostics").description("Inspect sidecar and managed schedule health").action(cliAction(async () => {
          const health = await request("health");
          const schedules = await scheduleStatus();
          process.stdout.write(`${JSON.stringify({ health, schedules }, null, 2)}\n`);
        }));
        command.command("migration").description("Validate configuration compatibility without mutation").action(cliAction(async () => {
          const result = await request("health");
          process.stdout.write(`${JSON.stringify({ status: "compatible", ...result }, null, 2)}\n`);
        }));
        command.command("recovery").description("Run one bounded reconciliation pass").action(cliAction(async () => {
          const result = await request("run.once", { kind: "reconcile" });
          process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        }));
        const schedule = command.command("schedule").description("Manage OpenClaw Gateway schedules");
        schedule.command("status").action(cliAction(async () => { process.stdout.write(`${JSON.stringify(await scheduleStatus(), null, 2)}\n`); }));
        schedule.command("install").action(cliAction(async () => { process.stdout.write(`${JSON.stringify(await reconcileSchedules(), null, 2)}\n`); }));
        for (const action of ["pause", "resume", "remove"] as const) {
          schedule.command(`${action} [name]`).action(cliAction(async (name?: string) => { process.stdout.write(`${JSON.stringify(await mutateSchedules(action, name), null, 2)}\n`); }));
        }
        schedule.command("edit").option("--reconcile-every <duration>").option("--review-every <duration>").action(cliAction(async (options: { reconcileEvery?: string; reviewEvery?: string }) => {
          await request("schedule.configure", { reconcile_every: options.reconcileEvery, review_every: options.reviewEvery });
          process.stdout.write(`${JSON.stringify(await reconcileSchedules(), null, 2)}\n`);
        }));
        command.command("workboard <fullName> <board>").description("Configure the optional OpenClaw Workboard tracker").action(cliAction(async (fullName: string, board: string) => {
          const result = await request("repo.update", { full_name: fullName, orchestrator: "openclaw", orchestration_board: board });
          process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        }));
        command.command("plan <fullName> <courseKey>").description("Start a native planning conversation").action(cliAction(async (fullName: string, courseKey: string) => {
          const result = await request("course.planning_session", { full_name: fullName, course_key: courseKey });
          process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        }));
      },
      { descriptors: [{ name: "make-it-so", description: "Set the course and inspect the agent crew", hasSubcommands: true }] },
    );

    api.registerService?.({
      id: PLUGIN_ID,
      start: async () => {
        await sidecar.start();
        api.logger?.info?.("Make It So sidecar started");
      },
      stop: async () => {
        await sidecarLease.release();
        api.logger?.info?.("Make It So sidecar stopped");
      },
    });
  },
});
