import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { readFile } from "node:fs/promises";
import { SidecarSupervisor, type RpcResult } from "./sidecar.js";
import {
  buildCronAddArgs,
  cronIdentifier,
  findExistingCronJob,
  parseCronJobs,
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
  registerHttpRoute?: (route: { path: string; auth: string; handler: (req: any, res: any) => Promise<void> }) => void;
  registerService?: (service: { id: string; start: () => Promise<void>; stop: () => Promise<void> }) => void;
  registerCli?: (registrar: (context: { program: any }) => Promise<void>, opts: Record<string, unknown>) => void;
  session?: { controls?: { registerControlUiDescriptor?: (descriptor: Record<string, unknown>) => void } };
  runtime?: {
    system?: { runCommandWithTimeout?: (command: string, args: string[], options?: Record<string, unknown>) => Promise<any> };
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
      handler: async (_req, res) => {
        res.statusCode = 200;
        res.setHeader("content-type", "text/html; charset=utf-8");
        res.end("<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Captain's Chair</title><link rel=\"stylesheet\" href=\"/captains-chair/assets/index.css\"></head><body><div id=\"root\"></div><script type=\"module\" src=\"/captains-chair/assets/index.js\"></script></body></html>");
      },
    });
    api.registerHttpRoute?.({
      path: "/captains-chair/assets/index.css",
      auth: "plugin",
      handler: async (_req, res) => {
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
      handler: async (_req, res) => {
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
    const installSchedules = async (): Promise<{ status: string; jobs: unknown[] }> => {
      if (config.installSchedules !== true) {
        throw new Error("schedule installation is disabled in plugin configuration");
      }
      const description = await request("schedule.describe");
      const jobs = Array.isArray(description.jobs)
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
      const executable = configString(config, "openclawExecutable", "openclaw");
      const runCommand = api.runtime?.system?.runCommandWithTimeout;
      if (!runCommand) throw new Error("OpenClaw command runtime is unavailable");
      const invoke = async (args: string[]): Promise<CommandResult> => {
        const result = (await runCommand(executable, args, { timeoutMs: 120_000 })) as CommandResult;
        if (typeof result?.code === "number" && result.code !== 0) {
          throw new Error(String(result.stderr ?? `openclaw exited with code ${result.code}`));
        }
        return result ?? {};
      };
      const listed = await invoke(["cron", "list", "--json"]);
      const cronJobs = parseCronJobs(String(listed.stdout ?? ""));
      const installed: unknown[] = [];
      const pythonExecutable = configString(config, "pythonExecutable", "python3");
      for (const job of jobs) {
        const existing = findExistingCronJob(cronJobs, job, pythonExecutable, configPath);
        if (existing) {
          const id = cronIdentifier(existing);
          const enabled = existing.enabled !== false;
          if (!enabled) await invoke(["cron", "enable", id]);
          installed.push({ name: job.name, id, status: enabled ? "unchanged" : "enabled" });
          continue;
        }
        const result = await invoke(buildCronAddArgs(job, pythonExecutable, configPath, dirname(configPath)));
        installed.push({ name: job.name, status: "created", result: result.stdout ?? result });
      }
      return { status: "reconciled", jobs: installed };
    };

    api.registerGatewayMethod?.("captainsChair.schedule.install", async ({ respond }) => {
      try {
        respond(true, await installSchedules());
      } catch (error) {
        respond(false, { error: String(error) });
      }
    }, { scope: "operator.admin" });

    api.registerHttpRoute?.({
      path: "/captains-chair/api/schedule/install",
      auth: "plugin",
      handler: async (_req, res) => {
        try {
          const result = await installSchedules();
          res.statusCode = 200;
          res.setHeader("content-type", "application/json; charset=utf-8");
          res.end(JSON.stringify(result));
        } catch (error) {
          res.statusCode = 500;
          res.end(JSON.stringify({ error: String(error) }));
        }
      },
    });

    api.registerCli?.(
      async ({ program }) => {
        const command = program.command("captains-chair").description("Set the course and inspect the agent crew");
        command.command("status").description("Show portfolio status").action(async () => {
          const result = await request("portfolio.status");
          process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        });
        command.command("schedules").description("Describe Captain's Chair schedules").action(async () => {
          const result = await request("schedule.describe");
          process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        });
        command.command("plan <fullName> <courseKey>").description("Start a native planning conversation").action(async (fullName: string, courseKey: string) => {
          const result = await request("course.planning_session", { full_name: fullName, course_key: courseKey });
          process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
        });
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
