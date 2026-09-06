from pathlib import Path


def patch_file(path: str, replacements: list[tuple[str, str]]) -> None:
    p = Path(path)
    raw = p.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    for old, new in replacements:
        count = text.count(old)
        if count != 1:
            raise SystemExit(f"{path}: expected exactly one patch anchor, found {count}")
        text = text.replace(old, new, 1)
    if newline == "\r\n":
        text = text.replace("\n", "\r\n")
    p.write_bytes(text.encode("utf-8"))


helper_anchor = '''    return last_per_show, last_watched_at


def _has_aired(release_date: str | None, today: date) -> bool:
'''
helper_replacement = '''    return last_per_show, last_watched_at


_NEXT_UP_RECENT_REWATCH_DAYS = 14
_NEXT_UP_BULK_BURST_SECONDS = 10
_NEXT_UP_BULK_BURST_MIN_EPISODES = 4


def _recent_rewatch_anchors(
    rows: list[tuple[int, int, int, datetime, datetime, int]],
    furthest_per_show: dict[int, tuple[int, int]],
    *,
    cutoff: datetime,
) -> dict[int, tuple[int, int, datetime]]:
    """Return short-lived anchors when a newest recent watch moved backward.

    Rows are ordered newest watched_at first. The newest event is authoritative:
    if it looks like a bulk/import burst, the show is rejected rather than
    falling through to an older event and being resurrected.
    """
    by_show: dict[int, list[tuple[int, int, datetime, datetime, int]]] = {}
    for show_id, season, episode, watched_at, created_at, media_id in rows:
        if season is None or episode is None or watched_at is None:
            continue
        by_show.setdefault(show_id, []).append(
            (season, episode, watched_at, created_at, media_id)
        )

    anchors: dict[int, tuple[int, int, datetime]] = {}
    for show_id, events in by_show.items():
        season, episode, watched_at, created_at, _media_id = events[0]
        if watched_at < cutoff:
            continue

        furthest = furthest_per_show.get(show_id)
        if furthest is None or (season, episode) >= furthest:
            continue

        if created_at is not None:
            burst_positions = {
                (s, e)
                for s, e, _watched, inserted_at, _mid in events
                if inserted_at is not None
                and abs((inserted_at - created_at).total_seconds()) <= _NEXT_UP_BULK_BURST_SECONDS
            }
            if len(burst_positions) >= _NEXT_UP_BULK_BURST_MIN_EPISODES:
                continue

        anchors[show_id] = (season, episode, watched_at)

    return anchors


def _mapped_tvdb_successor(
    series_tmdb_id: int,
    anchor_position: tuple[int, int],
    mappings: dict[tuple[int, int, int], EpisodeOrderMapping],
) -> tuple[int, int] | None:
    """Return the TMDB-side position of the immediate cached TVDB successor.

    Never jump over a mapping gap. Same-season successors must be exactly +1;
    a season transition must land on episode 1 of the next mapped official
    season. If the cache is incomplete, recovery waits instead of guessing.
    """
    season, episode = anchor_position
    anchor = mappings.get((series_tmdb_id, season, episode))
    if not anchor or anchor.tvdb_season_number <= 0:
        return None

    candidates = sorted(
        (
            m for (sid, _sn, _en), m in mappings.items()
            if sid == series_tmdb_id
            and m.tvdb_season_number > 0
            and (m.tvdb_season_number, m.tvdb_episode_number)
                > (anchor.tvdb_season_number, anchor.tvdb_episode_number)
        ),
        key=lambda m: (m.tvdb_season_number, m.tvdb_episode_number),
    )
    if not candidates:
        return None

    nxt = candidates[0]
    if nxt.tvdb_season_number == anchor.tvdb_season_number:
        if nxt.tvdb_episode_number != anchor.tvdb_episode_number + 1:
            return None
    elif nxt.tvdb_episode_number != 1:
        return None

    return nxt.tmdb_season_number, nxt.tmdb_episode_number


def _has_aired(release_date: str | None, today: date) -> bool:
'''

