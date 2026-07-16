const CONTROL_UI_PATH = "/plugin";
const PLUGIN_ID = "captains-chair";

type RequestLike = { headers?: Record<string, unknown> };

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
export function isCaptainUiRequest(request: RequestLike): boolean {
  const site = header(request, "sec-fetch-site")?.toLowerCase();
  if (site === "same-origin") return true;

  const referer = header(request, "referer") ?? header(request, "referrer");
  if (!referer) return false;
  try {
    const url = new URL(referer);
    return url.pathname === CONTROL_UI_PATH
      && url.searchParams.get("plugin") === PLUGIN_ID
      && url.searchParams.get("id") === PLUGIN_ID;
  } catch {
    return false;
  }
}

export function rejectNonControlUiRequest(request: RequestLike, response: { statusCode: number; setHeader: (name: string, value: string) => void; end: (body: string) => void }): boolean {
  if (isCaptainUiRequest(request)) return false;
  response.statusCode = 403;
  response.setHeader("content-type", "application/json; charset=utf-8");
  response.end(JSON.stringify({ error: { message: "Captain's Chair UI requests must originate from the OpenClaw Control UI.", type: "forbidden" } }));
  return true;
}
