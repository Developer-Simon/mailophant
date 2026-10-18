import re
import unittest

from mailviewer import i18n


def codes_except_en():
    return [l["code"] for l in i18n.languages() if l["code"] != "en"]


def placeholders(value):
    forms = value.values() if isinstance(value, dict) else [value]
    return {m.group(1) for f in forms for m in i18n.PLACEHOLDER.finditer(f)}


class CatalogTest(unittest.TestCase):
    def test_registry_lists_en_first_and_files_exist(self):
        codes = [l["code"] for l in i18n.languages()]
        self.assertEqual(codes[0], "en")
        for c in codes:
            self.assertTrue((i18n.I18N_DIR / f"{c}.json").is_file(), c)

    def test_all_languages_have_the_english_keys(self):
        en = i18n.messages("en")
        for c in codes_except_en():
            self.assertEqual(set(i18n.messages(c)), set(en), c)

    def test_plural_shape_and_placeholders_match_english(self):
        en = i18n.messages("en")
        for c in codes_except_en():
            other = i18n.messages(c)
            for key, value in en.items():
                self.assertEqual(isinstance(value, dict), isinstance(other[key], dict), (c, key))
                if isinstance(other[key], dict):
                    self.assertIn("other", other[key], (c, key))
                self.assertEqual(placeholders(value), placeholders(other[key]), (c, key))


class RenderTest(unittest.TestCase):
    def test_placeholders(self):
        self.assertEqual(i18n.render("{a} and {b}", {"a": 1, "b": "x"}), "1 and x")

    def test_values_are_not_expanded_again(self):
        self.assertEqual(i18n.render("Folder {name}", {"name": "{name}"}), "Folder {name}")

    def test_unknown_placeholder_stays(self):
        self.assertEqual(i18n.render("{missing}", {}), "{missing}")

    def test_plural(self):
        v = {"one": "{count} mail", "other": "{count} mails"}
        self.assertEqual([i18n.render(v, {"count": n}) for n in (0, 1, 2)], ["0 mails", "1 mail", "2 mails"])
        self.assertEqual(i18n.render(v, {"count": 1234}), "1,234 mails")

    def test_size(self):
        self.assertEqual(i18n.render("{n:size}", {"n": 1536}), "1.5 KB")
        self.assertEqual(i18n.format_size(10), "10 B")

    def test_nested_msg(self):
        self.assertEqual(i18n.render("x: {detail}", {"detail": i18n.raw("boom")}), "x: boom")


class MsgTest(unittest.TestCase):
    def test_user_error(self):
        exc = i18n.UserError("err.folder_not_found")
        self.assertIsInstance(exc, ValueError)
        self.assertEqual(i18n.error_body(i18n.detail(exc)),
                         {"error": str(exc), "code": "err.folder_not_found", "params": {}})

    def test_foreign_error_has_no_code(self):
        self.assertEqual(i18n.error_body(i18n.detail(OSError("disk full"))), {"error": "disk full"})

    def test_to_json_nests(self):
        m = i18n.msg("log.source_failed", source="A", detail=i18n.raw("boom"))
        self.assertEqual(m.to_json()["params"]["detail"], {"text": "boom"})
        self.assertEqual(m.text, "A: error: boom")


class ResolveTest(unittest.TestCase):
    def test_cases(self):
        cases = [(None, "de-AT,en;q=0.5", "de"), (None, "de-CH", "de"), (None, "fr-FR,de;q=0.9", "en"),
                 (None, "en;q=0.2,de;q=0.8", "de"), (None, None, "en"), (None, "*", "en"),
                 (None, "de;q=abc,en", "en"), (None, "en;Q=0.2,de;Q=0.8", "de"), (None, "garbage;;", "en"),
                 ("en", "de", "en"), ("de", "en", "de"), ("fr", "de", "de"), (42, "en", "en")]
        for setting, header, want in cases:
            self.assertEqual(i18n.resolve_language(setting, header), want, (setting, header))

    def test_boot_unknown_setting_is_system(self):
        self.assertEqual(i18n.boot("fr", "en")["setting"], "system")
        self.assertEqual(i18n.boot(42, None)["setting"], "system")


if __name__ == "__main__":
    unittest.main()


import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def string_literals(paths):
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield path, node


