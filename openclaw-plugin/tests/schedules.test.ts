import { describe, expect, it } from "vitest";
import {
  buildCommandArgv,
  buildCronAddArgs,
  buildCronEditArgs,
  findExistingCronJob,
  inspectCronJob,
  parseCronJobs,
  runOpenClawCommand,
  type ScheduleDefinition,
} from "../src/schedules.js";

const definition: ScheduleDefinition = {
  name: "captains-chair-course-review",
  every: "2h",
  kind: "course_review",
  command: ["python", "-m", "captains_chair.sidecar", "--once", "review"],
};

const expectedArgv = [
  "python3",
  "-m",
  "captains_chair.sidecar",
  "--once",
  "review",
  "--config",
  "/tmp/captains-chair.yaml",
];

describe("OpenClaw schedule reconciliation", () => {
  it("uses the OpenClaw 2026 command-runner contract", async () => {
    const calls: unknown[][] = [];
    const result = await runOpenClawCommand(async (argv, options) => {
      calls.push([argv, options]);
      return { code: 0, stdout: "ok" };
    }, "/opt/openclaw/bin/openclaw", ["cron", "list", "--json"]);

    expect(calls).toEqual([[
      ["/opt/openclaw/bin/openclaw", "cron", "list", "--json"],
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
    expect(buildCommandArgv(definition, "python3", "/tmp/captains-chair.yaml")).toEqual(
      expectedArgv,
    );
    expect(buildCronAddArgs(definition, "python3", "/tmp/captains-chair.yaml", "/tmp")).toContain(
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
      findExistingCronJob([existing], definition, "python3", "/tmp/captains-chair.yaml"),
    ).toBe(existing);
    const drifted = { ...existing, enabled: false, payload: { kind: "command", argv: ["python3", "wrong"] } };
    expect(inspectCronJob([drifted, existing], definition, "python3", "/tmp/captains-chair.yaml")).toMatchObject({
      primary: drifted,
      duplicates: [existing],
      drift: ["command"],
      enabled: false,
    });
    expect(buildCronEditArgs("job-1", definition, "python3", "/tmp/captains-chair.yaml", "/tmp")).toContain("edit");
  });

  it("reports unreadable command drift for repair", () => {
    expect(inspectCronJob(
      [{ id: "job-1", name: definition.name, schedule: { kind: "every", everyMs: 7_200_000 } }],
      definition,
      "python3",
      "/tmp/captains-chair.yaml",
    ).drift).toEqual(["command_unreadable"]);
  });
});
