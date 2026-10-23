"""Every key the interface uses has text in every language, with the same placeholders."""
import re

import i18n_keys as keys

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_rows_are_complete_and_unique():
    rows = keys.defined_rows()
    assert rows, "i18n.js has no rows"
    bad = [r for r in rows if len(r) != 4 or not all(cell.strip() for cell in r)]
    assert not bad, f"rows need a key and three non-empty texts: {bad[:5]}"
    names = [r[0] for r in rows]
    dupes = sorted({k for k in names if names.count(k) > 1})
    assert not dupes, f"duplicate keys: {dupes}"


def test_every_used_key_is_translated():
    defined = {r[0] for r in keys.defined_rows()}
    missing = sorted(keys.needed_keys() - defined)
    assert not missing, f"keys used in the UI without text: {missing}"


def test_no_dead_keys():
    defined = {r[0] for r in keys.defined_rows()}
    unused = sorted(defined - keys.needed_keys())
    assert not unused, f"keys nobody uses: {unused}"


def test_placeholders_match_across_languages():
    for key, sr, en, no in keys.defined_rows():
        want = set(PLACEHOLDER.findall(en))
        for lang, text in (("sr", sr), ("no", no)):
            assert set(PLACEHOLDER.findall(text)) == want, f"{key} [{lang}] placeholders differ from English"


def test_every_other_language_has_every_key_and_the_same_placeholders():
    from core import settings

    rows = {r[0]: r[2] for r in keys.defined_rows()}
    langs = keys.extra_langs()
    assert len(langs) == 19
    for code, (name, texts) in langs.items():
        assert set(texts) == set(rows), f"{code}: missing {sorted(set(rows) - set(texts))[:5]}, extra {sorted(set(texts) - set(rows))[:5]}"
        assert all(str(v).strip() for v in texts.values()), f"{code} has empty texts"
        for key, en in rows.items():
            assert set(PLACEHOLDER.findall(texts[key])) == set(PLACEHOLDER.findall(en)), f"{key} [{code}] placeholders differ from English"
        assert "Marko Veselinović" in texts["aboutAuthor"], f"{code}: the author is named in About"
    # index.html loads every file, and Windows detection only hands out languages the program has
    html = (keys.WEB / "index.html").read_text(encoding="utf-8")
    assert all(f'src="js/lang/{code}.js"' in html for code in langs)
    assert set(settings.PRIMARY.values()) | {"pt-BR", "zh-CN"} == set(langs) | {"sr", "no"}
