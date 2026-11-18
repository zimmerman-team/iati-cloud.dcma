#!/usr/bin/env bash
# Render a markdown file to a formatted PDF.
#
#   scripts/md_to_pdf.sh <input.md> [output.pdf]
#
# The output defaults to the input path with a .pdf extension. Pandoc turns the markdown (GitHub flavour) into a
# styled HTML page, Mermaid renders ```mermaid blocks in that page, and headless Chrome prints it to A4.
#
# Requires: pandoc, and google-chrome or chromium (override with CHROME=/path/to/browser).
# Mermaid is loaded from a CDN, so documents with diagrams need network access.
set -euo pipefail

MERMAID_URL="https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
RENDER_BUDGET_MS=20000 # time Chrome gives the page (script download, diagram rendering) before printing

usage() {
    echo "usage: $0 <input.md> [output.pdf]" >&2
    exit 2
}

find_chrome() {
    if [[ -n "${CHROME:-}" ]]; then
        echo "$CHROME"
        return
    fi
    for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
        if command -v "$candidate" >/dev/null; then
            echo "$candidate"
            return
        fi
    done
    echo "error: no Chrome or Chromium found; set CHROME=/path/to/browser" >&2
    exit 1
}

[[ $# -ge 1 && $# -le 2 ]] || usage
input=$1
[[ -f "$input" ]] || { echo "error: no such file: $input" >&2; exit 1; }
command -v pandoc >/dev/null || { echo "error: pandoc is not installed" >&2; exit 1; }
chrome=$(find_chrome)

input_abs=$(realpath "$input")
output=${2:-${input%.*}.pdf}
output_abs=$(realpath -m "$output")
title=$(basename "${input%.*}")

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cat >"$work/style.css" <<'CSS'
@page { size: A4; margin: 18mm 16mm; }
html { font-size: 10.5pt; }
body {
    font-family: "Inter", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    line-height: 1.5;
    color: #1f2328;
    max-width: none;
    margin: 0;
}
h1, h2, h3, h4 { line-height: 1.25; margin: 1.4em 0 0.5em; break-after: avoid; }
h1 { font-size: 1.9em; border-bottom: 2px solid #d0d7de; padding-bottom: 0.25em; margin-top: 0; }
h2 { font-size: 1.45em; border-bottom: 1px solid #d0d7de; padding-bottom: 0.2em; }
h3 { font-size: 1.2em; }
h4 { font-size: 1.05em; }
p, ul, ol { margin: 0 0 0.8em; }
a { color: #0969da; text-decoration: none; }
code {
    font-family: "JetBrains Mono", "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    font-size: 0.88em;
    background: #f3f4f6;
    padding: 0.1em 0.35em;
    border-radius: 4px;
}
pre {
    background: #f6f8fa;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 0.8em 1em;
    line-height: 1.35;
    overflow: hidden;
    white-space: pre-wrap;
    word-break: break-word;
    break-inside: avoid;
}
pre code { background: none; padding: 0; font-size: 0.85em; }
blockquote { margin: 0 0 0.8em; padding: 0.1em 1em; color: #59636e; border-left: 4px solid #d0d7de; }
table { border-collapse: collapse; width: 100%; margin: 0 0 1em; font-size: 0.9em; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
th, td { border: 1px solid #d0d7de; padding: 0.35em 0.6em; text-align: left; vertical-align: top; }
th { background: #f6f8fa; font-weight: 600; }
tbody tr:nth-child(even) td { background: #fafbfc; }
img { max-width: 100%; }
hr { border: 0; border-top: 1px solid #d0d7de; margin: 1.5em 0; }
.mermaid { text-align: center; margin: 1em 0; break-inside: avoid; }
.mermaid svg { max-width: 100%; height: auto; }
CSS

# Pandoc writes ```mermaid as <pre class="mermaid"><code>…</code></pre>. Mermaid wants the bare source in an element,
# so each block is swapped for a div holding its text before rendering. <base> keeps relative image links working.
cat >"$work/header.html" <<HTML
<base href="file://$(dirname "$input_abs")/">
<script src="$MERMAID_URL"></script>
<script>
document.addEventListener("DOMContentLoaded", async () => {
    if (typeof mermaid === "undefined") return;
    for (const pre of document.querySelectorAll("pre.mermaid")) {
        const div = document.createElement("div");
        div.className = "mermaid";
        div.textContent = pre.textContent;
        pre.replaceWith(div);
    }
    mermaid.initialize({ startOnLoad: false, theme: "neutral", securityLevel: "strict" });
    await mermaid.run();
});
</script>
HTML

pandoc "$input_abs" \
    --from gfm \
    --to html5 \
    --standalone \
    --metadata pagetitle="$title" \
    --css "$work/style.css" \
    --include-in-header "$work/header.html" \
    --output "$work/page.html"

mkdir -p "$(dirname "$output_abs")"
"$chrome" \
    --headless \
    --disable-gpu \
    --no-sandbox \
    --allow-file-access-from-files \
    --no-pdf-header-footer \
    --virtual-time-budget="$RENDER_BUDGET_MS" \
    --print-to-pdf="$output_abs" \
    "file://$work/page.html" 2>/dev/null

[[ -s "$output_abs" ]] || { echo "error: Chrome did not write $output_abs" >&2; exit 1; }
echo "Wrote $output"
