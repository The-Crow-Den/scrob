from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one {label} match, found {count}")
    return text.replace(old, new, 1)


history_path = Path("backend/routers/history.py")
data = history_path.read_bytes()
text = data.decode("utf-8")
nl = "\r\n" if "\r\n" in text else "\n"

old_helper = nl.join([
    'def _has_confirmed_air_date(release_date: str | None, today: date) -> bool:',
    '    """True only if release_date is set AND on or before today.',
    '',
    '    Used for Next Up specifically (issue #111): the suggested "next" episode',
    '    is often a placeholder TMDB has pre-created for a renewed show before an',
    "    air date is announced. Unlike _has_aired's callers, there's no other",
    '    confirmation this episode actually exists yet, so an unknown date must',
    '    NOT be treated as "aired" here - that would suggest watching something',
    '    that may not even be out."""',
    '    return bool(release_date) and release_date <= today.isoformat()',
])
new_helper = nl.join([
    'def _has_confirmed_release_date(release_date: str | None) -> bool:',
    '    """True when the immediate next episode has a real release date.',
    '',
    '    Next Up intentionally keeps confirmed future episodes visible so a',
    '    caught-up show does not disappear between releases. Missing dates still',
    '    fail closed because provider-created placeholders may not be real yet.',
    '    """',
    '    return bool(release_date)',
])
text = replace_once(text, old_helper, new_helper, "air-date helper")
text = replace_once(
    text,
    'See _has_confirmed_air_date for' + nl + '    the opposite case.',
    'See _has_confirmed_release_date for' + nl + '    the opposite case.',
    "helper doc reference",
)

old_filter = nl.join([
    '    today = date.today()',
    '    next_up = [',
    '        m for m in next_per_show.values()',
    '        if m.id not in completed_ids',
    '        and (include_hidden or (m.show_id not in hidden_set and m.show_id not in dropped_show_ids))',
    "        # Don't surface an episode that hasn't aired yet — the immediately-next",
    '        # episode for the show, not a later one, so we simply show nothing for',
    '        # this show until it airs rather than skipping ahead. An episode with',
    "        # no air date at all (a renewal placeholder TMDB hasn't dated yet) is",
    '        # treated the same as "not aired" here, not "assume it\'s fine" (#111).',
    '        and _has_confirmed_air_date(m.release_date, today)',
    '    ]',
])
new_filter = nl.join([
    '    next_up = [',
    '        m for m in next_per_show.values()',
    '        if m.id not in completed_ids',
    '        and (include_hidden or (m.show_id not in hidden_set and m.show_id not in dropped_show_ids))',
    '        # Keep a confirmed immediate successor visible even when its air date',
    '        # is still in the future, so caught-up shows do not disappear from',
    '        # Next Up between releases. Episodes with no confirmed date remain',
    '        # hidden because they may only be provider placeholders (#111).',
    '        and _has_confirmed_release_date(m.release_date)',
    '    ]',
])
text = replace_once(text, old_filter, new_filter, "Next Up release-date filter")
history_path.write_bytes(text.encode("utf-8"))


test_path = Path("backend/tests/test_history.py")
test_data = test_path.read_bytes()
test_text = test_data.decode("utf-8")
test_nl = "\r\n" if "\r\n" in test_text else "\n"
marker = "class NextUpReleaseDateEligibilityTests(unittest.TestCase):"
if marker in test_text:
    raise RuntimeError("Next Up release-date tests already present")

tests = test_nl.join([
    '',
    '',
    marker,
    '    def test_already_aired_episode_remains_eligible(self) -> None:',
    '        self.assertTrue(history._has_confirmed_release_date("2026-09-01"))',
    '',
    '    def test_confirmed_future_episode_remains_eligible(self) -> None:',
    '        self.assertTrue(history._has_confirmed_release_date("2026-09-12"))',
    '',
    '    def test_missing_release_date_remains_hidden(self) -> None:',
    '        self.assertFalse(history._has_confirmed_release_date(None))',
    '        self.assertFalse(history._has_confirmed_release_date(""))',
    '',
])
test_path.write_bytes((test_text.rstrip("\r\n") + tests).encode("utf-8"))
