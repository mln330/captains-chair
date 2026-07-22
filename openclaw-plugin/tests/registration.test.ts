import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import plugin, { configuredDiscordRouteOptions, createToolExecutor, deliverDiscordPlanningStatus, deliverNumberOneDiscordTurn, deliverRegistrationFollowUp, discoverDiscordRouteOptions, discordAnswerMentionsRequirement, discordPendingReadinessQuestion, discordPlanningEventKey, discordPlanningRouteMatches, inferDiscordReadinessKey, isDiscordPlanningCourseStatus, nextDiscordReadinessKey, parseConfiguredDiscordGuildIds, parseDiscordChannelOptions, parseDiscordCourseApproval, parseDiscordGuildId, parseRouteParams, pendingDiscordReadinessKey, readRouteParams, resolveDiscordRoute, resolveSidecarLaunch, selectDiscordReadinessQuestion, READINESS_REVIEW_TIMEOUT_MS } from "../src/index.js";

describe("Make It So OpenClaw registration", () => {
  it("runs a configured standalone sidecar without Python module arguments", () => {
    expect(resolveSidecarLaunch(undefined, {
      sidecarExecutable: "C:/make-it-so/make-it-so-sidecar.exe",
      sidecarCommand: ["-m", "make_it_so.sidecar"],
    })).toEqual({
      executable: "C:/make-it-so/make-it-so-sidecar.exe",
      args: [],
      bundled: true,
    });
  });

  it("retains the Python development fallback when no packaged runtime is present", () => {
    expect(resolveSidecarLaunch(undefined, { pythonExecutable: "python-dev" })).toEqual({
      executable: "python-dev",
      args: ["-m", "make_it_so.sidecar"],
      bundled: false,
    });
  });

  it("gives readiness review RPCs more time than the Number One host turn", () => {
    expect(READINESS_REVIEW_TIMEOUT_MS).toBeGreaterThan(600_000);
  });

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
    const config = { discordRouteAliases: { notifications: "channel:111111111111111111" } };
    expect(resolveDiscordRoute("notifications", config)).toBe("channel:111111111111111111");
    expect(resolveDiscordRoute("NOTIFICATIONS", config)).toBe("channel:111111111111111111");
    expect(resolveDiscordRoute("channel:123", config)).toBe("channel:123");
  });

  it("turns live OpenClaw Discord metadata into friendly text-channel routes", () => {
    const config = { discordRouteAliases: { notifications: "channel:111111111111111111" } };
    expect(configuredDiscordRouteOptions(config)[0]).toMatchObject({
      route: "channel:111111111111111111",
      label: "#notifications",
    });
    expect(parseDiscordGuildId(JSON.stringify({ payload: { channel: { guild_id: "guild-1" } } }))).toBe("guild-1");
    expect(parseConfiguredDiscordGuildIds(JSON.stringify({
      "333333333333333333": { requireMention: false },
      invalid: {},
    }))).toEqual(["333333333333333333"]);
    const routes = parseDiscordChannelOptions(JSON.stringify({
      payload: {
        channels: [
          { id: "category", type: 4, name: "Text Channels", guild_id: "guild-1" },
          { id: "200", type: 0, name: "image-manager", guild_id: "guild-1" },
          { id: "111111111111111111", type: 0, name: "notifications", guild_id: "guild-1" },
        ],
      },
    }), config);
    expect(routes.map((route) => route.label)).toEqual(["#notifications", "#image-manager"]);
    expect(routes[0].alias).toBe("notifications");
  });

  it("discovers Discord routes from OpenClaw guild configuration without route aliases", async () => {
    const calls: string[][] = [];
    const result = await discoverDiscordRouteOptions(async (argv) => {
      calls.push(argv);
      if (argv.includes("config")) {
        return { code: 0, stdout: JSON.stringify({ "333333333333333333": { requireMention: false } }) };
      }
      return { code: 0, stdout: JSON.stringify({ payload: { channels: [
        { id: "123", type: 0, name: "project-room", guild_id: "333333333333333333" },
        { id: "456", type: 0, name: "notifications", guild_id: "333333333333333333" },
      ] } }) };
    }, "openclaw", {});

    expect(calls).toHaveLength(2);
    expect(calls[0]).toEqual(["openclaw", "config", "get", "channels.discord.guilds", "--json"]);
    expect(calls[1]).toContain("333333333333333333");
    expect(result.discord_routes.map((route) => route.label)).toEqual(["#notifications", "#project-room"]);
    expect(result.default_discord_route).toBe("channel:456");
    expect(result.warnings).toEqual([]);
  });

  it("falls back to a configured route only when OpenClaw exposes no guild configuration", async () => {
    const calls: string[][] = [];
    const result = await discoverDiscordRouteOptions(async (argv) => {
      calls.push(argv);
      if (argv.includes("config")) return { code: 1, stderr: "missing" };
      if (argv.includes("info")) {
        return { code: 0, stdout: JSON.stringify({ payload: { channel: { guild_id: "guild-1" } } }) };
      }
      return { code: 0, stdout: JSON.stringify({ payload: { channels: [{ id: "123", type: 0, name: "project-room", guild_id: "guild-1" }] } }) };
    }, "openclaw", { discordRouteAliases: { project: "channel:123" } });

    expect(calls).toHaveLength(3);
    expect(calls[1]).toContain("channel:123");
    expect(result.default_discord_route).toBe("channel:123");
  });

  it("matches OpenClaw Discord conversation identifiers to configured channel routes", () => {
    expect(discordPlanningRouteMatches("channel:111111111111111111", ["111111111111111111"])).toBe(true);
    expect(discordPlanningRouteMatches("111111111111111111", ["channel:111111111111111111"])).toBe(true);
    expect(discordPlanningRouteMatches("channel:111111111111111111", ["222222222222222222"])).toBe(false);
  });

  it("does not deduplicate a retried answer by identical text when the host omits message ids", () => {
    expect(discordPlanningEventKey(
      { content: "same answer" },
      { conversationId: "channel:123" },
      "same answer",
    )).toBeUndefined();
    expect(discordPlanningEventKey(
      { timestamp: "2026-07-20T06:30:00Z" },
      { conversationId: "channel:123" },
      "same answer",
    )).toBe("2026-07-20T06:30:00Z:same answer");
  });

  it("keeps Number One reachable after a course is engaged", () => {
  expect(isDiscordPlanningCourseStatus("engaged")).toBe(true);
  expect(isDiscordPlanningCourseStatus("post_merge_verification")).toBe(true);
  expect(isDiscordPlanningCourseStatus("baseline_review")).toBe(true);
  expect(isDiscordPlanningCourseStatus("completed")).toBe(false);
  });

  it("recognizes only explicit Discord course approvals", () => {
    expect(parseDiscordCourseApproval("A — Approve the reconciled plan.")).toBe("approve");
    expect(parseDiscordCourseApproval("B: approved, keep the current plan.")).toBe("approve");
    expect(parseDiscordCourseApproval("I approve this course.")).toBe("approve");
    expect(parseDiscordCourseApproval("The primary users are maintainers.")).toBeUndefined();
    expect(parseDiscordCourseApproval("C — Require the larger architecture.")).toBeUndefined();
  });

  it("selects the next required readiness answer from the canonical course", () => {
    expect(pendingDiscordReadinessKey({
      readiness: [
        { key: "baseline", required: true, status: "answered" },
        { key: "permissions", required: true, status: "unknown" },
        { key: "optional", required: false, status: "unknown" },
      ],
    })).toBe("permissions");
    expect(pendingDiscordReadinessKey({
      readiness: [{ key: "permissions", required: true, status: "verified" }],
    })).toBeUndefined();
    expect(pendingDiscordReadinessKey({
      readiness: [{ key: "token_policy", required: true, status: "answered" }],
      readiness_review: { verdict: "needs_input" },
    })).toBe("token_policy");
  });

  it("does not attach a conversational answer to the first pending requirement", () => {
    expect(discordAnswerMentionsRequirement(
      "Replace the legacy graph with the approved minimal graph and continue planning.",
      "baseline_complete",
    )).toBe(false);
    expect(discordAnswerMentionsRequirement(
      "Record baseline_complete as yes based on the accepted baseline.",
      "baseline_complete",
    )).toBe(true);
  });

  it("routes conversational answers by topic instead of first blocked item", () => {
    const course = {
      readiness: [
        { key: "goals", required: true, status: "blocked" },
        { key: "architecture-constraints", required: true, status: "blocked" },
        { key: "secret-references", required: true, status: "blocked" },
        { key: "permissions", required: true, status: "blocked" },
        { key: "environments", required: true, status: "blocked" },
        { key: "rollback", required: true, status: "blocked" },
      ],
    };
    expect(inferDiscordReadinessKey(
      "Keep the current language, runtime, database format, filesystem layout, and CLI compatibility.",
      course,
    )).toBe("architecture-constraints");
    expect(inferDiscordReadinessKey(
      "Support Linux and Python 3.13 in isolated OpenClaw workspaces and CI; additional operating systems are out of scope.",
      course,
    )).toBe("environments");
    expect(pendingDiscordReadinessKey(
      course,
      "No new secret values or credentials are required.",
    )).toBe("secret-references");
    expect(pendingDiscordReadinessKey(
      course,
      "Number One may create branches, issues, and pull requests; production changes remain owner-approved.",
    )).toBe("permissions");
    expect(pendingDiscordReadinessKey(
      course,
      "Failed milestones keep their branch for diagnosis; use reversible repair or revert commits without force-push, preserve data, and require Number One approval for recovery.",
    )).toBe("rollback");
  });

  it("binds a conversational reply to the first unresolved readiness item", () => {
    expect(nextDiscordReadinessKey({
      readiness: [
        { key: "goals", required: true, status: "unknown" },
        { key: "users", required: true, status: "unknown" },
        { key: "environments", required: true, status: "unknown" },
      ],
    })).toBe("goals");
    expect(nextDiscordReadinessKey({
      readiness: [
        { key: "goals", required: true, status: "answered" },
        { key: "users", required: true, status: "unknown" },
        { key: "environments", required: true, status: "unknown" },
      ],
    })).toBe("users");
    expect(nextDiscordReadinessKey({
      readiness: [
        { key: "goals", required: true, status: "verified" },
        { key: "users", required: true, status: "waived" },
      ],
    })).toBeUndefined();
  });

  it("binds replies to the exact durable question instead of the first unresolved item", () => {
    const course = {
      pending_readiness_key: "UX-inputs",
      pending_readiness_question: "What UX inputs should guide the interface?",
      readiness: [
        { key: "security", required: true, status: "blocked", question: "What security properties are required?" },
        { key: "UX-inputs", required: true, status: "blocked", question: "What UX inputs should guide the interface?" },
      ],
    };

    expect(discordPendingReadinessQuestion(course)).toEqual({
      key: "UX-inputs",
      question: "What UX inputs should guide the interface?",
    });
    expect(discordPendingReadinessQuestion({ readiness: course.readiness })).toBeUndefined();
  });

  it("selects and binds the reviewer's one specific follow-up question", () => {
    const course = {
      readiness: [
        { key: "security", required: true, status: "blocked", question: "What security properties are required?" },
        { key: "UX-inputs", required: true, status: "blocked", question: "What UX expectations should be tested?" },
      ],
      readiness_review: {
        next_questions: ["Should the repository remain public or be private to satisfy the privacy policy?"],
      },
    };

    expect(selectDiscordReadinessQuestion(course, { unresolved: ["security", "UX-inputs"] })).toEqual({
      key: "security",
      question: "Should the repository remain public or be private to satisfy the privacy policy?",
    });
  });

  it("forwards OpenClaw tool-call parameters after the tool call id", async () => {
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const execute = createToolExecutor(async (method, params) => {
      calls.push({ method, params });
      return { ok: true };
    }, "course.planning_session");

    await expect(execute("tool-call-1", {
      full_name: "mln330/github-actions-runner-viewer",
      course_key: "mvp-kiosk-viewer",
    })).resolves.toEqual({
      content: [{ type: "text", text: JSON.stringify({ ok: true }) }],
      details: { ok: true },
    });
    expect(calls).toEqual([{
      method: "course.planning_session",
      params: {
        full_name: "mln330/github-actions-runner-viewer",
        course_key: "mvp-kiosk-viewer",
      },
    }]);

    const legacyExecute = createToolExecutor(async (method, params) => {
      calls.push({ method, params });
      return { ok: true };
    }, "course.planning_session");
    await expect((legacyExecute as unknown as (params: Record<string, unknown>) => Promise<unknown>)({
      full_name: "mln330/github-actions-runner-viewer",
      course_key: "mvp-kiosk-viewer",
    })).resolves.toEqual({
      content: [{ type: "text", text: JSON.stringify({ ok: true }) }],
      details: { ok: true },
    });
  });

  it("delivers registration follow-ups through the host command runner", async () => {
    const calls: unknown[][] = [];
    const result = await deliverRegistrationFollowUp(
      {
        follow_up_message: "Repository registered. Number One will follow up in chat before any work begins.",
        number_one_prompt: "You are Number One. Ask the builder the initial planning questions.",
        number_one_session_key: "make-it-so:number-one:example-project",
        notification_route: "channel:111111111111111111",
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
        "--channel", "discord", "--deliver", "--reply-channel", "discord", "--reply-to", "channel:111111111111111111",
        "--session-key", "make-it-so:number-one:example-project",
        "--message", "You are Number One. Ask the builder the initial planning questions.",
        "--json",
      ],
      { timeoutMs: 600_000 },
    ]]);
    expect(result.notification_status).toBe("sent");
    expect(result.notification_delivery).toBe("number_one_agent");
  });

  it("falls back to a direct planning message when the Number One turn fails", async () => {
    const calls: unknown[][] = [];
    const result = await deliverRegistrationFollowUp(
      {
        follow_up_message: "registration receipt",
        number_one_prompt: "NUMBER ONE | INITIAL PLANNING\nPlease answer the goal question.",
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
      "--message", "NUMBER ONE | INITIAL PLANNING\nPlease answer the goal question.", "--json",
    ]);
  });

  it("continues a Discord planning reply in the same Number One session", async () => {
    const calls: string[][] = [];
    const options: unknown[] = [];
    await deliverNumberOneDiscordTurn(
      "The primary users are maintainers.",
      { repository: "example/project", route: "channel:123", sessionKey: "make-it-so:number-one:example-project" },
      async (argv, commandOptions) => {
        calls.push(argv);
        options.push(commandOptions);
        return { code: 0, stdout: "{}" };
      },
      "openclaw",
      "github-captain",
      "codex/gpt-5.6-sol",
      "high",
    );

    expect(calls).toEqual([[
      "openclaw", "agent", "--agent", "github-captain", "--model", "codex/gpt-5.6-sol", "--thinking", "high",
      "--channel", "discord", "--deliver", "--reply-channel", "discord", "--reply-to", "channel:123",
      "--session-key", "make-it-so:number-one:example-project", "--message", "The primary users are maintainers.", "--json",
    ]]);
    expect(options).toEqual([{ timeoutMs: 600_000 }]);
  });

  it("acknowledges a planning answer through Discord before a slow readiness review", async () => {
    const calls: string[][] = [];
    await deliverDiscordPlanningStatus(
      "Number One received your answer.",
      "channel:123",
      async (argv) => {
        calls.push(argv);
        return { code: 0, stdout: "{}" };
      },
      "openclaw",
    );

    expect(calls).toEqual([[
      "openclaw", "message", "send", "--channel", "discord", "--target", "channel:123",
      "--message", "Number One received your answer.", "--json",
    ]]);
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
      "make_it_so_review_readiness",
      "make_it_so_ready_work",
      "make_it_so_approve_course",
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
      controlDescriptors: [] as Array<Record<string, unknown>>,
      cli: 0,
      commands: [] as string[],
      commandDefinitions: [] as Array<{ name: string; handler: (context: Record<string, unknown>) => Promise<{ text: string }> }>,
      hookDefinitions: [] as Array<{ name: string; handler: (...args: any[]) => Promise<unknown> | unknown }>,
    };
    const api = {
      pluginConfig: { installSchedules: false },
      rootDir: process.cwd(),
      session: {
        controls: {
          registerControlUiDescriptor: (descriptor: Record<string, unknown> & { id?: string }) => {
            if (descriptor.id) registrations.controls.push(descriptor.id);
            registrations.controlDescriptors.push(descriptor);
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
      registerHook: (_events: string | string[], handler: (...args: any[]) => Promise<unknown> | unknown, opts?: { name?: string }) => {
        if (opts?.name) {
          registrations.hooks.push(opts.name);
          registrations.hookDefinitions.push({ name: opts.name, handler });
        }
      },
      on: (_event: string, handler: (...args: any[]) => Promise<unknown> | unknown, opts?: { name?: string }) => {
        if (opts?.name) {
          registrations.hooks.push(opts.name);
          registrations.hookDefinitions.push({ name: opts.name, handler });
        }
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
    expect(registrations.controlDescriptors).toContainEqual(expect.objectContaining({
      id: "make-it-so",
      icon: "rocket",
    }));
    expect(registrations.gateway).toContain("makeItSo.portfolio.status");
    expect(registrations.gateway).toContain("makeItSo.bootstrap.status");
    expect(registrations.gateway).toContain("makeItSo.bootstrap.apply");
    expect(registrations.gateway).toContain("makeItSo.models.config");
    expect(registrations.gateway).toContain("makeItSo.models.update");
    expect(registrations.gateway).toContain("makeItSo.usage.config");
    expect(registrations.gateway).toContain("makeItSo.usage.update");
    expect(registrations.gateway).toContain("makeItSo.repos.create");
    expect(registrations.gateway).toContain("makeItSo.registration.options");
    expect(registrations.gateway).toContain("makeItSo.course.requirement");
    expect(registrations.gateway).toContain("makeItSo.course.planningSession");
    expect(registrations.gateway).toContain("makeItSo.course.models");
    expect(registrations.gatewayScopes["makeItSo.health"]).toBe("operator.read");
    expect(registrations.gatewayScopes["makeItSo.bootstrap.apply"]).toBe("operator.admin");
    expect(registrations.gatewayScopes["makeItSo.course.create"]).toBe("operator.write");
    expect(registrations.gatewayScopes["makeItSo.schedule.install"]).toBe("operator.admin");
    expect(registrations.gatewayScopes["makeItSo.schedule.status"]).toBe("operator.read");
    expect(registrations.gatewayScopes["makeItSo.runNow"]).toBe("operator.admin");
    expect(registrations.tools).toContain("make_it_so_course_status");
    expect(registrations.tools).toContain("make_it_so_answer_readiness");
    expect(registrations.tools).toContain("make_it_so_start_planning");
    expect(registrations.tools).toContain("make_it_so_review_readiness");
    expect(registrations.tools).toContain("make_it_so_approve_course");
    expect(registrations.hooks).toEqual([
      "make-it-so-workboard-reconciliation",
      "make-it-so-discord-number-one-planning",
      "make-it-so-discord-number-one-planning-before-dispatch",
    ]);
    expect(registrations.routes).toContain("/make-it-so/");
    expect(registrations.routes).toContain("/make-it-so/api/bootstrap/status");
    expect(registrations.routes).toContain("/make-it-so/api/bootstrap/apply");
    expect(registrations.routes).toContain("/make-it-so/api/schedule/install");
    expect(registrations.routes).toContain("/make-it-so/api/schedule/status");
    expect(registrations.routes).toContain("/make-it-so/api/schedule/edit");
    expect(registrations.routes).toContain("/make-it-so/api/run/start");
    expect(registrations.routes).toContain("/make-it-so/api/repos/create");
    expect(registrations.routes).toContain("/make-it-so/api/registration/options");
    expect(registrations.routes).toContain("/make-it-so/api/course/models");
    expect(registrations.routes).toContain("/make-it-so/api/models/config");
    expect(registrations.routes).toContain("/make-it-so/api/models/update");
    expect(registrations.routes).toContain("/make-it-so/api/usage/update");
    expect(registrations.routeAuth["/make-it-so/"]).toBe("plugin");
    expect(registrations.routeAuth["/make-it-so/assets/index.js"]).toBe("plugin");
    expect(registrations.routeAuth["/make-it-so/api/repos/create"]).toBe("plugin");
    expect(registrations.routeAuth["/make-it-so/api/schedule/install"]).toBe("plugin");
    expect(registrations.routeAuth["/make-it-so/api/schedule/status"]).toBe("plugin");
    expect(registrations.routeAuth["/make-it-so/api/run/start"]).toBe("plugin");
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
    expect(source).toContain('src="/make-it-so/assets/index.js?v=${UI_ASSET_VERSION}"');
    expect(source).toContain('content-security-policy", "frame-ancestors \'self\'');
    expect(source).toContain('make-it-so-control-token');
  });
});
