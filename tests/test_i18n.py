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
from tests import test_readiness as engine_tests

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "aio_system_dashboard"
ZH_TW = "zh-TW"
CJK = re.compile(r"[㐀-鿿]")
# Text that is the same in both languages: product names, abbreviations, numbers with units.
SAME = {"AIO NAV", "GNSS / RTK", "GNSS", "LiDAR", "Wi-Fi", "ZUPT", "ZIHR", "NHC", "VUPT", "—", ""}
# Words docs/TERMINOLOGY.zh-TW.md replaces (the same list is checked in aio-nav-receiver), and by what.
AVOID = {"橫滾": "滾轉", "浮點解": "浮動解", "槓桿臂": "桿臂", "接收器": "接收機", "校正資料": "修正資料",
         "坐標": "座標", "分米": "公寸", "週內秒": "週秒", "時間戳(?!記)": "時間戳記", "回放": "重播",
         "重啟": "重新啟動", "操作員": "操作人員", "入站": "輸入", "運行": "運作或執行", "年齡": "延遲",
         "過舊": "延遲", "精度": "準確度、精密度或不確定度"}


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
            "Step 2 of 3: Initial heading": "步驟 2/3：初始航向",
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
    """One word for one meaning, in the user pages and in the zh-TW manuals."""

    def avoided(self, texts):
        return sorted({f"{w} -> {AVOID[w]}: {t[:60]}" for t in texts for w in AVOID if re.search(w, t)})

    def test_user_pages(self):
        texts = list(ZH.values()) + [template for _rx, template in PATTERNS]
        self.assertEqual(self.avoided(texts), [])
        self.assertEqual([t for t in texts if "光達" in t], [], "the pages write LiDAR")

    def test_manuals(self):
        lines = [line for f in sorted((ROOT / "docs").glob("*.zh-TW.md")) if f.name != "TERMINOLOGY.zh-TW.md"
                 for line in f.read_text(encoding="utf-8").splitlines()]
        self.assertGreater(len(lines), 1000)
        self.assertEqual(self.avoided(lines), [])


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