class NoGermanInServerTest(unittest.TestCase):
    """String literals in mailviewer/*.py (docstrings included) must not look German."""
    GERMAN = re.compile(r"[äöüÄÖÜß]|\b(nicht|kein|keine|keinen|der|die|das|und|oder|ist|sind|wird|werden|"
                        r"Fehler|Ordner|Quelle|Mails|Datei|bitte|Bitte|übersprungen|gefunden|fehlt)\b")
    ALLOWED = {
        # Outlook folder names are data from the mailbox, matched in several languages
        "posteingang", "gelöschte elemente", "entwürfe",
        "Nachricht.rtf",  # file name written into imported mails
        "[^a-z0-9äöüß]",  # import_slug keeps German letters in directory names
    }
    ALLOWED_PREFIXES = ("Directory name for an imported source",)  # docstring of import_slug

    def test_no_german_literals(self):
        files = sorted((ROOT / "mailviewer").glob("*.py"))
        hits = [f"{p.name}:{n.lineno}: {n.value[:70]!r}" for p, n in string_literals(files)
                if self.GERMAN.search(n.value) and n.value not in self.ALLOWED and not n.value.startswith(self.ALLOWED_PREFIXES) and "i18n" not in p.name]
        self.assertEqual(hits, [])


class UsedKeysTest(unittest.TestCase):
    def test_python_codes_exist_in_english(self):
        used = set()
        for p in (ROOT / "mailviewer").glob("*.py"):
            used |= set(re.findall(r'(?:msg|UserError|Error)\(\s*"((?:err|log|msg)\.[\w.]+)"', p.read_text(encoding="utf-8")))
        self.assertEqual(sorted(used - set(i18n.messages("en"))), [])


class ServerMessageTest(unittest.TestCase):
    def test_archive_error_has_code_and_params(self):
        from mailviewer.archive import ArchiveError
        e = ArchiveError("err.invalid_folder_name", max=100)
        self.assertEqual(i18n.error_body(i18n.detail(e))["params"], {"max": 100})
        self.assertIn("100", e.msg.text)

    def test_library_error_is_a_value_error_with_code(self):
        from mailviewer.library import LibraryError
        e = LibraryError("err.not_a_folder", root="{count}")
        self.assertIsInstance(e, ValueError)
        self.assertEqual(str(e), "{count} is not a folder.")


UI_FILES = [ROOT / "static" / "index.html", *sorted((ROOT / "static" / "js").glob("*.js"))]
ELECTRON_FILES = sorted((ROOT / "electron" / "src").glob("*.js"))


def strip_comments(path: Path) -> list[tuple[int, str]]:
    """(line number, text) with comments and console.* lines removed."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".html":
        text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
        text = re.sub(r"<style.*?</style>", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
        text = re.sub(r"(^|\n)\s*//[^\n]*", r"\1", text)
    else:
        text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        if path.suffix == ".js":
            line = re.sub(r"(^|\s)//.*$", "", line)
            if "console." in line:
                continue
        out.append((n, line))
    return out


class NoGermanInUiTest(unittest.TestCase):
    """index.html, static/js/*.js (and later electron/src/*.js): no German user text outside the language files."""
    ALLOWED = (
        "posteingang", "gesendet", "entw", "papierkorb", "gelöscht", "archiv",  # folderIconName: German folder names are data
    )

    def test_no_german_text(self):
        pattern = NoGermanInServerTest.GERMAN
        hits = []
        for path in [*UI_FILES, *ELECTRON_FILES]:
            if path.name == "pure.js":
                continue  # folder name patterns and case folding only
            for n, line in strip_comments(path):
                if pattern.search(line) and not any(a in line for a in self.ALLOWED):
                    hits.append(f"{path.name}:{n}: {line.strip()[:110]}")
        self.assertEqual(hits, [])


class UsedKeysUiTest(unittest.TestCase):
    def test_ui_keys_exist_in_english(self):
        used = set()
        for path in [*UI_FILES, *ELECTRON_FILES]:
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".js":
                used |= set(re.findall(r"\bt\(\s*'([\w.]+)'", text))
            else:
                used |= set(re.findall(r'data-i18n(?:-[a-z-]+)?="([\w.]+)"', text))
        self.assertEqual(sorted(used - set(i18n.messages("en"))), [])


class HtmlMarkupTest(unittest.TestCase):
    def test_translated_elements_have_no_child_elements(self):
        # I18N.apply() sets textContent, which would delete icons or filled-in spans inside
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for m in re.finditer(r"<(\w+)[^>]*\sdata-i18n=\"[^\"]+\"[^>]*>(.*?)</\1>", html, re.S):
            self.assertNotIn("<", m.group(2), m.group(0)[:100])
