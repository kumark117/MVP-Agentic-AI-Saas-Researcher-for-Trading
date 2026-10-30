#!/usr/bin/env node
// Checks a running production build: every <script> on each page must carry the nonce from
// that response's Content-Security-Policy header. A prerendered static shell (PPR / Cache
// Components) has no nonce, so the browser blocks its scripts and the app never starts
// (ADR-017). Usage: node scripts/check-csp-nonce.mjs [baseUrl]   (default http://localhost:3000)

const base = (process.argv[2] ?? "http://localhost:3000").replace(/\/+$/, "");
const pages = ["/login", "/register", "/research", "/about", "/history"];
let failed = 0;

for (const path of pages) {
  const res = await fetch(base + path, { redirect: "manual" });
  const csp = res.headers.get("content-security-policy") ?? "";
  const nonce = /'nonce-([^']+)'/.exec(csp)?.[1];
  if (res.status >= 300 && res.status < 400) {
    console.log(`skip ${path}: redirect ${res.status}`);
    continue;
  }
  const html = await res.text();
  const scripts = html.match(/<script\b[^>]*>/g) ?? [];
  const missing = scripts.filter((tag) => !nonce || !tag.includes(`nonce="${nonce}"`));
  const ok = res.status === 200 && nonce && scripts.length > 0 && missing.length === 0;
  console.log(
    `${ok ? "ok  " : "FAIL"} ${path}: status ${res.status}, ${scripts.length} scripts, ${missing.length} without the nonce`,
  );
  for (const tag of missing.slice(0, 3)) console.log(`       ${tag.slice(0, 120)}`);
  if (!ok) failed++;
}
process.exit(failed ? 1 : 0);
