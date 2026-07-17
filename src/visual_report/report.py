# src/visual_report/report.py
"""
Public API for visual report generation — generate_visual_report and its
small JSON helpers (_json_for_script, json_dumps_str).
"""

import html
import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

from src.research_utils import strip_thinking
from urllib.parse import urlparse

from src.visual_report.helpers import (
    _md_to_html,
    _extract_headings,
    _apply_heading_ids,
    _inject_images,
    _category_css,
    _extract_report_title,
    _is_icon_or_logo_url,
    _TEMPLATE,
)

logger = logging.getLogger(__name__)


def generate_visual_report(
    question: str,
    report_markdown: str,
    sources: Optional[List[Dict]] = None,
    stats: Optional[Dict] = None,
    category: Optional[str] = None,
    session_id: Optional[str] = None,
    hidden_images: Optional[List[str]] = None,
) -> str:
    sources = sources or []
    stats = stats or {}
    hidden_images_set = set(hidden_images or [])

    # Strip thinking artifacts
    report_markdown = strip_thinking(report_markdown)

    # Use the report's first heading as the title (synthesized by the LLM)
    # rather than the raw user query. Fall back to the query if absent.
    synthesized, report_markdown = _extract_report_title(report_markdown, question)
    title_text = synthesized[:120] + ("..." if len(synthesized) > 120 else "")

    # Promote bold-only lines to ## headings if no markdown headings exist
    if not re.search(r'^#{2,3}\s+', report_markdown, re.MULTILINE):
        report_markdown = re.sub(
            r'^\*\*([^*]+)\*\*\s*$',
            lambda m: f'## {m.group(1).strip()}',
            report_markdown,
            flags=re.MULTILINE,
        )

    report_html = _md_to_html(report_markdown)

    headings = _extract_headings(report_markdown)
    report_html = _apply_heading_ids(report_html, headings)

    # Collect all OG images from sources (skip icons, tiny images, known junk)
    _IMAGE_BLOCKLIST = {
        "cdn.shopify.com/s/files/1/0179/4388/7926/files/icon.png",
    }
    _seen_images = set()
    all_images = []
    for s in sources:
        img = s.get("image", "")
        if (img and img.startswith("https://")
            and img not in _seen_images
            and img not in hidden_images_set
            and not img.endswith((".svg", ".ico", ".gif"))
            and not any(b in img for b in _IMAGE_BLOCKLIST)
            and not _is_icon_or_logo_url(img)):
            _seen_images.add(img)
            all_images.append(img)

    # Hero image = first available.
    hero_image_html = ""
    if all_images:
        hero_url = html.escape(all_images[0])
        hero_image_html = (
            f'<div class="hero-image" data-img-url="{hero_url}">'
            f'<img src="{hero_url}" alt="" loading="lazy" '
            f'onerror="this.parentElement.style.display=\'none\'">'
            f'</div>'
        )

    # Product quick-links bar
    if category == "product" and headings:
        product_headings = [h for h in headings if h["level"] == 3]
        if product_headings:
            pills = " ".join(
                f'<a href="#{h["slug"]}" class="quick-link">{html.escape(h["text"][:40])}</a>'
                for h in product_headings
            )
            report_html = f'<div class="quick-links-bar">{pills}</div>\n' + report_html

    # Inject remaining images between sections.
    section_pool = all_images[1:]
    report_html, _consumed = _inject_images(report_html, section_pool)
    spare_images = section_pool[_consumed:]

    # Build TOC
    toc_lines = []
    for h in headings:
        depth_class = f"depth-{h['level']}"
        toc_lines.append(
            f'<a href="#{h["slug"]}" class="{depth_class}">{html.escape(h["text"])}</a>'
        )
    toc_html = "\n      ".join(toc_lines) if toc_lines else ""

    # Build stats bar
    stat_items = []
    for key, label in [("Duration", "Duration"), ("Rounds", "Rounds"), ("Queries", "Queries"), ("URLs", "URLs Analyzed"), ("Model", "Model"), ("Search", "Search")]:
        val = stats.get(key)
        if val is not None:
            stat_items.append(
                f'<div class="stat"><span class="stat-value">{html.escape(str(val))}</span> {html.escape(label)}</div>'
            )
    stats_html = "\n  ".join(stat_items)

    # Build sources panel — compact collapsible list
    sources_html = ""
    if sources:
        items = []
        for i, s in enumerate(sources, 1):
            url = s.get("url", "")
            title = html.escape(s.get("title", "") or url)
            domain = ""
            try:
                domain = urlparse(url).hostname or ""
                if domain.startswith("www."):
                    domain = domain[4:]
            except Exception:
                domain = url
            items.append(
                f'<a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">'
                f'<span class="snum">{i}.</span>'
                f'<span>{title}</span>'
                f'<span class="sdomain">{html.escape(domain)}</span>'
                f'</a>'
            )
        sources_html = (
            '<div class="sources-panel">\n'
            '<details>\n'
            f'<summary>Sources ({len(sources)})</summary>\n'
            '<div class="sources-list">\n'
            + "\n".join(items)
            + "\n</div>\n</details>\n</div>"
        )

    timestamp = datetime.now().strftime("%B %d, %Y at %H:%M")

    # Build description for OG/meta tags (first 160 chars of plain text)
    desc_text = re.sub(r'[#*_\[\]()]', '', report_markdown)[:160].strip()
    og_image_meta = ""
    if all_images:
        og_image_meta = f'<meta property="og:image" content="{html.escape(all_images[0])}">'

    chat_cta_html = ""
    if session_id:
        chat_cta_html = (
            '<div class="chat-cta">'
            '<button id="btn-chat-about" class="chat-cta-btn" '
            f'data-research-id="{html.escape(session_id)}">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
            'width="18" height="18">'
            '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'
            '</svg>'
            '<span>Discuss</span>'
            '</button>'
            '<div class="chat-cta-hint">Opens a new chat with this report as context.</div>'
            '</div>'
        )

    # "Restore hidden images" toolbar button
    restore_btn_html = ""
    if session_id and hidden_images_set:
        restore_btn_html = (
            '<button id="btn-restore-images" type="button" '
            f'title="Restore {len(hidden_images_set)} hidden image'
            f'{"" if len(hidden_images_set) == 1 else "s"}">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M1 4v6h6"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>'
            '</svg>'
            f'Show hidden ({len(hidden_images_set)})'
            '</button>'
        )

    return _TEMPLATE.format(
        title=html.escape(title_text),
        description=html.escape(desc_text),
        og_image_meta=og_image_meta,
        question_html=html.escape(synthesized),
        hero_image_html=hero_image_html,
        stats_html=stats_html,
        toc_html=toc_html,
        report_html=report_html,
        sources_html=sources_html,
        chat_cta_html=chat_cta_html,
        restore_btn_html=restore_btn_html,
        timestamp=timestamp,
        category_css=_category_css(category),
        body_class=f"category-{html.escape(str(category))}" if category else "",
        session_id_js=json_dumps_str(session_id or ""),
        spare_images_js=_json_for_script(spare_images),
    )


def _json_for_script(value) -> str:
    """JSON-encode a value safe to embed inside a <script> block.

    json.dumps doesn't escape '/', so a string containing the literal
    substring '</script>' would terminate the script element early.
    Escape the closing slash to keep the inline JSON inert as HTML.
    """
    return json.dumps(value).replace("</", "<\\/")


def json_dumps_str(s: str) -> str:
    """JSON-encode a string so it's safe to embed inside a <script> block."""
    return _json_for_script(s)
