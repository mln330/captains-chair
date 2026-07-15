import { execPath } from "node:process";
import { describe, expect, it } from "vitest";
import { SidecarSupervisor } from "../src/sidecar.js";

describe("SidecarSupervisor", () => {
  it("starts, correlates JSON-RPC responses, and stops cleanly", async () => {
    const child = [
      "process.stdin.setEncoding('utf8');",
      "process.stdin.on('data',d=>d.split('\\n').filter(Boolean).forEach(line=>{const r=JSON.parse(line);const result=r.method==='health'?{status:'healthy',protocol_version:1}:{method:r.method,params:r.params};process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:r.id,result})+'\\n')}));",
    ].join("");
    const supervisor = new SidecarSupervisor({
      executable: execPath,
      args: ["-e", child, "--"],
      configPath: "unused.yaml",
      timeoutMs: 2_000,
    });

    await expect(supervisor.request("echo", { probe: true })).resolves.toEqual({
      method: "echo",
      params: { probe: true },
    });
    expect(supervisor.running).toBe(true);
    await supervisor.stop();
    expect(supervisor.running).toBe(false);
  });

  it("fails startup when the sidecar protocol is incompatible", async () => {
    const child = [
      "process.stdin.setEncoding('utf8');",
      "process.stdin.on('data',d=>d.split('\\n').filter(Boolean).forEach(line=>{const r=JSON.parse(line);process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:r.id,result:{status:'healthy',protocol_version:99}})+'\\n')}));",
    ].join("");
    const supervisor = new SidecarSupervisor({
      executable: execPath,
      args: ["-e", child, "--"],
      configPath: "unused.yaml",
      timeoutMs: 2_000,
    });

    await expect(supervisor.start()).rejects.toThrow(/protocol mismatch/);
    expect(supervisor.running).toBe(false);
  });
});
