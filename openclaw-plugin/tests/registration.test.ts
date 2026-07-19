import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import plugin, { deliverRegistrationFollowUp, parseRouteParams, readRouteParams, resolveDiscordRoute } from "../src/index.js";

describe("Make It So OpenClaw registration", () => {
  it("preserves JSON parameters from string and byte route bodies", () => {
    expect(parseRouteParams('{"full_name":"example/project"}')).toEqual({ full_name: "example/project" });
    expect(parseRouteParams(new TextEncoder().encode('{"local_path":"/workspace/project"}'))).toEqual({ local_path: "/workspace/project" });
  });

  it("reads JSON parameters from the raw OpenClaw request stream", async () => {
    async function* requestBody() {
      yield new TextEncoder().encode('{"full_name":"stream/project",');
      yield '"course_key":"foundation"}';
    }

    await expect(readRouteParams(requestBody())).resolves.toEqual({
      full_name: "stream/project",
      course_key: "foundation",
    });
  });

  it("accepts an ArrayBuffer request chunk", async () => {
    async function* requestBody() {
      yield new TextEncoder().encode('{"full_name":"buffer/project"}').buffer;
    }

    await expect(readRouteParams(requestBody())).resolves.toEqual({
      full_name: "buffer/project",
    });
  });

  it("resolves configured Discord route aliases without changing explicit targets", () => {
    const config = { discordRouteAliases: { notifications: "channel:1483192074344988954" } };
    expect(resolveDiscordRoute("notifications", config)).toBe("channel:1483192074344988954");
    expect(resolveDiscordRoute("NOTIFICATIONS", config)).toBe("channel:1483192074344988954");
    expect(resolveDiscordRoute("channel:123", config)).toBe("channel:123");
  });

  it("delivers registration follow-ups through the host command runner", async () => {
    const calls: unknown[][] = [];
    const result = await deliverRegistrationFollowUp(
      {
        follow_up_message: "Repository registered. Number 1 will follow up in chat before any work begins.",
        number_one_prompt: "You are Number 1. Ask the builder the initial planning questions.",
        number_one_session_key: "make-it-so:number-one:example-project",
        notification_route: "channel:1483192074344988954",
      },
      async (argv, options) => {
        calls.push([argv, options]);
        return { code: 0, stdout: '{"id":"discord-message-1"}' };
      },
      "openclaw",
    );

    expect(calls).toEqual([[
      [
        "openclaw",
        "agent", "--agent", "github-captain", "--model", "codex/gpt-5.6-sol", "--thinking", "high",
        "--channel", "discord", "--deliver", "--reply-channel", "discord", "--reply-to", "channel:1483192074344988954",
        "--session-key", "make-it-so:number-one:example-project",
        "--message", "You are Number 1. Ask the builder the initial planning questions.",
        "--json",
      ],
      { timeoutMs: 180_000 },
    ]]);
    expect(result.notification_status).toBe("sent");
    expect(result.notification_delivery).toBe("number_one_agent");
  });

  it("falls back to a direct planning message when the Number 1 turn fails", async () => {
    const calls: unknown[][] = [];
    const result = await deliverRegistrationFollowUp(
      {
        follow_up_message: "registration receipt",
        number_one_prompt: "NUMBER 1 | INITIAL PLANNING\nPlease answer the goal question.",
        number_one_session_key: "make-it-so:number-one:fallback",
        notification_route: "channel:123",
      },
      async (argv, options) => {
        calls.push([argv, options]);
        if (String(argv[1]) === "agent") return { code: 1, stderr: "model unavailable" };
        return { code: 0, stdout: '{"id":"discord-message-2"}' };
      },
      "openclaw",
    );

    expect(result.notification_status).toBe("sent");
    expect(result.notification_delivery).toBe("message_fallback");
    expect(calls).toHaveLength(2);
    expect(calls[1][0]).toEqual([
      "openclaw", "message", "send", "--channel", "discord", "--target", "channel:123",
      "--message", "NUMBER 1 | INITIAL PLANNING\nPlease answer the goal question.", "--json",
    ]);
  });

  it("surfaces registration delivery failures to the dashboard caller", async () => {
    const warnings: string[] = [];
    const result = await deliverRegistrationFollowUp(
      { follow_up_message: "follow up", notification_route: "channel:123" },
      async () => ({ code: 1, stderr: "Discord target rejected" }),
      "openclaw",
      (message) => warnings.push(message),
    );

    expect(result.notification_status).toBe("failed");
    expect(result.notification_error).toContain("Discord target rejected");
    expect(warnings[0]).toContain("Discord target rejected");
  });

  it("declares every agent tool in the host manifest contract", () => {
    const manifest = JSON.parse(
      readFileSync(resolve(process.cwd(), "openclaw.plugin.json"), "utf8"),
    ) as {
      activation?: { onStartup?: boolean };
      contracts?: { tools?: string[] };
      configSchema?: { properties?: { discordRouteAliases?: { type?: string } } };
    };
    expect(manifest.activation?.onStartup).toBe(true);
    expect(manifest.configSchema?.properties?.discordRouteAliases?.type).toBe("object");
    expect(manifest.contracts?.tools).toEqual([
      "make_it_so_course_status",
      "make_it_so_resolve_checkpoint",
      "make_it_so_answer_readiness",
      "make_it_so_start_planning",
      "make_it_so_ready_work",
    ]);
  });

  it("registers the dashboard, RPC, tools, hooks, CLI, routes, and sidecar service", async () => {
    const registrations = {
      gateway: [] as string[],
      gatewayScopes: {} as Record<string, string | undefined>,
      tools: [] as string[],
      hooks: [] as string[],
      routes: [] as string[],
      routeAuth: {} as Record<string, string>,
      services: [] as string[],
      controls: [] as string[],
      cli: 0,
      commands: [] as string[],
      commandDefinitions: [] as Array<{ name: string; handler: (context: Record<string, unknown>) => Promise<{ text: string }> }>,
    };
    const api = {
      pluginConfig: { installSchedules: false },
      rootDir: process.cwd(),
      session: {
        controls: {
          registerControlUiDescriptor: (descriptor: { id?: string }) => {
            if (descriptor.id) registrations.controls.push(descriptor.id);
          },
        },
      },
      registerGatewayMethod: (name: string, _handler: unknown, opts?: { scope?: string }) => {
        registrations.gateway.push(name);
        registrations.gatewayScopes[name] = opts?.scope;
      },
      registerTool: (tool: { name?: string }) => {
        if (tool.name) registrations.tools.push(tool.name);
      },
      registerHook: (_events: string | string[], _handler: (...args: unknown[]) => Promise<void>, opts?: { name?: string }) => {
        if (opts?.name) registrations.hooks.push(opts.name);
      },
      registerHttpRoute: (route: { path: string; auth: string }) => {
        registrations.routes.push(route.path);
        registrations.routeAuth[route.path] = route.auth;
      },
      registerService: (service: { id: string }) => registrations.services.push(service.id),
      registerCli: () => { registrations.cli += 1; },
      registerCommand: (command: { name: string; handler: (context: Record<string, unknown>) => Promise<{ text: string }> }) => { registrations.commands.push(command.name); registrations.commandDefinitions.push(command); },
    };

    const entry = plugin as unknown as { register: (value: typeof api) => void };
    entry.register(api);

    expect(registrations.controls).toContain("make-it-so");
    expect(registrations.gateway).toContain("makeItSo.portfolio.status");
    expect(registrations.gateway).toContain("makeItSo.models.config");
    expect(registrations.gateway).toContain("makeItSo.models.update");
    expect(registrations.gateway).toContain("makeItSo.usage.config");
    expect(registrations.gateway).toContain("makeItSo.usage.update");
    expect(registrations.gateway).toContain("makeItSo.repos.create");
    expect(registrations.gateway).toContain("makeItSo.course.requirement");
    expect(registrations.gateway).toContain("makeItSo.course.planningSession");
    expect(registrations.gateway).toContain("makeItSo.course.models");
    expect(registrations.gatewayScopes["makeItSo.health"]).toBe("operator.read");
    expect(registrations.gatewayScopes["makeItSo.course.create"]).toBe("operator.write");
    expect(registrations.gatewayScopes["makeItSo.schedule.install"]).toBe("operator.admin");
    expect(registrations.gatewayScopes["makeItSo.schedule.status"]).toBe("operator.read");
    expect(registrations.tools).toContain("make_it_so_course_status");
    expect(registrations.tools).toContain("make_it_so_answer_readiness");
    expect(registrations.tools).toContain("make_it_so_start_planning");
    expect(registrations.hooks).toEqual(["make-it-so-workboard-reconciliation"]);
    expect(registrations.routes).toContain("/make-it-so/");
    expect(registrations.routes).toContain("/make-it-so/api/schedule/install");
    expect(registrations.routes).toContain("/make-it-so/api/schedule/status");
    expect(registrations.routes).toContain("/make-it-so/api/schedule/edit");
    expect(registrations.routes).toContain("/make-it-so/api/repos/create");
    expect(registrations.routes).toContain("/make-it-so/api/course/models");
    expect(registrations.routes).toContain("/make-it-so/api/models/config");
    expect(registrations.routes).toContain("/make-it-so/api/models/update");
    expect(registrations.routes).toContain("/make-it-so/api/usage/update");
    expect(registrations.routeAuth["/make-it-so/"]).toBe("plugin");
    expect(registrations.routeAuth["/make-it-so/assets/index.js"]).toBe("plugin");
    expect(registrations.routeAuth["/make-it-so/api/repos/create"]).toBe("plugin");
    expect(registrations.routeAuth["/make-it-so/api/schedule/install"]).toBe("plugin");
    expect(registrations.routeAuth["/make-it-so/api/schedule/status"]).toBe("plugin");
    expect(registrations.services).toEqual(["make-it-so"]);
    expect(registrations.cli).toBe(1);
    expect(registrations.commands).toEqual(["make-it-so"]);
    await expect(registrations.commandDefinitions[0].handler({ args: "approve example/project feature", senderIsOwner: false, gatewayClientScopes: ["operator.read"] })).resolves.toEqual({
      text: "Make It So refused that mutation: owner or operator.write scope is required.",
    });
  });

  it("marks embedded UI assets as CORS-enabled for the sandboxed plugin frame", () => {
    const source = readFileSync(resolve(process.cwd(), "src/index.ts"), "utf8");
    expect(source).toContain('<link rel="stylesheet" crossorigin="anonymous"');
    expect(source).toContain('<script type="module" crossorigin="anonymous"');
    expect(source).toContain('src="/make-it-so/assets/index.js"');
    expect(source).toContain('content-security-policy", "frame-ancestors \'self\'');
    expect(source).toContain('make-it-so-control-token');
  });
});
