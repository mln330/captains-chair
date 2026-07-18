const CONTROL_UI_PATH = "/plugin";
const PLUGIN_ID = "make-it-so";
const PLUGIN_UI_PATH = "/make-it-so/";

type RequestLike = { method?: string; headers?: Record<string, unknown> };
type ResponseLike = {
  statusCode: number;
  setHeader: (name: string, value: string) => void;
  end: (body: string) => void;
};
type GuardOptions = { token?: string; cors?: boolean };

export const CONTROL_UI_TOKEN_HEADER = "x-make-it-so-control-token";

function header(request: RequestLike, name: string): string | undefined {
  const value = request.headers?.[name];
  if (Array.isArray(value)) return value[0] ?? undefined;
  return typeof value === "string" ? value : undefined;
}

/**
 * Plugin tabs are rendered in a sandboxed same-origin iframe by OpenClaw.
 * The iframe cannot forward the parent dashboard's Gateway credential, so
 * plugin-owned UI routes authenticate the browser surface with fetch metadata.
 */
export function isMakeItSoUiRequest(request: RequestLike): boolean {
  const site = header(request, "sec-fetch-site")?.toLowerCase();
  if (site === "same-origin") return true;
  const destination = header(request, "sec-fetch-dest")?.toLowerCase();
  const mode = header(request, "sec-fetch-mode")?.toLowerCase();
  if (destination === "iframe" && (mode === "navigate" || mode === "nested-navigate")) return true;
  if (header(request, "origin")?.toLowerCase() === "null") return true;

  const referer = header(request, "referer") ?? header(request, "referrer");
  if (!referer) return false;
  try {
    const url = new URL(referer);
    return (url.pathname === CONTROL_UI_PATH
      && url.searchParams.get("plugin") === PLUGIN_ID
      && url.searchParams.get("id") === PLUGIN_ID)
      || url.pathname === PLUGIN_UI_PATH;
  } catch {
    return false;
  }
}

export function rejectNonControlUiRequest(
  request: RequestLike,
  response: ResponseLike,
  options: GuardOptions = {},
): boolean {
  if (options.cors !== false) {
    response.setHeader("access-control-allow-origin", "*");
    response.setHeader("access-control-allow-methods", "GET,POST,OPTIONS");
    response.setHeader(
      "access-control-allow-headers",
      `content-type,${CONTROL_UI_TOKEN_HEADER}`,
    );
  }
  if (request.method?.toUpperCase() === "OPTIONS") {
    response.statusCode = 204;
    response.end("");
    return true;
  }
  const tokenMatches = options.token === undefined
    || header(request, CONTROL_UI_TOKEN_HEADER) === options.token;
  if (isMakeItSoUiRequest(request) && tokenMatches) return false;
  response.statusCode = 403;
  response.setHeader("content-type", "application/json; charset=utf-8");
  response.end(JSON.stringify({ error: { message: "Make It So UI requests must originate from the OpenClaw Control UI.", type: "forbidden" } }));
  return true;
}
