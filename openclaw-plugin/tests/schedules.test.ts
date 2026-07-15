import { describe, expect, it } from "vitest";
import {
  buildCommandArgv,
  buildCronAddArgs,
  findExistingCronJob,
  parseCronJobs,
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

  it("reuses an exact existing job and rejects same-name drift", () => {
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
    expect(() =>
      findExistingCronJob(
        [{ ...existing, payload: { kind: "command", argv: ["python3", "wrong"] } }],
        definition,
        "python3",
        "/tmp/captains-chair.yaml",
      ),
    ).toThrow(/different command/);
  });

  it("fails closed when a same-name job cannot be verified", () => {
    expect(() =>
      findExistingCronJob(
        [{ id: "job-1", name: definition.name, schedule: { kind: "every", everyMs: 7_200_000 } }],
        definition,
        "python3",
        "/tmp/captains-chair.yaml",
      ),
    ).toThrow(/cannot be verified/);
  });
});
