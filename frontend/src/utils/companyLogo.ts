// Maps a company name to its bundled brand logo, falling back to null when we don't
// have one (the caller then shows the initial-letter placeholder).
//
// Logo files live in src/assets/logos/<slug>.{svg,png} where <slug> is the slugified
// company name (e.g. "Google DeepMind" -> "google-deepmind"). import.meta.glob lets
// Vite fingerprint each file and rewrite the URL under the /static/ base, so we never
// hardcode hashed asset paths. SVGs are crisp brand marks; the few brands missing from
// the icon set use a PNG favicon fallback.
const modules = import.meta.glob("../assets/logos/*.{svg,png}", {
  eager: true,
  query: "?url",
  import: "default",
}) as Record<string, string>;

const logos: Record<string, string> = {};
for (const [path, url] of Object.entries(modules)) {
  const slug = path.split("/").pop()!.replace(/\.(svg|png)$/, "");
  logos[slug] = url;
}

export function companySlug(name: string): string {
  return (name || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function companyLogo(name: string): string | null {
  return logos[companySlug(name)] ?? null;
}
