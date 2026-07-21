import { execPath } from "node:process";
import { describe, expect, it } from "vitest";
import { SidecarSupervisor, withSidecarShutdown } from "../src/sidecar.js";

describe("SidecarSupervisor", () => {
  it("stops CLI sidecars after both successful and failed actions", async () => {
    let stops = 0;
    const sidecar = { stop: async () => { stops += 1; } };

    await expect(withSidecarShutdown(sidecar, async () => "ok")()).resolves.toBe("ok");
    await expect(withSidecarShutdown(sidecar, async () => { throw new Error("failed"); })()).rejects.toThrow("failed");
    expect(stops).toBe(2);
  });

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

  it("serializes concurrent startup requests onto one sidecar", async () => {
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

    await expect(Promise.all([
      supervisor.request("first", { value: 1 }),
      supervisor.request("second", { value: 2 }),
    ])).resolves.toEqual([
      { method: "first", params: { value: 1 } },
      { method: "second", params: { value: 2 } },
    ]);
    await supervisor.stop();
  });

  it("allows a long-running request to override the normal RPC timeout", async () => {
    const child = [
      "process.stdin.setEncoding('utf8');",
      "process.stdin.on('data',d=>d.split('\\n').filter(Boolean).forEach(line=>{const r=JSON.parse(line);const respond=()=>process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:r.id,result:r.method==='health'?{status:'healthy',protocol_version:1}:{method:r.method}})+'\\n');if(r.method==='health') respond(); else setTimeout(respond,700);}));",
    ].join("");
    const supervisor = new SidecarSupervisor({
      executable: execPath,
      args: ["-e", child, "--"],
      configPath: "unused.yaml",
      timeoutMs: 500,
    });

    await expect(supervisor.request("slow", {}, 2_000)).resolves.toEqual({ method: "slow" });
    await supervisor.stop();
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
