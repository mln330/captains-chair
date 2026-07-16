export type ScheduleDefinition = {
  name: string;
  every: string;
  kind: string;
  command: string[];
};

export type CronJob = Record<string, unknown>;

export type ScheduleInspection = {
  primary?: CronJob;
  duplicates: CronJob[];
  drift: string[];
  enabled: boolean;
};

export function parseCronJobs(stdout: string): CronJob[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(stdout);
  } catch (error) {
    throw new Error(`OpenClaw returned invalid cron JSON: ${String(error)}`);
  }
  if (Array.isArray(parsed)) return parsed.filter(isRecord);
  if (isRecord(parsed) && Array.isArray(parsed.jobs)) return parsed.jobs.filter(isRecord);
  throw new Error("OpenClaw cron JSON did not contain a jobs array");
}

export function buildCommandArgv(
  job: ScheduleDefinition,
  pythonExecutable: string,
  configPath: string,
): string[] {
  if (job.command.length === 0) throw new Error(`schedule ${job.name} has no command`);
  const command = job.command[0].toLowerCase().includes("python")
    ? [pythonExecutable, ...job.command.slice(1)]
    : [...job.command];
  return [...command, "--config", configPath];
}

export function buildCronAddArgs(
  job: ScheduleDefinition,
  pythonExecutable: string,
  configPath: string,
  cwd: string,
): string[] {
  return [
    "cron", "add", "--name", job.name, "--every", job.every,
    "--command-argv", JSON.stringify(buildCommandArgv(job, pythonExecutable, configPath)),
    "--command-cwd", cwd, "--no-deliver", "--json",
  ];
}

export function buildCronEditArgs(
  id: string,
  job: ScheduleDefinition,
  pythonExecutable: string,
  configPath: string,
  cwd: string,
): string[] {
  return [
    "cron", "edit", id, "--name", job.name, "--every", job.every,
    "--command-argv", JSON.stringify(buildCommandArgv(job, pythonExecutable, configPath)),
    "--command-cwd", cwd, "--no-deliver",
  ];
}

export function inspectCronJob(
  jobs: CronJob[],
  definition: ScheduleDefinition,
  pythonExecutable: string,
  configPath: string,
): ScheduleInspection {
  const matching = jobs.filter((job) => job.name === definition.name);
  const primary = matching[0];
  if (!primary) return { duplicates: [], drift: ["missing"], enabled: false };
  const drift: string[] = [];
  try {
    if (readEvery(primary) !== definition.every) drift.push("interval");
  } catch {
    drift.push("interval_unreadable");
  }
  const actualArgv = readArgv(primary);
  if (!actualArgv) drift.push("command_unreadable");
  else if (!sameArray(actualArgv, buildCommandArgv(definition, pythonExecutable, configPath))) {
    drift.push("command");
  }
  return {
    primary,
    duplicates: matching.slice(1),
    drift,
    enabled: primary.enabled !== false,
  };
}

/** Compatibility helper retained for extensions that only need the primary job. */
export function findExistingCronJob(
  jobs: CronJob[],
  definition: ScheduleDefinition,
  pythonExecutable: string,
  configPath: string,
): CronJob | undefined {
  return inspectCronJob(jobs, definition, pythonExecutable, configPath).primary;
}

export function cronIdentifier(job: CronJob): string {
  const id = stringValue(job.id);
  if (!id) throw new Error("OpenClaw cron job has no ID");
  return id;
}

function readEvery(job: CronJob): string {
  const schedule = isRecord(job.schedule) ? job.schedule : undefined;
  if (schedule && schedule.kind === "every") {
    const milliseconds = schedule.everyMs ?? schedule.every_ms;
    if (typeof milliseconds === "number") return millisecondsToEvery(milliseconds);
  }
  if (typeof job.every === "string") return job.every;
  throw new Error("OpenClaw cron job has no supported every interval");
}

function readArgv(job: CronJob): string[] | undefined {
  const payload = isRecord(job.payload) ? job.payload : undefined;
  const candidates: unknown[] = [payload?.argv, payload?.commandArgv, job.argv, job.commandArgv];
  const value = candidates.find((candidate) => Array.isArray(candidate));
  return Array.isArray(value) && value.every((item) => typeof item === "string") ? value : undefined;
}

function millisecondsToEvery(value: number): string {
  if (!Number.isFinite(value) || value <= 0) throw new Error("invalid OpenClaw every interval");
  if (value % 86_400_000 === 0) return `${value / 86_400_000}d`;
  if (value % 3_600_000 === 0) return `${value / 3_600_000}h`;
  if (value % 60_000 === 0) return `${value / 60_000}m`;
  if (value % 1_000 === 0) return `${value / 1_000}s`;
  throw new Error(`unsupported OpenClaw every interval: ${value}ms`);
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function sameArray(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
