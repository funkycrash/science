import type { Config, Context } from "@netlify/edge-functions";

// HTTP Basic Auth gate for the whole site.
// Set SITE_PASSWORD in Netlify: Project configuration > Environment variables.
// The username must be "funkycrash". The password must match SITE_PASSWORD.
const USERNAME = "funkycrash";

export default async (request: Request, context: Context) => {
  const expected = Netlify.env.get("SITE_PASSWORD");
  if (!expected) {
    return new Response("SITE_PASSWORD is not configured.", { status: 503 });
  }

  const header = request.headers.get("authorization") ?? "";
  if (header.startsWith("Basic ")) {
    let decoded = "";
    try {
      decoded = atob(header.slice(6));
    } catch {
      // malformed base64, fall through to 401
    }
    const separator = decoded.indexOf(":");
    const username = decoded.slice(0, separator);
    const password = decoded.slice(separator + 1);
    if (separator !== -1 && username === USERNAME && password === expected) {
      return context.next();
    }
  }

  return new Response("Authentication required.", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Brief.science archive", charset="UTF-8"' },
  });
};

export const config: Config = { path: "/*" };
