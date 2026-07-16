import { describe, expect, it } from "vitest";
import { isCaptainUiRequest } from "../src/control-ui-auth.js";

describe("Captain's Chair Control UI route guard", () => {
  it("accepts same-origin requests from the dashboard iframe", () => {
    expect(isCaptainUiRequest({ headers: { "sec-fetch-site": "same-origin" } })).toBe(true);
  });

  it("accepts a dashboard referer when fetch metadata is unavailable", () => {
    expect(isCaptainUiRequest({ headers: { referer: "https://openclaw.example/plugin?plugin=captains-chair&id=captains-chair" } })).toBe(true);
  });

  it("rejects direct and cross-site requests", () => {
    expect(isCaptainUiRequest({ headers: {} })).toBe(false);
    expect(isCaptainUiRequest({ headers: { "sec-fetch-site": "cross-site", referer: "https://evil.example/" } })).toBe(false);
  });
});
