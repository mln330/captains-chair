import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
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
    handler: (...args: any[]) => Promise<void>,
    opts?: { name?: string; description?: string },
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

const PLUGIN_ID = "captains-chair";
const CONFIG_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    configPath: { type: "string" },
    pythonExecutable: { type: "string", default: "python3" },
    sidecarCommand: { type: "array", items: { type: "string" }, default: ["-m", "captains_chair.sidecar"] },
    openclawExecutable: { type: "string", default: "openclaw" },
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

function configArgs(config: Record<string, unknown>): string[] {
  const value = config.sidecarCommand;
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? [...value]
    : ["-m", "captains_chair.sidecar"];
}

export default definePluginEntry({
  id: PLUGIN_ID,
  name: "Captain's Chair",
  description: "An SDLC control plane that puts the builder in command of an agent crew.",
  configSchema: CONFIG_SCHEMA,
  register(api: Api) {
    const config = api.pluginConfig ?? {};
    const configPath = expandPath(configString(config, "configPath", "~/.config/captains-chair/config.yaml"));
    const sidecar = new SidecarSupervisor(
      {
        executable: configString(config, "pythonExecutable", "python3"),
        args: configArgs(config),
        configPath,
      },
      (message, error) => api.logger?.warn?.(`${message}${error ? `: ${String(error)}` : ""}`),
    );
    const request = async (method: string, params?: Record<string, unknown>): Promise<RpcResult> =>
      sidecar.request(method, params);

    api.session?.controls?.registerControlUiDescriptor?.({
      surface: "tab",
      id: PLUGIN_ID,
      label: "Captain's Chair",
      description: "Set the course, inspect progress, and engage the crew.",
      icon: "compass",
      group: "control",
      order: 70,
      path: "/captains-chair/",
      requiredScopes: ["operator.read"],
    });

    const uiRoot = join(api.rootDir ?? process.cwd(), "dist", "ui");
    api.registerHttpRoute?.({
      path: "/captains-chair/",
      auth: "plugin",
      handler: async (req, res) => {
        if (rejectNonControlUiRequest(req, res)) return;
        res.statusCode = 200;
        res.setHeader("content-type", "text/html; charset=utf-8");
        res.end("<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Captain's Chair</title><link rel=\"stylesheet\" href=\"/captains-chair/assets/index.css\"></head><body><div id=\"root\"></div><script src=\"/captains-chair/assets/index.js\"></script></body></html>");
      },
    });
    api.registerHttpRoute?.({
      path: "/captains-chair/assets/index.css",
      auth: "plugin",
      handler: async (req, res) => {
        if (rejectNonControlUiRequest(req, res)) return;
        try {
          const body = await readFile(join(uiRoot, "assets", "index.css"));
          res.statusCode = 200;
          res.setHeader("content-type", "text/css; charset=utf-8");
          res.end(body);
        } catch (error) {
          res.statusCode = 503;
          res.end(`Captain's Chair UI is not built: ${String(error)}`);
        }
      },
    });
    api.registerHttpRoute?.({
      path: "/captains-chair/assets/index.js",
      auth: "plugin",
      handler: async (req, res) => {
        if (rejectNonControlUiRequest(req, res)) return;
        try {
          const body = await readFile(join(uiRoot, "assets", "index.js"));
          res.statusCode = 200;
          res.setHeader("content-type", "text/javascript; charset=utf-8");
          res.end(body);
        } catch (error) {
          res.statusCode = 503;
          res.end(`Captain's Chair UI is not built: ${String(error)}`);
        }
      },
    });

    const apiRoute = (path: string, method: string) => {
      api.registerHttpRoute?.({
        path,
        auth: "plugin",
        handler: async (req, res) => {
          if (rejectNonControlUiRequest(req, res)) return;
          try {
            const params = req?.body && typeof req.body === "object" ? req.body : {};
            const result = await request(method, params);
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
    apiRoute("/captains-chair/api/portfolio/status", "portfolio.status");
    apiRoute("/captains-chair/api/repos/list", "repos.list");
    apiRoute("/captains-chair/api/repos/register", "repo.register");
    apiRoute("/captains-chair/api/repos/create", "repo.create");
    apiRoute("/captains-chair/api/repos/update", "repo.update");
    apiRoute("/captains-chair/api/models/validate", "models.validate");
    apiRoute("/captains-chair/api/models/config", "models.config");
    apiRoute("/captains-chair/api/models/update", "models.update");
    apiRoute("/captains-chair/api/usage/config", "usage.config");
    apiRoute("/captains-chair/api/usage/update", "usage.update");
    apiRoute("/captains-chair/api/courses/list", "courses.list");
    apiRoute("/captains-chair/api/course/get", "course.get");
    apiRoute("/captains-chair/api/course/create", "course.create");
    apiRoute("/captains-chair/api/course/readiness", "course.readiness");
    apiRoute("/captains-chair/api/course/planning-session", "course.planning_session");
    apiRoute("/captains-chair/api/course/models", "course.models");
    apiRoute("/captains-chair/api/course/requirement", "course.requirement");
    apiRoute("/captains-chair/api/course/approve", "course.approve");
    apiRoute("/captains-chair/api/course/ready-work", "course.ready_work");
    apiRoute("/captains-chair/api/course/checkpoint", "course.checkpoint");
    apiRoute("/captains-chair/api/course/pause", "course.pause");
    apiRoute("/captains-chair/api/course/resume", "course.resume");
    apiRoute("/captains-chair/api/schedule/describe", "schedule.describe");
    apiRoute("/captains-chair/api/schedule/configure", "schedule.configure");
    apiRoute("/captains-chair/api/attention/ack", "attention.ack");

    api.registerTool?.({
      name: "captains_chair_course_status",
      description: "Read Captain's Chair course readiness and work-package state.",
      parameters: { type: "object", properties: { full_name: { type: "string" }, course_key: { type: "string" } }, required: ["full_name", "course_key"] },
      execute: async (params: Record<string, unknown>) => request("course.get", params),
    });
    api.registerTool?.({
      name: "captains_chair_resolve_checkpoint",
      description: "Record a checkpoint decision through Captain's Chair policy.",
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
      execute: async (params: Record<string, unknown>) => request("course.checkpoint", params),
    });
    api.registerTool?.({
      name: "captains_chair_answer_readiness",
      description: "Record or verify a course readiness answer through Captain's Chair.",
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
      execute: async (params: Record<string, unknown>) => request("course.requirement", params),
    });
    api.registerTool?.({
      name: "captains_chair_start_planning",
      description: "Return the durable course context and next questions for a native OpenClaw planning conversation.",
      parameters: {
        type: "object",
        properties: { full_name: { type: "string" }, course_key: { type: "string" } },
        required: ["full_name", "course_key"],
      },
      execute: async (params: Record<string, unknown>) => request("course.planning_session", params),
    });
    api.registerTool?.({
      name: "captains_chair_ready_work",
      description: "List dependency-ready work packages for an approved course.",
      parameters: { type: "object", properties: { full_name: { type: "string" }, course_key: { type: "string" } }, required: ["full_name", "course_key"] },
      execute: async (params: Record<string, unknown>) => request("course.ready_work", params),
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
        name: "captains-chair-workboard-reconciliation",
        description: "Reconcile Captain's Chair when an OpenClaw Workboard card changes.",
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
    gateway("captainsChair.health", "health");
    gateway("captainsChair.portfolio.status", "portfolio.status");
    gateway("captainsChair.repos.list", "repos.list");
    gateway("captainsChair.repos.register", "repo.register", "operator.write");
    gateway("captainsChair.repos.create", "repo.create", "operator.write");
    gateway("captainsChair.repos.update", "repo.update", "operator.write");
    gateway("captainsChair.models.validate", "models.validate");
    gateway("captainsChair.models.config", "models.config");
    gateway("captainsChair.models.update", "models.update", "operator.write");
    gateway("captainsChair.usage.config", "usage.config");
    gateway("captainsChair.usage.update", "usage.update", "operator.write");
    gateway("captainsChair.courses.list", "courses.list");
    gateway("captainsChair.course.get", "course.get");
    gateway("captainsChair.course.create", "course.create", "operator.write");
    gateway("captainsChair.course.readiness", "course.readiness");
    gateway("captainsChair.course.planningSession", "course.planning_session");
    gateway("captainsChair.course.models", "course.models", "operator.write");
    gateway("captainsChair.course.requirement", "course.requirement", "operator.write");
    gateway("captainsChair.course.approve", "course.approve", "operator.write");
    gateway("captainsChair.course.readyWork", "course.ready_work");
    gateway("captainsChair.course.checkpoint", "course.checkpoint", "operator.write");
    gateway("captainsChair.course.pause", "course.pause", "operator.write");
    gateway("captainsChair.course.resume", "course.resume", "operator.write");
    gateway("captainsChair.schedule.describe", "schedule.describe");
    gateway("captainsChair.schedule.configure", "schedule.configure", "operator.admin");
    gateway("captainsChair.attention.ack", "attention.ack", "operator.write");
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
    const executable = configString(config, "openclawExecutable", "openclaw");
    const runCommand = api.runtime?.system?.runCommandWithTimeout;
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
      if (!selected.length) throw new Error(`unknown Captain's Chair schedule: ${name}`);
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

    api.registerGatewayMethod?.("captainsChair.schedule.install", async ({ respond }) => {
      try {
        respond(true, await reconcileSchedules());
      } catch (error) {
        respond(false, { error: String(error) });
      }
    }, { scope: "operator.admin" });
    api.registerGatewayMethod?.("captainsChair.schedule.status", async ({ respond }) => {
      try { respond(true, await scheduleStatus()); } catch (error) { respond(false, { error: String(error) }); }
    }, { scope: "operator.read" });
    for (const action of ["pause", "resume", "remove"] as const) {
      api.registerGatewayMethod?.(`captainsChair.schedule.${action}`, async ({ respond, params }) => {
        try { respond(true, await mutateSchedules(action, typeof params?.name === "string" ? params.name : undefined)); }
        catch (error) { respond(false, { error: String(error) }); }
      }, { scope: "operator.admin" });
    }

    api.registerHttpRoute?.({
      path: "/captains-chair/api/schedule/install",
      auth: "gateway",
      gatewayRuntimeScopeSurface: "trusted-operator",
      handler: async (_req, res) => {
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
        auth: "gateway",
        gatewayRuntimeScopeSurface: "trusted-operator",
        handler: async (req, res) => {
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
    scheduleRoute("/captains-chair/api/schedule/status", async () => scheduleStatus());
    scheduleRoute("/captains-chair/api/schedule/pause", async (params) => mutateSchedules("pause", typeof params.name === "string" ? params.name : undefined));
    scheduleRoute("/captains-chair/api/schedule/resume", async (params) => mutateSchedules("resume", typeof params.name === "string" ? params.name : undefined));
    scheduleRoute("/captains-chair/api/schedule/remove", async (params) => mutateSchedules("remove", typeof params.name === "string" ? params.name : undefined));
    scheduleRoute("/captains-chair/api/schedule/edit", async (params) => {
      await request("schedule.configure", params);
      return reconcileSchedules();
    });

    api.registerCommand?.({
      name: "captains-chair",
      description: "Inspect and control Captain's Chair courses",
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
          return { text: lines.length ? `Captain's Chair status\n${lines.join("\n")}\nDashboard: /captains-chair/` : "No matching repository is registered." };
        }
        const mayWrite = senderIsOwner === true || gatewayClientScopes?.some((scope) => scope === "operator.write" || scope === "operator.admin") === true;
        if (action !== "plan" && !mayWrite) return { text: "Captain's Chair refused that mutation: owner or operator.write scope is required." };
        const fullName = parts.shift();
        const courseKey = parts.shift();
        if (action === "plan" && fullName && courseKey) result = await request("course.planning_session", { full_name: fullName, course_key: courseKey });
        else if (action === "approve" && fullName && courseKey) result = await request("course.approve", { full_name: fullName, course_key: courseKey, approved_by: actor });
        else if ((action === "pause" || action === "resume") && fullName && courseKey) result = await request(`course.${action}`, { full_name: fullName, course_key: courseKey });
        else if (action === "checkpoint" && fullName && courseKey && parts.length >= 2) result = await request("course.checkpoint", { full_name: fullName, course_key: courseKey, checkpoint_key: parts[0], status: parts[1], resolved_by: actor, evidence: ["openclaw-command"] });
        else if (action === "ack" && fullName && courseKey) result = await request("attention.ack", { full_name: fullName, fingerprint: courseKey, event_type: parts[0] });
        else return { text: "Usage: /captains-chair status [repo] | plan|approve|pause|resume <repo> <course> | checkpoint <repo> <course> <key> <status> | ack <repo> <fingerprint> [event]" };
        const status = String(result.status ?? result.interaction ?? "completed");
        return { text: `Captain's Chair: ${action} ${status}. Dashboard: /captains-chair/` };
      },
    });

    api.registerCli?.(
      async ({ program }) => {
        const command = program.command("captains-chair").description("Set the course and inspect the agent crew");
        const cliAction = <Args extends unknown[]>(action: (...args: Args) => Promise<void>) =>
          withSidecarShutdown(sidecar, action);
        command.command("status").description("Show portfolio status").action(cliAction(async () => {
          const result = await request("portfolio.status");
          process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        }));
        command.command("schedules").description("Describe Captain's Chair schedules").action(cliAction(async () => {
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
      { descriptors: [{ name: "captains-chair", description: "Set the course and inspect the agent crew", hasSubcommands: true }] },
    );

    api.registerService?.({
      id: PLUGIN_ID,
      start: async () => {
        await sidecar.start();
        api.logger?.info?.("Captain's Chair sidecar started");
      },
      stop: async () => {
        await sidecar.stop();
        api.logger?.info?.("Captain's Chair sidecar stopped");
      },
    });
  },
});