recent_query_anchor = '''    # Keep only the furthest episode per show, and the most recent watched_at per show.
    last_per_show, last_watched_at = _group_last_watched(rows)

    # Step 1b: Rewatching shows - candidate position comes from that rewatch's
'''
recent_query_replacement = '''    # Keep only the furthest episode per show, and the most recent watched_at per show.
    last_per_show, last_watched_at = _group_last_watched(rows)

    # Short-lived automatic rewatch signal. Only a recent backward move from
    # the historical furthest position qualifies, so normal first-time/current
    # progression stays entirely on the existing Next Up algorithm.
    recent_cutoff = datetime.utcnow() - timedelta(days=_NEXT_UP_RECENT_REWATCH_DAYS)
    recent_filters = [
        WatchEvent.user_id == current_user.id,
        WatchEvent.watched_at.isnot(None),
        WatchEvent.watched_at >= recent_cutoff,
        Media.media_type == MediaType.episode,
        Media.show_id.isnot(None),
        Media.season_number.isnot(None),
        Media.episode_number.isnot(None),
        or_(WatchEvent.completed == True, WatchEvent.progress_percent >= 0.5),
    ]
    if active_rewatch_show_ids:
        recent_filters.append(Media.show_id.notin_(active_rewatch_show_ids))
    recent_result = await db.execute(
        select(
            Media.show_id,
            Media.season_number,
            Media.episode_number,
            WatchEvent.watched_at,
            WatchEvent.created_at,
            Media.id,
        )
        .join(WatchEvent, WatchEvent.media_id == Media.id)
        .where(*recent_filters)
        .order_by(desc(WatchEvent.watched_at), desc(WatchEvent.created_at), desc(WatchEvent.id))
    )
    recent_rewatch_anchors = _recent_rewatch_anchors(
        recent_result.all(), last_per_show, cutoff=recent_cutoff
    )

    # Step 1b: Rewatching shows - candidate position comes from that rewatch's
'''

recovery_anchor = '''            await db.commit()

    if not next_per_show:
        return {"next_up": []}

    # Remove episodes the user has already (re)watched. For a rewatching show
'''
recovery_replacement = '''            await db.commit()

    # Supplemental recent-rewatch recovery. This is cache/local-row only: no
    # provider call is made just because an old episode was revisited.
    recent_recovery_show_ids: set[int] = set()
    if recent_rewatch_anchors:
        recovery_show_ids = list(recent_rewatch_anchors)
        recovery_shows_result = await db.execute(
            select(Show).where(Show.id.in_(recovery_show_ids))
        )
        recovery_shows = {s.id: s for s in recovery_shows_result.scalars().all()}

        series_tmdb_ids = [
            s.tmdb_id for s in recovery_shows.values() if s.tmdb_id is not None
        ]
        recovery_orders = await get_episode_orders_for_series(
            db, current_user.id, series_tmdb_ids
        ) if series_tmdb_ids else {}
        tvdb_series_ids = [
            sid for sid, pref in recovery_orders.items()
            if pref.episode_order == "tvdb"
        ]
        recovery_mappings = await get_tmdb_to_tvdb_positions(
            db, tvdb_series_ids
        ) if tvdb_series_ids else {}

        target_positions: dict[int, tuple[int, int]] = {}
        for show_id, (season, episode, _watched_at) in recent_rewatch_anchors.items():
            show = recovery_shows.get(show_id)
            if not show or show.tmdb_id is None:
                continue

            pref = recovery_orders.get(show.tmdb_id)
            if pref and pref.episode_order == "tvdb":
                target = _mapped_tvdb_successor(
                    show.tmdb_id, (season, episode), recovery_mappings
                )
            else:
                target = _compute_next_episode(
                    (show.tmdb_data or {}).get("seasons", []), season, episode
                )
            if target is not None:
                target_positions[show_id] = target

        if target_positions:
            target_filters = [
                and_(
                    Media.show_id == show_id,
                    Media.season_number == season,
                    Media.episode_number == episode,
                )
                for show_id, (season, episode) in target_positions.items()
            ]
            recovered_result = await db.execute(
                select(Media)
                .options(selectinload(Media.show))
                .where(
                    Media.media_type == MediaType.episode,
                    Media.tmdb_id.isnot(None),
                    or_(*target_filters),
                )
                .order_by(Media.show_id, Media.id)
            )
            for media in recovered_result.scalars().all():
                expected = target_positions.get(media.show_id)
                if expected != (media.season_number, media.episode_number):
                    continue
                next_per_show[media.show_id] = media
                recent_recovery_show_ids.add(media.show_id)

    if not next_per_show:
        return {"next_up": []}

    # Remove episodes the user has already (re)watched. For a rewatching show
'''

