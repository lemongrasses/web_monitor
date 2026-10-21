"""Languages of the user pages (:8080): English, and Traditional Chinese.

The dashboard works in English everywhere (logs, events, the readiness snapshot, the 0x05
packet, the Maintenance pages). Only what the user pages show is translated, at the edge:

* templates: ``{{ _("Text") }}``; the language comes from the ``aio_lang`` cookie (the menu in
  the top bar), else the browser's Accept-Language, else English;
* scripts: ``AIO.tr("Text")`` / ``AIO.trf("Text {0}", x)`` look the phrase up in the table the
  page embeds (``i18n_js``);
* API answers of the user app: the text fields (``TEXT_KEYS``) of the JSON are translated just
  before they are sent. Phrases with numbers or names in them are matched by ``PATTERNS``; the
  parts a pattern captures are translated again (so "Waiting for: IMU data" works). A phrase with
  no translation stays English: never wrong, at worst untranslated. tests/test_i18n.py checks
  that what the pages show in the usual states is covered.
"""

import functools
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import g, has_request_context

from .i18n_zh import PATTERNS as _ZH_PATTERNS, ZH as _ZH

LANGS: Tuple[Tuple[str, str], ...] = (("en", "English"), ("zh-TW", "繁體中文"))
CODES = tuple(c for c, _n in LANGS)
COOKIE = "aio_lang"

# JSON keys whose string values are words for people (anything else is data: names, addresses...).
TEXT_KEYS = frozenset({
    "title", "detail", "label", "name", "summary", "reasons", "action_text", "hint", "reason",
    "client", "risks", "k", "v", "text",
})

_TABLES: Dict[str, Tuple[Dict[str, str], List[Tuple["re.Pattern", str]]]] = {
    "zh-TW": (_ZH, [(re.compile("^" + p + "$", re.S), t) for p, t in _ZH_PATTERNS]),
}
_SEPARATORS = (("; ", "；"), (", ", "、"))
# A space between two Chinese characters (from joining translated parts) is dropped.
_CJK_GAP = re.compile(r"(?<=[\u3400-\u9fff）」、])[ ]+(?=[\u3400-\u9fff（「])")


def pick_lang(cookie: Optional[str], accept: Any = None) -> str:
    """The cookie if valid, else the browser's preference (zh -> Traditional Chinese), else English."""
    if cookie in CODES:
        return cookie
    if accept is not None:
        best = accept.best_match(["en", "zh-TW", "zh"], default="en")
        if best and best.lower().startswith("zh"):
            return "zh-TW"
    return "en"


@functools.lru_cache(maxsize=4096)
def tr(text: str, lang: str) -> str:
    """One phrase in ``lang``. Unknown phrases come back unchanged."""
    if lang == "en" or not text or lang not in _TABLES:
        return text
    out = _tr(text, lang)
    return _CJK_GAP.sub("", out) if out is not text else out


def _tr(text: str, lang: str) -> str:
    exact, patterns = _TABLES[lang]
    hit = exact.get(text)
    if hit is not None:
        return hit
    stripped = text.rstrip(".")
    if stripped != text and stripped in exact:
        return exact[stripped]
    for rx, template in patterns:
        m = rx.match(text)
        if m:
            return template.format(*(tr(x, lang) if x else "" for x in m.groups()))
    for sep, zh_sep in _SEPARATORS:                  # "a; b" -> each part
        if sep in text:
            parts = [tr(p, lang) for p in text.split(sep)]
            if any(p != o for p, o in zip(parts, text.split(sep))):
                return zh_sep.join(parts)
    return text


def gettext(text: str) -> str:
    """``_()`` in templates: the request's language (English outside the user app)."""
    lang = g.get("lang", "en") if has_request_context() else "en"
    return tr(text, lang)


def translate(obj: Any, lang: str, key: Optional[str] = None) -> Any:
    """The text fields of a JSON value in ``lang``; everything else as it is."""
    if lang == "en":
        return obj
    if isinstance(obj, dict):
        return {k: translate(v, lang, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [translate(v, lang, key) for v in obj]
    if isinstance(obj, str) and key in TEXT_KEYS:
        return tr(obj, lang)
    return obj


def js_table(lang: str) -> Dict[str, str]:
    """The phrase table a page embeds for its scripts (empty for English)."""
    return dict(_TABLES[lang][0]) if lang in _TABLES else {}


def untranslated(obj: Any, lang: str, keep: Callable[[str], bool], key: Optional[str] = None) -> List[str]:
    """Text fields that stay English after translation (for tests). ``keep`` accepts strings that
    need no translation (names, numbers, units)."""
    out: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += untranslated(v, lang, keep, k)
    elif isinstance(obj, list):
        for v in obj:
            out += untranslated(v, lang, keep, key)
    elif isinstance(obj, str) and key in TEXT_KEYS and obj and tr(obj, lang) == obj and not keep(obj):
        out.append(obj)
    return out
