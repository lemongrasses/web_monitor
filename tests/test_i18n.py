"""User pages in Traditional Chinese (web/i18n.py): every phrase the pages and scripts use has a
translation, what the product state says in the usual situations is translated, and the wording
follows docs/TERMINOLOGY.zh-TW.md (shared with aio-nav-receiver)."""

import glob
import re
import string
import unittest
from pathlib import Path

from aio_system_dashboard.web import i18n
from aio_system_dashboard.web.i18n_zh import PATTERNS, ZH
from tests import manuals
from tests import test_readiness as engine_tests

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "aio_system_dashboard"
ZH_TW = "zh-TW"
CJK = re.compile(r"[㐀-鿿]")
# Text that is the same in both languages: product names, abbreviations, numbers with units.
SAME = {"AIO NAV", "GNSS / RTK", "GNSS", "LiDAR", "Wi-Fi", "ZUPT", "ZIHR", "NHC", "VUPT", "—", ""}
GLOSSARY = manuals.read("docs/TERMINOLOGY.zh-TW.md")   # the same file in aio-nav-receiver


def glossary(heading):
    """The rows (lists of cells) of the table under ``heading`` in docs/TERMINOLOGY.zh-TW.md."""
    return table_after(GLOSSARY, f"\n{heading}\n")


def table_after(text, marker):
    """The rows of the first Markdown table after ``marker``, without the header and the |---| line."""
    rows = []
    for line in (marker + text.split(marker, 1)[1]).splitlines():
        if line.startswith("|"):
            rows.append([cell.strip().strip("`") for cell in line.strip().strip("|").split("|")])
        elif rows:
            break
    return rows[2:]


def avoided(texts):
    """The glossary's 不使用的詞 found in ``texts``, except as part of the word to use (時間戳 in 時間戳記)."""
    found = set()
    for avoid, use, _why in glossary("## 不使用的詞"):
        keep_ = [(w, w.index(avoid)) for w in use.split("、") if avoid in w]
        for text in texts:
            for m in re.finditer(re.escape(avoid), text):
                if not any(m.start() >= i and text.startswith(w, m.start() - i) for w, i in keep_):
                    found.add(f"{avoid} -> {use}: {text[:60]}")
    return sorted(found)


def keep(text: str) -> bool:
    return (text in SAME or bool(CJK.search(text)) or re.fullmatch(r"[a-z]+\d\w*", text) is not None      # eth0
            or re.fullmatch(r"[\d\s.:,%/()#·+\-]*(Hz|ms|s|m|GB)?", text) is not None)


def ui_phrases():
    """Every literal the user pages translate: _("...") in templates, AIO.tr / trf in scripts,
    and the scripts' phrase tables."""
    out = set()
    for f in glob.glob(str(PKG / "templates/product/*.html")) + [str(PKG / "templates/macros.html")]:
        s = Path(f).read_text(encoding="utf-8")
        out |= set(re.findall(r'''\b_\(\s*"([^"]+)"\s*\)''', s)) | set(re.findall(r"""\b_\(\s*'([^']+)'\s*\)""", s))
        out |= set(re.findall(r"""AIO\.trf?\(\s*'([^']+)'\s*[,)]""", s)) | set(re.findall(r'''AIO\.trf?\(\s*"([^"]+)"\s*[,)]''', s))
        out |= set(re.findall(r"\('\w+', '/\w*', '([^']+)'\)", s)) | set(re.findall(r"\{\{ row\('([^']+)'", s))
    js = (PKG / "static/js/product.js").read_text(encoding="utf-8")
    out |= set(re.findall(r'''\btrf?\(\s*"([^"]+)"\s*[,)]''', js))
    for block in re.findall(r"const (?:ACTION|WIFI_FAIL|STATE_LABEL) = \{(.*?)\};", js, re.S):
        out |= set(re.findall(r'"([A-Z][^"]{2,})"', block))
    return out


