import { describe, expect, it } from "vitest";
import {
  CONTROL_UI_TOKEN_HEADER,
  isMakeItSoUiRequest,
  rejectNonControlUiRequest,
} from "../src/control-ui-auth.js";

describe("Make It So Control UI route guard", () => {
  it("accepts same-origin requests from the dashboard iframe", () => {
    expect(isMakeItSoUiRequest({ headers: { "sec-fetch-site": "same-origin" } })).toBe(true);
  });

  it("accepts a dashboard referer when fetch metadata is unavailable", () => {
    expect(isMakeItSoUiRequest({ headers: { referer: "https://openclaw.example/plugin?plugin=make-it-so&id=make-it-so" } })).toBe(true);
    expect(isMakeItSoUiRequest({ headers: { referer: "https://openclaw.example/make-it-so/" } })).toBe(true);
    expect(isMakeItSoUiRequest({ headers: { origin: "null" } })).toBe(true);
    expect(isMakeItSoUiRequest({ headers: { "sec-fetch-dest": "iframe", "sec-fetch-mode": "navigate" } })).toBe(true);
  });

  it("rejects direct and cross-site requests", () => {
    expect(isMakeItSoUiRequest({ headers: {} })).toBe(false);
    expect(isMakeItSoUiRequest({ headers: { "sec-fetch-site": "cross-site", referer: "https://evil.example/" } })).toBe(false);
  });

  it("requires the embedded control token for API requests", () => {
    const bodies: string[] = [];
    const response = {
      statusCode: 200,
      setHeader: () => undefined,
      end: (body: string) => bodies.push(body),
    };

    expect(rejectNonControlUiRequest(
      { method: "POST", headers: { "sec-fetch-site": "same-origin" } },
      response,
      { token: "expected" },
    )).toBe(true);
    expect(response.statusCode).toBe(403);
    expect(rejectNonControlUiRequest(
      {
        method: "POST",
        headers: {
          "sec-fetch-site": "same-origin",
          [CONTROL_UI_TOKEN_HEADER]: "expected",
        },
      },
      response,
      { token: "expected" },
    )).toBe(false);
  });
});
