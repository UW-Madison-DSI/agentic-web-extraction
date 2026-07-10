"""Cache-stability text filters — an *example*, not part of the library.

The core library is deliberately site-agnostic: it knows nothing about any
particular website. But real sites embed *volatile* per-response fragments —
rotating anti-bot tokens, per-render timestamps, randomized honeypot labels,
shuffled recommendation carousels — that change the normalized markdown on every
fetch and so defeat the content-addressed page cache (the content hash never
repeats, so the cache never hits).

Each filter below is a pure ``str -> str`` transform that strips one such
volatile fragment so a page's content hash stays stable across fetches. They are
examples of what a *caller* passes to ``Extractor(text_filters=...)``; they live
here — keyed to specific real-world sites — precisely so the library itself
stays domain-agnostic. Every filter removes only content-free / invisible markup;
none removes text an LLM would use to judge a page.

Usage::

    from examples.strippers import CACHE_STABILITY_FILTERS
    from agentic_web_extraction import Extractor

    Extractor(schema=..., criteria=..., text_filters=CACHE_STABILITY_FILTERS)

Pick a subset if you only crawl some of these sites — the list is just a
convenience bundle.
"""

from __future__ import annotations

import re
from collections.abc import Callable

TextFilter = Callable[[str], str]

# Cloudflare's email obfuscation rewrites mailto links to
# `/cdn-cgi/l/email-protection#<hex>` and rotates that hex token on every
# response. Left in the markdown it changes the content hash on each fetch,
# permanently defeating the content cache for any page with an obfuscated email.
# Drop the volatile fragment so the normalized text is stable.
_CF_EMAIL_TOKEN = re.compile(r"(/cdn-cgi/l/email-protection)#[0-9a-fA-F]+")

# Foundant (grantinterface.com) dumps a diagnostic tooltip into the title
# attribute of its "provided by Foundant Technologies" footer link — a
# per-render server timestamp and a rotating hex token, both of which change on
# every response and permanently defeat the content cache for every portal page
# (login, register, apply). The whole title is an invisible tooltip with no
# value to the LLM, so drop it entirely. Anchored to the foundant.com footer
# link so it can match nothing else; `[^"]*` stops at the closing quote (the
# title contains no interior quote).
_FOUNDANT_FOOTER_TITLE = re.compile(r'(\]\(https://www\.foundant\.com)\s+"[^"]*"')

# Gravity Forms (and similar) ship an anti-bot **honeypot** field whose label is
# randomly rotated on every render — "Name", "Email", "Company", "Comments", … —
# to stop bots from auto-filling it. That rotating label is the only thing that
# changes between two fetches of the same form page (e.g. bwfund.org's
# "sign up for more information" pages), so it silently defeats the content
# cache. The label always sits immediately before Gravity Forms' fixed honeypot
# description; anchor on that exact sentence and drop the single preceding label
# paragraph. Fail-safe by construction: the lookbehind pins the match to a
# paragraph boundary and the `{1,40}` cap only ever matches a short honeypot
# label, so a genuine (longer) sentence in front of the sentinel can't be eaten.
_GFORMS_HONEYPOT = re.compile(
    r"(?<=\n\n)[^\n]{1,40}\n\n"
    r"(?=This field is for validation purposes and should be left unchanged\.)"
)

# EREF (erefdn.org) publication/`/product/` pages end with a "Related Guides &
# Reports" recommendation carousel whose items — links and titles of *other*
# products — are drawn at random and re-rolled whenever the site's page cache
# regenerates (stable within a request window, rotating over hours). It's a
# recommendation strip, not the page's own content, yet it changes the content
# hash run-over-run and so defeats the cache for nearly every EREF page. Drop
# the carousel, keeping the page's real content and the (stable) site footer
# that follows it. Fail-safe by construction: the match requires BOTH the exact
# carousel heading AND the footer's brand-logo link that begins the footer, so
# the non-greedy body can only ever span heading→footer; if either anchor is
# absent (any non-EREF or non-product page) nothing is stripped.
_EREF_RELATED = re.compile(
    r"\n#+ Related Guides & Reports\n.*?(?=\n\[!\[\]\([^)]*EREF-white-logo)",
    re.DOTALL,
)

# Some portals (e.g. CyberGrants' `ao_support.support` popups) render a page
# whose only body is an empty layout table — a run of `| | | |` cells plus the
# `| --- |` separator — and the *number* of empty columns varies per render, so
# the content hash never stabilises even though the page carries no text. Drop
# any table block that is made up **entirely** of pipes, dashes, colons and
# whitespace. This can't remove relevant content: a block is stripped only when
# it contains no other character, so a real table (any cell with a letter,
# digit, image, or symbol) fails the test and is kept in full, separator and all.
_TABLE_BLOCK = re.compile(r"(?m)(?:^[ \t]*\|.*(?:\n|$))+")
_TABLE_HAS_CONTENT = re.compile(r"[^|:\-\s]")


def strip_cloudflare_email(text: str) -> str:
    return _CF_EMAIL_TOKEN.sub(r"\1", text)


def strip_foundant_footer_title(text: str) -> str:
    return _FOUNDANT_FOOTER_TITLE.sub(r"\1", text)


def strip_gravity_forms_honeypot(text: str) -> str:
    return _GFORMS_HONEYPOT.sub("", text)


def strip_eref_related(text: str) -> str:
    return _EREF_RELATED.sub("", text)


def drop_empty_tables(text: str) -> str:
    return _TABLE_BLOCK.sub(
        lambda m: "" if _TABLE_HAS_CONTENT.search(m.group(0)) is None else m.group(0),
        text,
    )


# Convenience bundle: pass the whole set to `Extractor(text_filters=...)`, or
# import individual functions above for just the sites you crawl.
CACHE_STABILITY_FILTERS: list[TextFilter] = [
    strip_cloudflare_email,
    strip_foundant_footer_title,
    strip_gravity_forms_honeypot,
    strip_eref_related,
    drop_empty_tables,
]
