import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const source = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), "../ui/src/main.tsx"), "utf8");

describe("shared control UI contract", () => {
  it("keeps the core operator workflows visible", () => {
    expect(source).toContain("Register repository");
    expect(source).toContain("Create from the Chair");
    expect(source).toContain("repos/create");
    expect(source).toContain("repository is created only after the course passes readiness");
    expect(source).toContain("Managed schedules");
    expect(source).toContain("Run course review");
    expect(source).toContain("Save cadence");
    expect(source).toContain("Application surface");
    expect(source).toContain("model_profiles");
    expect(source).toContain("models/validate");
    expect(source).toContain("models/config");
    expect(source).toContain("models/update");
    expect(source).toContain("Global and runtime routes");
    expect(source).toContain("courses/list");
    expect(source).toContain("Start a planning session");
    expect(source).toContain("Initial work packages");
    expect(source).toContain("course/planning-session");
    expect(source).toContain("course/models");
    expect(source).toContain("Course and package model routes");
    expect(source).toContain("Save model routes");
    expect(source).toContain("Effective route preview");
    expect(source).toContain("Open planning brief");
    expect(source).toContain("course/requirement");
    expect(source).toContain("Attention queue");
    expect(source).toContain("Acknowledge");
    expect(source).toContain("Work-package dependency map");
    expect(source).toContain("Token efficiency by course, package, stage, model, and date");
  });
});
