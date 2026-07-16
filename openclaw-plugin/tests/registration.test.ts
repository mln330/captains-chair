import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import plugin from "../src/index.js";

describe("Captain's Chair OpenClaw registration", () => {
  it("declares every agent tool in the host manifest contract", () => {
    const manifest = JSON.parse(
      readFileSync(resolve(process.cwd(), "openclaw.plugin.json"), "utf8"),
    ) as { activation?: { onStartup?: boolean }; contracts?: { tools?: string[] } };
    expect(manifest.activation?.onStartup).toBe(true);
    expect(manifest.contracts?.tools).toEqual([
      "captains_chair_course_status",
      "captains_chair_resolve_checkpoint",
      "captains_chair_answer_readiness",
      "captains_chair_start_planning",
      "captains_chair_ready_work",
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

    expect(registrations.controls).toContain("captains-chair");
    expect(registrations.gateway).toContain("captainsChair.portfolio.status");
    expect(registrations.gateway).toContain("captainsChair.models.config");
    expect(registrations.gateway).toContain("captainsChair.models.update");
    expect(registrations.gateway).toContain("captainsChair.usage.config");
    expect(registrations.gateway).toContain("captainsChair.usage.update");
    expect(registrations.gateway).toContain("captainsChair.repos.create");
    expect(registrations.gateway).toContain("captainsChair.course.requirement");
    expect(registrations.gateway).toContain("captainsChair.course.planningSession");
    expect(registrations.gateway).toContain("captainsChair.course.models");
    expect(registrations.gatewayScopes["captainsChair.health"]).toBe("operator.read");
    expect(registrations.gatewayScopes["captainsChair.course.create"]).toBe("operator.write");
    expect(registrations.gatewayScopes["captainsChair.schedule.install"]).toBe("operator.admin");
    expect(registrations.gatewayScopes["captainsChair.schedule.status"]).toBe("operator.read");
    expect(registrations.tools).toContain("captains_chair_course_status");
    expect(registrations.tools).toContain("captains_chair_answer_readiness");
    expect(registrations.tools).toContain("captains_chair_start_planning");
    expect(registrations.hooks).toEqual(["captains-chair-workboard-reconciliation"]);
    expect(registrations.routes).toContain("/captains-chair/");
    expect(registrations.routes).toContain("/captains-chair/api/schedule/install");
    expect(registrations.routes).toContain("/captains-chair/api/schedule/status");
    expect(registrations.routes).toContain("/captains-chair/api/schedule/edit");
    expect(registrations.routes).toContain("/captains-chair/api/repos/create");
    expect(registrations.routes).toContain("/captains-chair/api/course/models");
    expect(registrations.routes).toContain("/captains-chair/api/models/config");
    expect(registrations.routes).toContain("/captains-chair/api/models/update");
    expect(registrations.routes).toContain("/captains-chair/api/usage/update");
    expect(registrations.routeAuth["/captains-chair/"]).toBe("plugin");
    expect(registrations.routeAuth["/captains-chair/assets/index.js"]).toBe("plugin");
    expect(registrations.routeAuth["/captains-chair/api/repos/create"]).toBe("plugin");
    expect(registrations.routeAuth["/captains-chair/api/schedule/install"]).toBe("plugin");
    expect(registrations.routeAuth["/captains-chair/api/schedule/status"]).toBe("plugin");
    expect(registrations.services).toEqual(["captains-chair"]);
    expect(registrations.cli).toBe(1);
    expect(registrations.commands).toEqual(["captains-chair"]);
    await expect(registrations.commandDefinitions[0].handler({ args: "approve example/project feature", senderIsOwner: false, gatewayClientScopes: ["operator.read"] })).resolves.toEqual({
      text: "Captain's Chair refused that mutation: owner or operator.write scope is required.",
    });
  });
});