class TableTest(unittest.TestCase):
    def test_every_page_and_script_phrase_is_translated(self):
        phrases = ui_phrases()
        self.assertGreater(len(phrases), 100)
        missing = sorted(p for p in phrases if p not in ZH)
        self.assertEqual(missing, [], "add these to web/i18n_zh.py")

    def test_patterns_compile_and_fill(self):
        for rx, template in PATTERNS:
            groups = re.compile(rx).groups
            used = {int(i) for _l, i, _f, _c in string.Formatter().parse(template) if i is not None and i != ""}
            self.assertTrue(used <= set(range(groups)), (rx, template))

    def test_numbers_and_names_go_through_patterns(self):
        cases = {
            "Step 2 of 3: Initial heading": "對準第 2/3 階段：初始航向",
            "Waiting for IMU data, GNSS position": "等待 IMU 資料、GNSS 位置",
            "Camera not connected": "相機未連線",
            "GNSS SPP; needs rtk float or better": "GNSS 單點定位（SPP）；需要 RTK 浮動解或更好",
            "Changing Wi-Fi now may interrupt: navigation is in use (READY)": "現在變更 Wi-Fi 可能會中斷：導航正在使用中（就緒）",
            "192.0.2.10:9000": "192.0.2.10:9000",                      # data stays as it is
        }
        for en, zh in cases.items():
            self.assertEqual(i18n.tr(en, ZH_TW), zh)
        self.assertEqual(i18n.tr("Step 2 of 3: Initial heading", "en"), "Step 2 of 3: Initial heading")

    def test_only_text_fields_are_translated(self):
        obj = {"title": "Ready", "ssid": "Ready", "rows": [{"k": "Now", "v": "RTK fix"}], "state": "READY"}
        self.assertEqual(i18n.translate(obj, ZH_TW),
                         {"title": "就緒", "ssid": "Ready", "rows": [{"k": "目前", "v": "RTK 固定解"}], "state": "READY"})

    def test_language_choice(self):
        class Accept:
            def __init__(self, best):
                self.best = best

            def best_match(self, offers, default=None):
                return self.best

        self.assertEqual(i18n.pick_lang("zh-TW", Accept("en")), "zh-TW")
        self.assertEqual(i18n.pick_lang("fr", Accept("zh")), "zh-TW")
        self.assertEqual(i18n.pick_lang(None, Accept("en")), "en")
        self.assertEqual(i18n.pick_lang(None, None), "en")


class TerminologyTest(unittest.TestCase):
    """One word for one meaning, in the user pages and in the zh-TW manuals, as docs/TERMINOLOGY.zh-TW.md says."""

    def test_user_pages(self):
        texts = list(ZH.values()) + [template for _rx, template in PATTERNS]
        self.assertEqual(avoided(texts), [])
        self.assertEqual([t for t in texts if "光達" in t], [], "the pages write LiDAR")

    def test_manuals(self):
        lines = [line for f in manuals.glob("docs/*.zh-TW.md") if f != "docs/TERMINOLOGY.zh-TW.md"
                 for line in manuals.read(f).splitlines()]
        self.assertGreater(len(lines), 1000)
        self.assertEqual(avoided(lines), [])

    def test_operator_instructions_are_the_glossarys(self):
        from aio_system_dashboard.state import readiness
        rows = glossary("### 要求的動作（requested_action）")
        self.assertEqual([r[1] for r in rows], list(readiness.ACTIONS))
        for _code, name, _vehicle, page, _en in rows:
            text = readiness.ACTION_TEXT[name]
            self.assertEqual(i18n.tr(text, ZH_TW) if text else "（不顯示）", page, name)

    def test_alignment_lamps_are_the_glossarys(self):
        page = (PKG / "templates/product/navigation.html").read_text(encoding="utf-8")
        for _flag, zh, _vehicle_en, label in glossary("### 對準旗標（0x04 flags）"):
            self.assertIn(f'link(_("{label}")', page)
            self.assertEqual(i18n.tr(label, ZH_TW), zh, label)

    def test_status_packet_tables_in_the_manual_are_the_glossarys(self):
        manual = manuals.read("docs/OPERATION.zh-TW.md")
        states = table_after(manual, "| 代碼 | 狀態 | 車端顯示 | 意義 |")
        self.assertEqual([r[:3] for r in states], [r[:3] for r in glossary("### 狀態（state）")])
        actions = table_after(manual, "| 代碼 | 要求的動作 | 儀表板顯示 | 車端顯示 |")
        self.assertEqual(actions, [[r[0], r[1], r[3], r[2]] for r in glossary("### 要求的動作（requested_action）")])
        bits = table_after(manual, "| 位元 | health_flags | 成立條件 | inhibit_flags | 不可用原因 |")
        self.assertEqual(bits, [h[:3] + i[1:3] for h, i in zip(glossary("### 成立條件（health_flags）"),
                                                               glossary("### 不可用原因（inhibit_flags）"))])
        quality = "、".join(f"{r[0]} {r[1]}" for r in glossary("### GNSS 品質（gnss_quality）"))
        self.assertIn(quality.replace("單點定位", "單點定位（SPP）"), manual)


