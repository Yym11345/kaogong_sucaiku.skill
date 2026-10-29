"""skill 入口脚本的测试。

只测不联网的部分：路径解析、参数校验、卡片渲染、数据读写。
抓取本身依赖外部站点，不进单元测试。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 兼容两种布局：主仓库 skill/scripts/，发布版 scripts/
SCRIPT = os.path.join(ROOT, "skill", "scripts", "kaogong.py")
if not os.path.isfile(SCRIPT):
    SCRIPT = os.path.join(ROOT, "scripts", "kaogong.py")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load():
    spec = importlib.util.spec_from_file_location("kaogong_cli", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


K = _load()


class TestLocate(unittest.TestCase):
    def test_find_root_finds_repo(self):
        self.assertEqual(os.path.abspath(K.find_root()),
                         os.path.abspath(ROOT))

    def test_find_root_respects_env(self):
        old = os.environ.get("KAOGONG_HOME")
        os.environ["KAOGONG_HOME"] = ROOT
        try:
            self.assertEqual(os.path.abspath(K.find_root()),
                             os.path.abspath(ROOT))
        finally:
            if old is None:
                os.environ.pop("KAOGONG_HOME", None)
            else:
                os.environ["KAOGONG_HOME"] = old

    def test_data_dir_env_override(self):
        old = os.environ.get("KAOGONG_DATA")
        os.environ["KAOGONG_DATA"] = os.path.join(ROOT, "_tmp_kaogong")
        try:
            self.assertTrue(K.data_dir().endswith("_tmp_kaogong"))
            self.assertTrue(K.db_path().endswith("kaogong.db"))
        finally:
            if old is None:
                os.environ.pop("KAOGONG_DATA", None)
            else:
                os.environ["KAOGONG_DATA"] = old

    def test_load_engine_exposes_sources(self):
        _root, _cls, sources, _store, _run = K.load_engine()
        self.assertIn("人民日报", sources.LISTERS)
        self.assertEqual(len(sources.LISTERS), 5)


class TestCli(unittest.TestCase):
    def test_days_out_of_range_rejected(self):
        import contextlib
        import io
        for bad in ("0", "31"):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    K.main(["show", "--days", bad])

    def test_no_subcommand_prints_help(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(K.main([]), 0)

    def test_defaults(self):
        ap = K.build_parser()
        a = ap.parse_args(["show"])
        self.assertEqual(a.days, 1)
        self.assertEqual(a.limit, 20)
        self.assertFalse(a.no_fetch)
        self.assertIsNone(a.keep)

    def test_every_subcommand_has_func(self):
        ap = K.build_parser()
        for name in ("show", "fetch", "search", "memo", "deep",
                     "stats", "clean", "doctor"):
            a = ap.parse_args([name] + (["词"] if name == "search" else []))
            self.assertTrue(callable(getattr(a, "func", None)), name)


class TestRender(unittest.TestCase):
    ART = {
        "title": "测试标题", "source": "人民日报", "channel": "05版·评论",
        "date": "2026-10-29", "score": 88, "url": "http://example.com/a",
        "topics": ["基层治理"], "tracks": ["申论素材"],
        "summary": "核心观点一句话", "angles": ["可命制归纳概括题"],
        "body": "正文" * 100,
    }

    def test_card_contains_essentials(self):
        s = K._card(self.ART, 1)
        for piece in ("## 1. 测试标题", "人民日报", "2026-10-29",
                      "命题价值 88", "核心观点一句话", "可命制归纳概括题",
                      "http://example.com/a"):
            self.assertIn(piece, s)

    def test_card_prefers_summary_over_reason(self):
        a = dict(self.ART, reason="判定依据文本")
        self.assertIn("核心观点一句话", K._card(a, 1))
        self.assertNotIn("判定依据文本", K._card(a, 1))

    def test_card_falls_back_to_reason(self):
        a = dict(self.ART, summary="")
        a["reason"] = "判定依据文本"
        self.assertIn("判定依据文本", K._card(a, 1))

    def test_loads_handles_bad_json(self):
        self.assertEqual(K._loads(None, []), [])
        self.assertEqual(K._loads("", []), [])
        self.assertEqual(K._loads("not json", []), [])
        self.assertEqual(K._loads('["a"]', []), ["a"])
        self.assertEqual(K._loads(["a"], []), ["a"])


class TestStore(unittest.TestCase):
    """用临时库验证 skill 自己的读写路径，不碰仓库的 data/kaogong.db。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kaogong_test_")
        self.old = os.environ.get("KAOGONG_DATA")
        os.environ["KAOGONG_DATA"] = self.tmp
        _root, _cls, _src, store, _run = K.load_engine()
        self.store = store
        self.con = store.connect(K.db_path())

    def tearDown(self):
        self.con.close()
        if self.old is None:
            os.environ.pop("KAOGONG_DATA", None)
        else:
            os.environ["KAOGONG_DATA"] = self.old
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _art(self, title, day):
        from scraper.sources import Article
        return Article("人民日报", "05版·评论", title,
                       f"http://example.com/{abs(hash(title))}", day,
                       "正文" * 200, "记者")

    def test_save_and_query(self):
        from scraper.classify import classify
        today = date.today().isoformat()
        a = self._art("测试文章一", today)
        self.assertTrue(self.store.save(self.con, a, classify(a)))

        rows = K._rows(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "测试文章一")
        self.assertIsInstance(rows[0]["topics"], list)
        self.assertIsInstance(rows[0]["tracks"], list)

    def test_duplicate_rejected(self):
        from scraper.classify import classify
        today = date.today().isoformat()
        a = self._art("重复文章", today)
        self.assertTrue(self.store.save(self.con, a, classify(a)))
        b = self._art("重复文章", today)
        b.url = a.url
        self.assertFalse(self.store.save(self.con, b, classify(b)))

    def test_prune_keeps_window(self):
        from scraper.classify import classify
        today = date.today()
        for i in range(6):
            d = (today - timedelta(days=i)).isoformat()
            a = self._art(f"文章{i}", d)
            self.store.save(self.con, a, classify(a))
        self.assertEqual(len(K._rows(30)), 6)

        gone = self.store.prune(self.con, 2)
        self.assertGreater(gone, 0)
        left = K._rows(30)
        self.assertTrue(all(r["date"] >= (today - timedelta(days=2)).isoformat()
                            for r in left))

    def test_covered_roundtrip(self):
        K.mark_covered("2026-10-29", 12)
        c = K.load_covered()
        self.assertIn("2026-10-29", c)
        self.assertEqual(c["2026-10-29"]["n"], 12)
        K.clear_covered()
        self.assertEqual(K.load_covered(), {})


if __name__ == "__main__":
    unittest.main()