completed_anchor = '''    non_rewatch_ids = [m.id for m in next_per_show.values() if m.show_id not in active_rewatch_show_ids]
    rewatch_ids = [m.id for m in next_per_show.values() if m.show_id in active_rewatch_show_ids]
'''
completed_replacement = '''    recent_recovery_media_ids = {
        m.id for show_id, m in next_per_show.items()
        if show_id in recent_recovery_show_ids
    }
    non_rewatch_ids = [
        m.id for m in next_per_show.values()
        if m.show_id not in active_rewatch_show_ids
        and m.id not in recent_recovery_media_ids
    ]
    rewatch_ids = [m.id for m in next_per_show.values() if m.show_id in active_rewatch_show_ids]
'''

patch_file("backend/routers/history.py", [
    (helper_anchor, helper_replacement),
    (recent_query_anchor, recent_query_replacement),
    (recovery_anchor, recovery_replacement),
    (completed_anchor, completed_replacement),
])

test_import_anchor = 'from datetime import datetime\n'
test_import_replacement = 'from datetime import datetime, timedelta\n'
test_anchor = '''

if __name__ == "__main__":
    unittest.main()
'''
test_replacement = '''

class RecentNextUpRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 6, 12, 0, 0)
        self.cutoff = self.now - timedelta(days=14)

    def _row(self, episode: int, *, watched_at=None, created_at=None, season=3, show_id=55, media_id=None):
        watched_at = watched_at or self.now
        created_at = created_at or watched_at
        return (show_id, season, episode, watched_at, created_at, media_id or episode)

    def test_recent_backward_watch_becomes_rewatch_anchor(self) -> None:
        anchors = history._recent_rewatch_anchors(
            [self._row(5)], {55: (3, 7)}, cutoff=self.cutoff
        )
        self.assertEqual(anchors[55], (3, 5, self.now))

    def test_current_furthest_watch_leaves_normal_next_up_untouched(self) -> None:
        anchors = history._recent_rewatch_anchors(
            [self._row(7)], {55: (3, 7)}, cutoff=self.cutoff
        )
        self.assertEqual(anchors, {})

    def test_stale_backward_watch_does_not_resurrect_show(self) -> None:
        old = self.now - timedelta(days=15)
        anchors = history._recent_rewatch_anchors(
            [self._row(5, watched_at=old, created_at=old)],
            {55: (3, 7)}, cutoff=self.cutoff,
        )
        self.assertEqual(anchors, {})

    def test_bulk_timestamp_burst_is_not_a_rewatch_anchor(self) -> None:
        rows = [self._row(ep, media_id=100 + ep) for ep in (5, 4, 3, 2)]
        anchors = history._recent_rewatch_anchors(
            rows, {55: (3, 7)}, cutoff=self.cutoff
        )
        self.assertEqual(anchors, {})

    def test_cached_tvdb_mapping_returns_exact_immediate_successor(self) -> None:
        anchor = EpisodeOrderMapping(
            series_tmdb_id=100, tmdb_season_number=3, tmdb_episode_number=7,
            tmdb_episode_id=307, tvdb_id=1007, tvdb_season_number=3,
            tvdb_episode_number=7, match_method="external_id",
        )
        successor = EpisodeOrderMapping(
            series_tmdb_id=100, tmdb_season_number=3, tmdb_episode_number=8,
            tmdb_episode_id=308, tvdb_id=1008, tvdb_season_number=3,
            tvdb_episode_number=8, match_method="external_id",
        )
        mappings = {(100, 3, 7): anchor, (100, 3, 8): successor}
        self.assertEqual(history._mapped_tvdb_successor(100, (3, 7), mappings), (3, 8))

    def test_incomplete_tvdb_mapping_never_skips_forward(self) -> None:
        anchor = EpisodeOrderMapping(
            series_tmdb_id=100, tmdb_season_number=3, tmdb_episode_number=7,
            tmdb_episode_id=307, tvdb_id=1007, tvdb_season_number=3,
            tvdb_episode_number=7, match_method="external_id",
        )
        gap = EpisodeOrderMapping(
            series_tmdb_id=100, tmdb_season_number=3, tmdb_episode_number=9,
            tmdb_episode_id=309, tvdb_id=1009, tvdb_season_number=3,
            tvdb_episode_number=9, match_method="external_id",
        )
        mappings = {(100, 3, 7): anchor, (100, 3, 9): gap}
        self.assertIsNone(history._mapped_tvdb_successor(100, (3, 7), mappings))


if __name__ == "__main__":
    unittest.main()
'''

patch_file("backend/tests/test_history.py", [
    (test_import_anchor, test_import_replacement),
    (test_anchor, test_replacement),
])
