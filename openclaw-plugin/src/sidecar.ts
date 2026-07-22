import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface, type Interface } from "node:readline";

export type RpcResult = Record<string, unknown>;
export const SIDECAR_PROTOCOL_VERSION = 1;
export type SidecarRequest = {
  jsonrpc: "2.0";
  id: number;
  method: string;
  params?: Record<string, unknown>;
};

type Pending = {
  resolve: (value: RpcResult) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
};

export type SidecarOptions = {
  executable: string;
  args: string[];
  cwd?: string;
  configPath: string;
  timeoutMs?: number;
  env?: NodeJS.ProcessEnv;
};

export function withSidecarShutdown<Args extends unknown[], Result>(
  sidecar: Pick<SidecarSupervisor, "stop">,
  action: (...args: Args) => Promise<Result>,
): (...args: Args) => Promise<Result> {
  return async (...args: Args) => {
    try {
      return await action(...args);
    } finally {
      await sidecar.stop();
    }
  };
}

export class SidecarSupervisor {
  private child: ChildProcessWithoutNullStreams | undefined;
  private lines: Interface | undefined;
  private startPromise: Promise<void> | undefined;
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();
  private readonly timeoutMs: number;

  public constructor(
    private readonly options: SidecarOptions,
    private readonly log: (message: string, error?: unknown) => void = () => undefined,
  ) {
    this.timeoutMs = options.timeoutMs ?? 60_000;
  }

  public get running(): boolean {
    return this.child !== undefined && this.child.exitCode === null;
  }

  public async start(): Promise<void> {
    if (this.running) return;
    if (this.startPromise) return this.startPromise;
    const pendingStart = this.startInternal();
    this.startPromise = pendingStart;
    try {
      await pendingStart;
    } finally {
      if (this.startPromise === pendingStart) this.startPromise = undefined;
    }
  }

  private async startInternal(): Promise<void> {
    const child = spawn(
      this.options.executable,
      [...this.options.args, "--config", this.options.configPath],
      {
        cwd: this.options.cwd,
        env: { ...process.env, ...this.options.env },
        shell: false,
        stdio: ["pipe", "pipe", "pipe"],
      },
    );
    this.child = child;
    this.lines = createInterface({ input: child.stdout });
    this.lines.on("line", (line) => this.handleLine(line));
    child.stderr.on("data", (chunk: Buffer) => this.log(chunk.toString().trim()));
    child.once("error", (error) => this.failPending(error instanceof Error ? error : new Error(String(error))));
    child.once("exit", (code, signal) => {
      this.failPending(new Error(`Make It So sidecar exited (${code ?? signal ?? "unknown"})`));
      this.lines?.close();
      this.lines = undefined;
      this.child = undefined;
    });
    try {
      const health = await this.request("health");
      if (health.status !== "healthy") {
        throw new Error(`Make It So sidecar health was ${String(health.status ?? "missing")}`);
      }
      if (health.protocol_version !== SIDECAR_PROTOCOL_VERSION) {
        throw new Error(
          `Make It So sidecar protocol mismatch: expected ${SIDECAR_PROTOCOL_VERSION}, got ${String(health.protocol_version ?? "missing")}`,
        );
      }
    } catch (error) {
      child.kill();
      this.child = undefined;
      this.lines?.close();
      this.lines = undefined;
      throw error;
    }
  }

  public async request(
    method: string,
    params: Record<string, unknown> = {},
    timeoutMs = this.timeoutMs,
  ): Promise<RpcResult> {
    if (!this.running) {
      if (this.startPromise) await this.startPromise;
      else await this.start();
    }
    if (!this.child?.stdin.writable) throw new Error("Make It So sidecar is not writable");
    const id = this.nextId++;
    const request: SidecarRequest = { jsonrpc: "2.0", id, method, params };
    return new Promise<RpcResult>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`sidecar request timed out: ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.child?.stdin.write(`${JSON.stringify(request)}\n`);
    });
  }

  public async stop(): Promise<void> {
    const child = this.child;
    if (!child) return;
    this.failPending(new Error("Make It So sidecar stopped"));
    this.lines?.close();
    child.kill();
    this.child = undefined;
    this.lines = undefined;
  }

  private handleLine(line: string): void {
    try {
      const value: unknown = JSON.parse(line);
      if (!value || typeof value !== "object" || !("id" in value)) return;
      const response = value as { id: number; result?: RpcResult; error?: { message?: string } };
      const pending = this.pending.get(response.id);
      if (!pending) return;
      clearTimeout(pending.timer);
      this.pending.delete(response.id);
      if (response.error) {
        pending.reject(new Error(response.error.message ?? "sidecar request failed"));
      } else {
        pending.resolve(response.result ?? {});
      }
    } catch (error) {
      this.log("invalid sidecar response", error);
    }
  }

  private failPending(error: Error): void {
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      pending.reject(error);
      this.pending.delete(id);
    }
  }
}