class StateCoverageTest(unittest.TestCase):
    """Drive the health engine through the usual situations; the user pages' text must be Chinese."""

    E = engine_tests.EngineTest                                  # its harness, not its tests
    setUp, engine, gnss, run_for = E.setUp, E.engine, E.gnss, E.run_for
    new_process, stop_process = E.new_process, E.stop_process
    del E

    def collect(self, seen):
        p = self.store.get("product")
        part = {k: p.get(k) for k in ("health", "checks", "indicators", "udp", "advisories")}
        seen.update(i18n.untranslated(part, ZH_TW, keep))

    def test_usual_situations_are_translated(self):
        from aio_system_dashboard.actions import process_control
        seen = set()
        self.data["autostart"]["start_services"] = ["drivers"]
        self.data["services"]["drivers"] = {"label": "Sensor drivers", "user_unit": "d.service"}
        self.data["devices"] = {"camera": {"label": "Camera", "host": "192.0.2.5"}}
        eng = self.engine()
        auto = {"enabled": True, "state": "waiting", "detail": "Waiting for IMU data, GNSS position", "hold": False}
        eng.autostart_status = lambda: dict(auto)
        self.stop_process()
        for _ in range(3):                                       # boot, checking
            self.run_for(eng, 2)
            self.collect(seen)
        auto.update(state="prereq_timeout", detail="IMU data not available after 120 s")
        self.run_for(eng, 2)
        self.collect(seen)
        auto.update(state="starting", detail="Starting AIO NAV (attempt 1 of 5)")
        process_control.set_wanted(self.cfg, "aio_nav", True)
        self.new_process()
        self.run_for(eng, 2)
        self.collect(seen)
        auto.update(state="running", detail="")
        for flags in ({"alignment": False}, {"alignment": True}, {"alignment": True, "heading_valid": True}, engine_tests.ALIGNED):
            self.nav.latest, self.nav.age = dict(flags), 0.01
            self.run_for(eng, 3)
            self.collect(seen)
        self.gnss("spp")                                         # RTK lost after Ready: degraded
        self.store.set("network", {"devices": {"camera": {"reachable": False, "host": "192.0.2.5"}},
                                   "sensor_link_up": False, "sensor_interface": "eth0"})
        self.run_for(eng, 5)
        self.collect(seen)
        self.gnss("none")
        self.run_for(eng, 3)
        self.collect(seen)
        self.nav.age = 9.0                                       # output stops
        self.run_for(eng, 3)
        self.collect(seen)
        self.clock.t += 100
        self.stop_process()                                      # crash after Ready
        self.run_for(eng, 4)
        self.collect(seen)
        process_control.set_wanted(self.cfg, "aio_nav", False)   # operator stop
        auto.update(state="held", detail="Stopped by an operator; press Start to resume", hold=True)
        self.run_for(eng, 4)
        self.collect(seen)
        self.assertEqual(sorted(seen), [], "add these to web/i18n_zh.py")


if __name__ == "__main__":
    unittest.main()
