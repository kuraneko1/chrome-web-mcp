"""Rendered-page extraction: JS DOM walks and shaped-markdown helpers.

This module owns the in-page JavaScript used to snapshot rendered content
(``_EXTRACT_RESULTS_JS`` for Google result candidates, ``_FETCH_*_JS`` for text,
markdown and links) together with the pure post-processing: ``_shape_markdown``
runs trafilatura first, html2text second and reports the method it used, and
``_smart_cut`` truncates at a sentence/word boundary. ``MAX_EXTRACTION_CHARS``
caps what the CDP reader will accept. ``server`` re-exports the names its call
sites and tests use.
"""

from __future__ import annotations

import re

try:
    import trafilatura
except ImportError:  # pragma: no cover - dependency declared in pyproject
    trafilatura = None  # type: ignore[assignment]
try:
    import html2text
except ImportError:  # pragma: no cover - dependency declared in pyproject
    html2text = None  # type: ignore[assignment]


MAX_EXTRACTION_CHARS = 16 * 1024 * 1024


_EXTRACT_RESULTS_JS = r"""(() => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim();
  const output = [];
  for (const anchor of document.querySelectorAll('a[href]')) {
    const heading = anchor.querySelector('h3');
    if (!heading) continue;
    const title = clean(heading.innerText || heading.textContent);
    if (!title) continue;
    const block = anchor.closest('[data-hveid]') || anchor.parentElement?.parentElement || anchor.parentElement;
    const blockText = clean(block?.innerText || '');
    let description = blockText;
    if (description.startsWith(title)) description = clean(description.slice(title.length));
    output.push({title, href: anchor.href || '', description: description.slice(0, 1000)});
    if (output.length >= 40) break;
  }
  return JSON.stringify(output);
})()"""


# Extract clean, readable text from a rendered page (no scripts/styles/nav).
_FETCH_TEXT_JS = r"""(() => {
  const clone = document.body ? document.body.cloneNode(true) : document;
  for (const sel of ['script','style','noscript','svg','canvas','iframe','nav','footer','header','[aria-hidden="true"]']) {
    clone.querySelectorAll(sel).forEach(el => el.remove());
  }
  const title = (document.title || '').trim();
  const text = (clone.innerText || '').replace(/ /g, ' ');
  return JSON.stringify({title, text});
})()"""


# Extract lightweight markdown (headings/paragraphs/lists) plus page links.
_FETCH_MD_JS = r"""(() => {
  const clean = v => (v || '').replace(/\s+/g, ' ').trim();
  const title = (document.title || '').trim();
  const links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const t = clean(a.innerText || a.textContent);
    let h = '';
    try { h = new URL(a.href, location.href).href; } catch (e) { continue; }
    if (!/^https?:\/\//i.test(h)) continue;
    if (t) links.push({text: t.slice(0, 200), url: h});
    if (links.length >= 200) break;
  }
  const parts = [];
  let total = 0;
  for (const el of document.querySelectorAll('h1,h2,h3,p,li,pre')) {
    if (!el.isConnected) continue;
    const t = clean(el.innerText || el.textContent);
    if (!t) continue;
    const tag = el.tagName;
    let piece = t;
    if (tag === 'H1') piece = '# ' + t;
    else if (tag === 'H2') piece = '## ' + t;
    else if (tag === 'H3') piece = '### ' + t;
    else if (tag === 'LI') piece = '- ' + t;
    else if (tag === 'PRE') piece = '```\n' + t.slice(0, 2000) + '\n```';
    parts.push(piece);
    // Track the joined length as we go: re-joining the whole array on every
    // iteration made the loop quadratic in the number of blocks (F10).
    total += piece.length + 2;
    if (total > 300000) break;
  }
  let markdown = parts.join('\n\n');
  if (!markdown) markdown = clean(document.body ? document.body.innerText : '');
  return JSON.stringify({title, markdown, links});
})()"""


# Page links as follow-up crawl targets (used with shaped markdown).
_FETCH_LINKS_JS = r"""(() => {
  const clean = v => (v || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const t = clean(a.innerText || a.textContent);
    let h = '';
    try { h = new URL(a.href, location.href).href; } catch (e) { continue; }
    if (!/^https?:\/\//i.test(h)) continue;
    if (t) out.push({text: t.slice(0, 200), url: h});
    if (out.length >= 200) break;
  }
  return JSON.stringify(out);
})()"""


def _shape_markdown(html: str, url: str) -> tuple[str, str]:
    """Shape rendered HTML into boilerplate-free markdown.

    Returns (markdown, extraction): trafilatura first, html2text fallback,
    dom-walk last resort. An empty markdown with extraction "none" means the
    shaper saw nothing usable (caller: retry with format "text").
    """
    if trafilatura is not None:
        try:
            shaped = trafilatura.extract(
                html,
                output_format="markdown",
                include_links=True,
                include_images=False,
                url=url,
                deduplicate=True,
            )
        except Exception:
            shaped = None
        if shaped and len(shaped.strip()) > 200:
            return shaped.strip(), "trafilatura"
    if html2text is not None:
        try:
            conv = html2text.HTML2Text()
            conv.body_width = 0
            conv.ignore_images = True
            shaped = conv.handle(html)
        except Exception:
            shaped = None
        if shaped and shaped.strip():
            return shaped.strip(), "html2text"
    return "", "none"


def _smart_cut(text: str, limit: int) -> tuple[str, bool]:
    """Cut text at a sentence/word boundary; return (cut, truncated)."""
    if len(text) <= limit:
        return text, False
    window = text[:limit]
    boundary = -1
    for match in re.finditer(r"[。.!?！？]", window):
        boundary = match.end()
    if boundary > limit * 0.5:
        return window[:boundary].rstrip(), True
    space = window.rfind(" ")
    if space > limit * 0.5:
        return window[:space], True
    return window, True
