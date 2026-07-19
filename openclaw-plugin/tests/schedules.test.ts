import { describe, expect, it } from "vitest";
import {
  buildCommandArgv,
  buildCronAddArgs,
  buildCronEditArgs,
  cronListArgs,
  findExistingCronJob,
  inspectCronJob,
  parseCronJobs,
  runOpenClawCommand,
  type ScheduleDefinition,
} from "../src/schedules.js";

const definition: ScheduleDefinition = {
  name: "make-it-so-course-review",
  every: "2h",
  kind: "course_review",
  command: ["python", "-m", "make_it_so.sidecar", "--once", "review"],
};

const expectedArgv = [
  "python3",
  "-m",
  "make_it_so.sidecar",
  "--once",
  "review",
  "--config",
  "/tmp/make-it-so.yaml",
];

describe("OpenClaw schedule reconciliation", () => {
  it("includes disabled jobs when inspecting managed schedules", () => {
    expect(cronListArgs()).toEqual(["cron", "list", "--all", "--json"]);
  });

  it("uses the OpenClaw command-runner contract", async () => {
    const calls: unknown[][] = [];
    const result = await runOpenClawCommand(async (command, args, options) => {
      calls.push([command, args, options]);
      return { code: 0, stdout: "ok" };
    }, "/opt/openclaw/bin/openclaw", ["cron", "list", "--json"]);

    expect(calls).toEqual([[
      "/opt/openclaw/bin/openclaw",
      ["cron", "list", "--json"],
      { timeoutMs: 120_000 },
    ]]);
    expect(result.stdout).toBe("ok");
  });

  it("accepts both object and array cron-list payloads", () => {
    const job = { id: "job-1", name: definition.name };
    expect(parseCronJobs(JSON.stringify({ jobs: [job] }))).toEqual([job]);
    expect(parseCronJobs(JSON.stringify([job]))).toEqual([job]);
  });

  it("builds the configured executable and preserves the sidecar argv", () => {
    expect(buildCommandArgv(definition, "python3", "/tmp/make-it-so.yaml")).toEqual(
      expectedArgv,
    );
    expect(buildCronAddArgs(definition, "python3", "/tmp/make-it-so.yaml", "/tmp")).toContain(
      JSON.stringify(expectedArgv),
    );
  });

  it("inspects exact, drifted, paused, and duplicate jobs without rejecting reconciliation", () => {
    const existing = {
      id: "job-1",
      name: definition.name,
      enabled: true,
      schedule: { kind: "every", everyMs: 7_200_000 },
      payload: { kind: "command", argv: expectedArgv },
    };
    expect(
      findExistingCronJob([existing], definition, "python3", "/tmp/make-it-so.yaml"),
    ).toBe(existing);
    const drifted = { ...existing, enabled: false, payload: { kind: "command", argv: ["python3", "wrong"] } };
    expect(inspectCronJob([drifted, existing], definition, "python3", "/tmp/make-it-so.yaml")).toMatchObject({
      primary: drifted,
      duplicates: [existing],
      drift: ["command"],
      enabled: false,
    });
    expect(buildCronEditArgs("job-1", definition, "python3", "/tmp/make-it-so.yaml", "/tmp")).toContain("edit");
  });

  it("reports unreadable command drift for repair", () => {
    expect(inspectCronJob(
      [{ id: "job-1", name: definition.name, schedule: { kind: "every", everyMs: 7_200_000 } }],
      definition,
      "python3",
      "/tmp/make-it-so.yaml",
    ).drift).toEqual(["command_unreadable"]);
  });
});
