"""Every key the interface uses has text in all three languages, with the same placeholders."""
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
