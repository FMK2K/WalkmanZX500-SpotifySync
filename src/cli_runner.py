"""
cli_runner.py — Headless SpiralOrchestrator entry point (no GUI required).

Usage:
    python cli_runner.py audit
    python cli_runner.py download
    python cli_runner.py reorganize
    python cli_runner.py full

All log output goes to stdout (and sync_log.txt via the root logger).
Exit code: 0 on success, 1 if canary failed or unhandled exception.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time

# ---------------------------------------------------------------------------
# Logging: timestamps + level to stdout so agents can parse live output
# ---------------------------------------------------------------------------
# Reconfigure stdout to UTF-8 so Japanese/CJK filenames don't crash the console
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger(__name__)


def _log_cb(msg: str, level: str = "INFO") -> None:
    # Use print() directly — routing through logging.getLogger() causes infinite
    # recursion when walkman_reorganize installs a logging handler whose emit()
    # calls this same callback.
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        print(f"{ts} [{level}] {msg}", flush=True)
    except UnicodeEncodeError:
        print(f"{ts} [{level}] {msg.encode('ascii', errors='replace').decode()}", flush=True)


def _attempt_adb_reconnect_if_needed() -> None:
    """
    2026-07-26: mtp_transfer.ADB_AVAILABLE is computed exactly once, at
    import time — if ADB was disconnected at that instant, every operation
    for the rest of the process uses COM/WPD exclusively, even if ADB
    reconnects moments later. Confirmed live (scan-duplicates, then
    audit/reorganize/full/download by the same architecture — all route
    device I/O through this one cached flag in mtp_transfer.py) that this
    can turn a normally-fast run into 20+ minutes of WPD live-lock guards
    plus per-storage Shell.Application timeouts before falling back to a
    scan that was never going to use ADB in the first place.

    One reconnect attempt, not a retry loop; entirely non-fatal — a genuine
    device outage (as opposed to a stale in-process flag) still degrades to
    the existing COM/WPD path exactly as before this fix.

    2026-07-30: added a second-stage WiFi ADB fallback. If the same-session
    USB reconnect() above fails, and the user has previously confirmed a
    WiFi ADB target (config.ADB_WIFI_ENABLED + ADB_WIFI_HOST, set by
    main.py's "Connect over WiFi" button), try that before giving up — a
    USB session going offline doesn't mean the device itself is
    unreachable, just that link. No-op if WiFi ADB was never configured.
    """
    try:
        import mtp_transfer as _mtp_reconnect
        if not _mtp_reconnect.ADB_AVAILABLE:
            import adb_transfer as _adb_reconnect
            if _adb_reconnect.reconnect() or _adb_reconnect.reconnect_wifi_if_configured():
                _mtp_reconnect.refresh_adb_availability()
    except Exception as _reconnect_err:
        log.debug(f"[CLI] ADB reconnect attempt failed (non-fatal): {_reconnect_err}")


def main() -> int:
    # 2026-07-20: main.py (GUI) has started dialog_watcher at launch since
    # Rev 14.2 ("the watchdog module has existed since Rev 13 with a complete
    # start()/stop() API, but NOTHING in the codebase ever called start()")
    # — cli_runner.py never got the same fix, so every CLI-driven run
    # (including any real unattended overnight run via this entry point, not
    # just ad-hoc testing) has had zero protection against a Windows "Confirm
    # File Delete"/"Confirm Permanent Deletion" dialog blocking indefinitely.
    # Confirmed live: a corrupt-remote cleanup fell through to
    # mtp_transfer_com._delete_remote_after_failed_verify's InvokeVerb
    # fallback (its own docstring says "dialog_watcher will auto-dismiss the
    # resulting dialog") with no watcher running to do that — the user had
    # to manually click the confirmation themselves. Same fix as main.py:
    # a daemon thread, safe to start unconditionally, exits with the process.
    try:
        import dialog_watcher as _dw
        _dw.start()
        _dw.set_cancel_flag([False])
    except Exception as _dwe:
        log.warning(f"  [cli_runner] dialog_watcher start failed (non-fatal): {_dwe}")

    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    args = [a for a in args if not a.startswith("--")]

    MODES = ("full", "download", "audit", "reorganize", "curate", "purge-deleted", "deep-clean",
              "purge-genre-cache", "merge-splintered-folders", "preview-genre-consolidation",
              "preview-acoustid-corrections", "preview-listenbrainz-discovery", "scan-duplicates",
              "refresh-tag-snapshot", "rebuild-genre-graph", "visualize-genre-graph",
              "completeness-report", "artist-completeness-report", "build-favorites-playlists-plan",
              "album-normalize", "duration-verify", "run-tool")

    # `run-tool <script.py> [args...]` — generic delegator so the GUI tools
    # panel can launch any standalone curation script (each with its own
    # dry-run / --execute / --exhaustive / etc. flags) and stream it into the
    # same viewer window as scan-duplicates. Whitelisted for safety.
    _RUN_TOOL_OK = {
        "walkman_album_normalize.py", "duration_verify.py", "duration_verify_categorize.py",
        "walkman_desplinter.py", "walkman_deluxe_merge.py", "walkman_storage_rebalance.py",
        "walkman_cover_fill.py", "walkman_year_backfill.py", "catalog_fill_scoped.py",
        "mislabel_redownload.py", "walkman_fav_upgrade.py", "walkman_incompleteness_scan.py",
        "artist_completeness_filter.py", "walkman_favplaylist_apply.py",
    }
    if not args or args[0] not in MODES:
        print(
            "Usage: python cli_runner.py "
            "<full|download|audit|reorganize|curate|purge-deleted|deep-clean|"
            "purge-genre-cache|merge-splintered-folders|preview-genre-consolidation|"
            "preview-acoustid-corrections|preview-listenbrainz-discovery|scan-duplicates|"
            "refresh-tag-snapshot|rebuild-genre-graph|visualize-genre-graph|"
            "completeness-report|artist-completeness-report|build-favorites-playlists-plan|"
            "album-normalize|duration-verify> "
            "[--dry-run] [--execute] [--exhaustive] [--fix-tags] [--fresh] [--artist NAME] "
            "[--device-only] [--folder PATH] [--username NAME] [--max-nodes=N] [--top=N] [--min-score=N]",
            file=sys.stderr,
        )
        return 1

    mode = args[0]

    # ── refresh-tag-snapshot: replace walkman_tag_snapshot.json wholesale ────
    # 2026-07-28: cohort_classifier.py was silently classifying every device
    # file "high" risk because it read the wrong snapshot field, AND because
    # walkman_tag_snapshot.json itself was stale (scanned before the library
    # moved from internal storage to the SD card, so it had zero real overlap
    # with the current walkman_snapshot.json). The field-name bug is fixed;
    # this command fixes the staleness by re-running walkman_tag_scan.py's
    # full device scan and overwriting the tag snapshot file outright — every
    # consumer (cohort_classifier, audit._enforce_album_genre_consensus,
    # spiral_orchestrator's cross-check) already reads it fresh from disk on
    # every call, none of them cache it in memory, so overwriting the file is
    # all that's needed for the next audit/reorganize/full run to see it.
    # Read-only on the device (adb_pull_head only ever reads file heads).
    # ── album-normalize / duration-verify: thin delegators so the GUI tool
    #    panel can launch them the same way it launches scan-duplicates etc.
    #    Pass-through flags: album-normalize takes --execute / --exhaustive /
    #    --fix-tags / --artist; duration-verify takes sweep|status / --fresh.
    if mode in ("album-normalize", "duration-verify", "run-tool"):
        import subprocess as _sp
        # `args` above has had all --flags stripped (line ~107) — pull the real
        # pass-through set straight from sys.argv, everything after the mode.
        _raw = sys.argv[1:]
        _passthru = _raw[_raw.index(mode) + 1:] if mode in _raw else []
        if mode == "album-normalize":
            _script, _pt = "walkman_album_normalize.py", _passthru
        elif mode == "duration-verify":
            _script, _pt = "duration_verify.py", _passthru
            if not any(a in ("sweep", "status") for a in _pt):
                _pt = ["sweep"] + _pt
        else:  # run-tool <script.py> [args...]
            if not _passthru or _passthru[0] not in _RUN_TOOL_OK:
                print(f"run-tool: script must be one of {sorted(_RUN_TOOL_OK)}", file=sys.stderr)
                return 1
            _script, _pt = _passthru[0], _passthru[1:]
        _cmd = [sys.executable, "-u", _script, *_pt]
        log.info(f"[{mode}] delegating: {' '.join(_cmd)}")
        try:
            _attempt_adb_reconnect_if_needed()
        except Exception:
            pass
        _p = _sp.Popen(_cmd, cwd=str(getattr(__import__('config'), 'SRC_DIR', '.')),
                       stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8",
                            "PYTHONUNBUFFERED": "1", "MSYS_NO_PATHCONV": "1"})
        for _ln in _p.stdout:
            print(_ln.rstrip())
        _p.wait()
        return _p.returncode

    if mode == "refresh-tag-snapshot":
        try:
            _attempt_adb_reconnect_if_needed()
            import walkman_tag_scan as _wts
            _wts.main()
            return 0
        except SystemExit as e:
            log.error(f"[RefreshTagSnapshot] {e}")
            return 1
        except Exception as e:
            log.error(f"[RefreshTagSnapshot] Failed: {e}", exc_info=True)
            return 1

    # ── scan-duplicates: read-only device scan + dupe/corrupt-file report ────
    # 2026-07-23: standalone version of the dedup scan that already runs
    # embedded inside audit/reorganize/full (report mode) — pulled out so it
    # can be run on its own without the rest of the pipeline. Always
    # non-destructive: DedupEngine.execute(dry_run=True) logs everything it
    # WOULD do and writes .dedup_log.json, but never deletes/moves anything.
    # deep-clean --from-log consumes the resulting journal to do the actual
    # (now ADB-fast) removal — this command only identifies candidates.
    if mode == "scan-duplicates":
        try:
            from walkman_dedup import DedupEngine, device_metadata_score_lookup

            # 2026-07-26: one ADB reconnect attempt before the scan — see
            # _attempt_adb_reconnect_if_needed()'s docstring for why this
            # matters (confirmed live: an overnight run spent ~22 minutes on
            # the WPD/Shell fallback chain instead of retrying a possibly-
            # reconnectable ADB).
            _attempt_adb_reconnect_if_needed()

            # 2026-07-25: --no-ai skips the ambiguous-match AI disambiguation
            # step (config.DEDUP_AMBIGUOUS_USE_AI, on by default) for just
            # this invocation, without touching the persistent config value
            # that audit/reorganize/full's embedded dedup pass also uses.
            # Confirmed live: that step can silently grind for 10+ minutes
            # with zero progress output — it only logs when the AI REJECTS a
            # match, and call_ai() cascades through up to 7 providers
            # (Groq x5 variants, Gemini, Claude, DeepSeek, GPT-4o) per
            # ambiguous pair, each with its own delay/retry.
            no_ai = "--no-ai" in sys.argv
            engine = DedupEngine(use_ai=not no_ai)
            if no_ai:
                log.info("[ScanDuplicates] --no-ai: skipping AI-assisted ambiguous-match disambiguation")

            # 2026-08-27: without this, build_plan() defaults
            # metadata_score_lookup=None and Pass 3 (_pass_metadata_completeness
            # — same-format duplicates, e.g. two identical-extension copies of
            # the same track in different folders) silently never runs, even
            # though this same pass is on by default in the GUI audit pipeline
            # (spiral_orchestrator.py) and behind walkman_dedup.py's own
            # --with-metadata flag. Confirmed live: a scan-duplicates run found
            # 0 of 4 real duplicate .flac copies of the same track because all
            # 4 shared the same extension — Pass 2 only compares across
            # formats. --no-metadata opts back out (Pass 3 pulls each
            # same-format candidate file from the device to score its tags,
            # so it's slower than passes 1+2 alone).
            no_metadata = "--no-metadata" in sys.argv
            if no_metadata:
                log.info("[ScanDuplicates] --no-metadata: skipping same-format duplicate detection (Pass 3)")
            plan = engine.build_plan(
                include_metadata_check=not no_metadata,
                metadata_score_lookup=None if no_metadata else device_metadata_score_lookup,
            )
            stats = engine.execute(plan, delete_mode="hard", dry_run=True)

            # DedupEngine.execute() returns BEFORE writing the journal when
            # plan.candidates is empty ("Nothing to delete") — so a clean scan
            # would otherwise never update .dedup_log.json, and deep-clean's
            # most-recent-entry lookup would keep falling back to an older,
            # possibly-stale entry instead of reflecting this scan. Write it
            # ourselves in that case so "scan, then deep-clean" always means
            # THIS scan.
            if not plan.candidates:
                engine._write_journal(plan, delete_mode="hard", dry_run=True)

            by_reason: dict = {}
            for c in plan.candidates:
                by_reason[c.reason] = by_reason.get(c.reason, 0) + 1

            log.info(
                f"[ScanDuplicates] Scanned {plan.scan_total} device file(s) — "
                f"{stats['planned']} duplicate/corrupt candidate(s) found"
            )
            for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
                log.info(f"[ScanDuplicates]   {reason}: {n}")
            log.info(
                "[ScanDuplicates] Journal written to .dedup_log.json — "
                "run 'deep-clean --from-log' (add --execute to actually remove) to act on it."
            )
            return 0
        except Exception as e:
            log.error(f"[ScanDuplicates] Failed: {e}", exc_info=True)
            return 1

    # ── preview-genre-consolidation: MK38.0-adjacent, read-only ──────────────
    # Shows what _consolidate_album_genres (modal/graph genre consolidation)
    # would change, without writing anything and without touching the MK38.0
    # freeze at all. Use this to review before ever considering an unlock.
    if mode == "preview-genre-consolidation":
        try:
            import config as _cfg
            if getattr(_cfg, "OFFLINE_MODE", False):
                import spotify_local_data as _sld
                track_map, _liked, _albums = _sld.fetch_and_build_offline()
            else:
                import spotify_client as _sc
                _sp = _sc.get_spotify_client()
                track_map, _liked, _albums = _sc.fetch_and_build(_sp)
            log.info(f"[PreviewGenre] Spotify fetch OK — {len(track_map)} playlist(s)")

            from walkman_sync import WalkmanSyncAgent
            agent = WalkmanSyncAgent(log_callback=_log_cb)
            preview = agent.preview_album_genre_consolidation(track_map)

            if not preview:
                log.info("[PreviewGenre] No changes would be made.")
                return 0
            for row in preview:
                log.info(
                    f"[PreviewGenre] {row['artist']} / {row['album']} / {row['title']}: "
                    f"{row['genre_from'] or '(blank)'} -> {row['genre_to']} "
                    f"(tier={row['tier']})"
                )
            log.info(f"[PreviewGenre] {len(preview)} track(s) would change.")
            return 0
        except Exception as e:
            log.error(f"[PreviewGenre] Failed: {e}", exc_info=True)
            return 1

    # ── rebuild-genre-graph: build the artist co-occurrence graph from the
    #    current confident genre-cache entries and PERSIST it to
    #    genre_graph.json. 2026-09-04: previously the graph was only ever
    #    built in-memory inside SpiralOrchestrator._build_graph() and
    #    discarded when the run ended — no way to inspect or refresh it on
    #    its own. Same confidence filter as _build_graph() (>=0.7,
    #    lastfm/tidal/discogs/musicbrainz/override sources).
    if mode == "rebuild-genre-graph":
        try:
            import config as _cfg
            if getattr(_cfg, "OFFLINE_MODE", False):
                import spotify_local_data as _sld
                track_map, _liked, _albums = _sld.fetch_and_build_offline()
            else:
                import spotify_client as _sc
                _sp = _sc.get_spotify_client()
                track_map, _liked, _albums = _sc.fetch_and_build(_sp)
            log.info(f"[GenreGraph] Track map: {len(track_map)} playlist(s)")

            from genre_client import _get_genre_cache
            import genre_graph as _gg
            cache = _get_genre_cache()
            # 2026-09-04: the source filter enumerated only the OLD per-provider
            # tags (lastfm/tidal/discogs/musicbrainz/override). genre_client's
            # actual cache-writer (line ~4198) stamps almost everything
            # source="phase1" (its combined Last.fm+Deezer pass) — that value
            # was never in the accepted set, so this filter matched ZERO
            # entries against the real cache (4,599 confident phase1 entries,
            # all silently excluded) and the graph has been built empty every
            # time. confidence>=0.7 already isolates the real hits from the
            # 100 failed-lookup stubs (confidence=0) in the current cache, so
            # widen the source set rather than trust it alone.
            confident = [
                {"artist": k.split("|")[0], "genre": v.get("genre", "")}
                for k, v in cache.items()
                if isinstance(v, dict)
                   and v.get("confidence", 0) >= 0.7
                   and v.get("source") in ("lastfm", "tidal", "discogs",
                                            "musicbrainz", "override", "phase1")
                   and v.get("genre")
            ]
            log.info(f"[GenreGraph] {len(confident)} confident cache entr(y/ies)")
            if not confident:
                log.warning("[GenreGraph] No confident entries — nothing to build. "
                            "Run an audit/sync first to populate the genre cache.")
                return 1

            prev_enabled = _cfg.GENRE_GRAPH_ENABLED
            _cfg.GENRE_GRAPH_ENABLED = True
            try:
                g = _gg.build_graph(track_map=track_map, tracks=iter(confident),
                                     _override_purity=0.4)
            finally:
                _cfg.GENRE_GRAPH_ENABLED = prev_enabled

            n_artists = len(g.adjacency)
            n_edges = sum(len(nb) for nb in g.adjacency.values()) // 2
            from collections import Counter as _Ctr2
            top_genres = _Ctr2(g.artist_modal.values()).most_common(10)
            log.info(f"[GenreGraph] Built: {n_artists} artist node(s), {n_edges} edge(s)")
            for gname, cnt in top_genres:
                log.info(f"[GenreGraph]   {gname}: {cnt} artist(s)")

            out = _gg.save_graph(g)
            log.info(f"[GenreGraph] Saved -> {out}")
            return 0
        except Exception as e:
            log.error(f"[GenreGraph] rebuild failed: {e}", exc_info=True)
            return 1

    # ── visualize-genre-graph: render the persisted graph to a PNG and
    #    open it. Read-only, no device/network. Run rebuild-genre-graph
    #    first if genre_graph.json doesn't exist yet or is stale.
    if mode == "visualize-genre-graph":
        try:
            import genre_graph as _gg
            g = _gg.load_graph()
            if not g.adjacency:
                log.error("[GenreGraph] No saved graph found — run "
                          "'rebuild-genre-graph' first.")
                return 1
            max_nodes = 250
            for a in sys.argv:
                if a.startswith("--max-nodes="):
                    try:
                        max_nodes = int(a.split("=", 1)[1])
                    except ValueError:
                        pass
            out = _gg.render_graph(g, max_nodes=max_nodes)
            log.info(f"[GenreGraph] Rendered -> {out}")
            try:
                os.startfile(str(out))
            except Exception as _oe:
                log.debug(f"[GenreGraph] Could not auto-open ({_oe}); open {out} manually")
            return 0
        except Exception as e:
            log.error(f"[GenreGraph] visualize failed: {e}", exc_info=True)
            return 1

    # ── completeness-report: read-only album-completeness check. For every
    #    album qualifying under the a/b/c rules (album_completeness.py),
    #    resolve its full expected tracklist and diff against what's
    #    actually on the device, WITHOUT downloading or deleting anything.
    #    2026-09-04: promoted from an ad-hoc scratchpad script after it
    #    caught 106 (later 93, post title-match fix) incomplete albums.
    if mode == "completeness-report":
        try:
            import config as _cfg
            import spotify_local_data as _sld
            import album_completeness as _ac
            import walkman_dedup as _wd

            track_map, _liked, saved_album_ids = _sld.fetch_and_build_offline() \
                if getattr(_cfg, "OFFLINE_MODE", False) else (None, None, None)
            if track_map is None:
                import spotify_client as _sc
                _sp = _sc.get_spotify_client()
                track_map, _liked, saved_album_ids = _sc.fetch_and_build(_sp)

            lib = _sld._read_json("YourLibrary.json") or {}
            library_albums = [
                {"artist": a.get("artist", ""), "album": a.get("album", a.get("name", "")),
                 "uri": a.get("uri", "")}
                for a in lib.get("albums", [])
            ]
            album_set = _ac.build_completion_album_set(
                track_map, saved_album_ids, library_albums,
                min_liked=int(getattr(_cfg, "ALBUM_COMPLETE_MIN_LIKED", 3)),
                artist_has_saved=bool(getattr(_cfg, "ALBUM_COMPLETE_ARTIST_HAS_SAVED", True)),
            )
            log.info(f"[CompletenessReport] completion set: {len(album_set)} album(s)")

            files, _sizes = _wd._bulk_device_scan(_wd._default_storages())
            dev_tracks: set = set()
            for f in files:
                parts = f["parts"]
                album_folder = parts[-1] if parts else ""
                stem = os.path.splitext(f["filename"])[0]
                bits = [b.strip() for b in stem.split(" - ")]
                title = re.sub(r"^\d{1,3}\s+", "", bits[-1]).strip() if bits else stem
                alb_from_name = bits[1] if len(bits) >= 3 else album_folder
                for alb in {_ac._norm(alb_from_name), _ac._norm(album_folder)}:
                    if alb:
                        dev_tracks.add((alb, _ac._norm(title)))
            log.info(f"[CompletenessReport] device: {len(dev_tracks)} (album,title) key(s)")

            scrapeable = [e for e in album_set if e.get("album_id")]
            log.info(f"[CompletenessReport] {len(scrapeable)} album(s) have a resolvable tracklist")

            incomplete = []
            for e in scrapeable:
                meta = _ac.spotify_album_meta(e["album_id"])
                if not meta or not meta.get("tracks"):
                    continue
                an = _ac._norm(e["album"])
                missing = [
                    tr.get("title") for tr in meta["tracks"]
                    if (tr.get("title") or "").strip()
                       and (an, _ac._norm(tr["title"])) not in dev_tracks
                ]
                if missing:
                    incomplete.append((e["artist"], e["album"], len(meta["tracks"]),
                                       missing, e["reason"]))

            incomplete.sort(key=lambda x: -len(x[3]))
            log.info(f"[CompletenessReport] {len(incomplete)}/{len(scrapeable)} album(s) incomplete")
            total_missing = 0
            for artist, album, nexp, missing, reason in incomplete:
                total_missing += len(missing)
                log.info(f"[CompletenessReport] {artist} -- {album}  "
                         f"[{nexp - len(missing)}/{nexp}]  ({reason})")
                for m in missing[:8]:
                    log.info(f"[CompletenessReport]     missing: {m}")
                if len(missing) > 8:
                    log.info(f"[CompletenessReport]     ... +{len(missing) - 8} more")
            log.info(f"[CompletenessReport] TOTAL missing tracks: {total_missing}")

            out_path = _cfg.BASE_DIR / "completeness_report.json"
            import json as _json
            out_path.write_text(_json.dumps([
                {"artist": a, "album": al, "expected": ne, "missing": m, "reason": r}
                for a, al, ne, m, r in incomplete
            ], ensure_ascii=False, indent=1), encoding="utf-8")
            log.info(f"[CompletenessReport] Saved -> {out_path}")
            return 0
        except Exception as e:
            log.error(f"[CompletenessReport] Failed: {e}", exc_info=True)
            return 1

    # ── artist-completeness-report: 2026-09-04 (user direction: followed
    #    artists whose catalog is incomplete, e.g. Freddie Gibbs -- followed,
    #    but Alfredo/Alfredo 2/Piñata not fully present -- fall through
    #    build_completion_album_set()'s rules a/b/c, which only fire per-album
    #    off an existing liked/saved signal ON THAT ALBUM. This works from the
    #    followed-artist list itself instead. Ranking is offline; discography
    #    + tracklists come from Deezer's public API (no auth) rather than
    #    Spotify's (currently 403'ing account-wide, an app-owner-level
    #    restriction) -- see album_completeness.py module note.
    #    Usage: python cli_runner.py artist-completeness-report [--top=N] [--min-score=N]
    if mode == "artist-completeness-report":
        try:
            import config as _cfg
            import album_completeness as _ac
            import walkman_dedup as _wd
            import json as _json_ac

            top_n = 40
            min_score = 0
            for a in sys.argv:
                if a.startswith("--top="):
                    top_n = int(a.split("=", 1)[1])
                elif a.startswith("--min-score="):
                    min_score = int(a.split("=", 1)[1])

            ranked = _ac.rank_followed_artists(min_score=min_score)
            log.info(f"[ArtistCompleteness] {len(ranked)} followed artist(s) with any "
                     f"engagement signal (of 255 total followed) — checking top {top_n}")
            for i, e in enumerate(ranked[:top_n], 1):
                log.info(f"[ArtistCompleteness]   #{i:<3} {e['artist']:<30} "
                         f"liked={e['liked']:<3} saved_albums={e['saved_albums']}")

            # 2026-09-06: keep the flat (norm_album -> {titles}) index — it
            # unions EVERY folder that maps to an album name, so a split album
            # (Music/X + Music/Artist/X) is still seen whole (DeviceLibrary's
            # _find_best_entry picks one folder and undercounts splits). But
            # store FUZZY-normalised titles and match fuzzily, so retagged /
            # variant / typo'd titles ("Somthing's Missing", "Darling, I",
            # "- 2019 Remaster") stop inflating the "missing" count.
            import device_library as _devlib
            _fz = _devlib._fuzzy_norm_title
            files, _sizes = _wd._bulk_device_scan(_wd._default_storages())
            dev_by_album: dict = {}
            for f in files:
                parts = f["parts"]
                album_folder = parts[-1] if parts else ""
                stem = os.path.splitext(f["filename"])[0]
                bits = [b.strip() for b in stem.split(" - ")]
                title = bits[-1] if bits else stem
                alb_from_name = bits[1] if len(bits) >= 3 else album_folder
                for alb in {_ac._norm(alb_from_name), _ac._norm(album_folder)}:
                    if alb:
                        dev_by_album.setdefault(alb, set()).add(_fz(title))
            log.info(f"[ArtistCompleteness] device: {len(files)} file(s), "
                     f"{len(dev_by_album)} album key(s)")

            def _dev_has(alb_name: str, tr_title: str) -> bool:
                dev = dev_by_album.get(_ac._norm(alb_name))
                if not dev:
                    return False
                return _devlib._fuzzy_title_in(tr_title, dev)

            report = {"ranked_artists": ranked[:top_n], "artists": []}
            for e in ranked[:top_n]:
                albums = _ac.fetch_artist_albums(e["artist"])
                if not albums:
                    log.info(f"[ArtistCompleteness] {e['artist']}: could not resolve "
                             f"discography (Deezer lookup miss) — skipped")
                    continue
                artist_out = {"artist": e["artist"], "liked": e["liked"],
                             "saved_albums": e["saved_albums"], "albums": []}
                for alb in albums:
                    tracks = _ac.fetch_album_tracklist(alb["id"])
                    missing = [
                        tr.get("title") for tr in tracks
                        if (tr.get("title") or "").strip()
                           and not _dev_has(alb["name"], tr["title"])
                    ]
                    artist_out["albums"].append({
                        "album": alb["name"], "album_type": alb["album_type"],
                        "release_date": alb["release_date"],
                        "expected": len(tracks) or alb.get("total_tracks", 0),
                        "missing_count": len(missing), "missing": missing,
                    })
                incomplete_albums = [a for a in artist_out["albums"] if a["missing_count"]]
                if incomplete_albums:
                    log.info(f"[ArtistCompleteness] {e['artist']}: "
                             f"{len(incomplete_albums)}/{len(albums)} album(s) incomplete")
                    for a in incomplete_albums:
                        log.info(f"[ArtistCompleteness]     {a['album']} "
                                 f"[{a['expected']-a['missing_count']}/{a['expected']}]")
                report["artists"].append(artist_out)

            out_path = _cfg.BASE_DIR / "artist_completeness_report.json"
            out_path.write_text(_json_ac.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
            log.info(f"[ArtistCompleteness] Saved -> {out_path}")
            return 0
        except Exception as e:
            log.error(f"[ArtistCompleteness] Failed: {e}", exc_info=True)
            return 1

    # ── build-favorites-playlists-plan: read-only. 2026-09-04 (user direction:
    #    "organize the current library for favorites and playlists"). The
    #    native Walkman app has no batch-write path right now (no root, its
    #    ContentProvider isn't exported — see memory) and its own UI is
    #    one-track-at-a-time by design even once that's unblocked. So instead
    #    of re-deriving this live against a device that may or may not be
    #    reachable, resolve every Spotify Liked / playlist track to its real
    #    on-device file ONCE and save the worklist — whatever automation path
    #    eventually works (UI-tap loop, or a root SQL script) just consumes
    #    this JSON rather than re-matching titles from scratch.
    if mode == "build-favorites-playlists-plan":
        try:
            import config as _cfg
            import spotify_local_data as _sld
            import album_completeness as _ac
            import walkman_dedup as _wd

            track_map, liked_ids, saved_album_ids = _sld.fetch_and_build_offline() \
                if getattr(_cfg, "OFFLINE_MODE", False) else (None, None, None)
            if track_map is None:
                import spotify_client as _sc
                _sp = _sc.get_spotify_client()
                track_map, liked_ids, saved_album_ids = _sc.fetch_and_build(_sp)
            log.info(f"[FavPlaylistPlan] {len(track_map)} playlist(s) from local export")

            # Bulk device scan (the Task 35 fast path — ~20-30s for the whole
            # library) -> normalized (artist,title) -> [device file ref, ...]
            files, _sizes = _wd._bulk_device_scan(_wd._default_storages())
            device_index: dict = {}
            for f in files:
                parts = f["parts"]
                album_folder = parts[-1] if parts else ""
                stem = os.path.splitext(f["filename"])[0]
                bits = [b.strip() for b in stem.split(" - ")]
                title = re.sub(r"^\d{1,3}\s+", "", bits[-1]).strip() if bits else stem
                artist = bits[0] if len(bits) >= 3 else ""
                ref = {"storage": f["storage"], "parts": parts, "filename": f["filename"],
                       "album_folder": album_folder}
                for a_guess in {artist, album_folder}:
                    if not a_guess:
                        continue
                    key = (_ac._norm(a_guess), _ac._norm(title))
                    device_index.setdefault(key, []).append(ref)
            log.info(f"[FavPlaylistPlan] device: {len(files)} audio file(s), "
                     f"{len(device_index)} (artist,title) key(s)")

            def _resolve(tracks: list) -> tuple:
                resolved, missing = [], []
                for t in tracks:
                    if not isinstance(t, dict):
                        continue
                    artist = t.get("artist") or (t.get("artists") or [""])[0]
                    title = t.get("title", "")
                    key = (_ac._norm(artist), _ac._norm(title))
                    hit = device_index.get(key)
                    entry = {"artist": artist, "title": title,
                             "album": t.get("album", ""), "uri": t.get("uri", "")}
                    if hit:
                        entry["device"] = hit[0]
                        resolved.append(entry)
                    else:
                        missing.append(entry)
                return resolved, missing

            plan: dict = {"favorites": {}, "playlists": {}}
            liked_tracks = track_map.get("Liked Songs", [])
            fav_resolved, fav_missing = _resolve(liked_tracks)
            plan["favorites"] = {
                "resolved": fav_resolved, "missing": fav_missing,
                "resolved_count": len(fav_resolved), "missing_count": len(fav_missing),
            }
            log.info(f"[FavPlaylistPlan] Favorites: {len(fav_resolved)}/{len(liked_tracks)} "
                     f"resolved to a device file")

            for pl_name, tracks in track_map.items():
                if pl_name == "Liked Songs":
                    continue
                resolved, missing = _resolve(tracks)
                plan["playlists"][pl_name] = {
                    "resolved": resolved, "missing": missing,
                    "resolved_count": len(resolved), "missing_count": len(missing),
                }
                log.info(f"[FavPlaylistPlan]   '{pl_name}': {len(resolved)}/{len(tracks)} resolved")

            out_path = _cfg.BASE_DIR / "favorites_playlists_plan.json"
            import json as _json
            out_path.write_text(_json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            total_pl_tracks = sum(len(t) for k, t in track_map.items() if k != "Liked Songs")
            total_pl_resolved = sum(p["resolved_count"] for p in plan["playlists"].values())
            log.info(
                f"[FavPlaylistPlan] TOTAL: favorites {len(fav_resolved)}/{len(liked_tracks)}, "
                f"playlists {total_pl_resolved}/{total_pl_tracks} across "
                f"{len(plan['playlists'])} playlist(s)"
            )
            log.info(f"[FavPlaylistPlan] Saved -> {out_path}")
            return 0
        except Exception as e:
            log.error(f"[FavPlaylistPlan] Failed: {e}", exc_info=True)
            return 1

    # ── preview-acoustid-corrections: Task 11, read-only ─────────────────────
    # Shows what mk39_phase_III_fallback.attempt_fingerprint_fallback() would
    # resolve for local files whose current tags are blank (artist or title),
    # WITHOUT writing anything — attempt_fingerprint_fallback itself has no
    # write side-effect (fpcalc + AcoustID + MusicBrainz are all read-only;
    # only a local JSON fingerprint cache is touched). Use this to review
    # before setting config.ACOUSTID_AUTO_CORRECT_ENABLED = True.
    if mode == "preview-acoustid-corrections":
        try:
            import pathlib
            import config as _cfg
            from audit import _read_tags
            import mk39_phase_III_fallback as _p3fb

            folder_arg = next(
                (sys.argv[i + 1] for i, a in enumerate(sys.argv)
                 if a == "--folder" and i + 1 < len(sys.argv)),
                None,
            )
            root = pathlib.Path(folder_arg) if folder_arg else pathlib.Path(_cfg.DOWNLOAD_DIR)
            if not root.is_dir():
                log.error(f"[PreviewAcoustID] Not a directory: {root}")
                return 2

            scanned = 0
            blank = 0
            resolved = 0
            for f in root.rglob("*"):
                if f.suffix.lower() not in (".flac", ".mp3", ".m4a"):
                    continue
                scanned += 1
                current = _read_tags(f)
                if not _p3fb.is_phase_I_resolution_blank(current):
                    continue
                blank += 1
                identity = _p3fb.attempt_fingerprint_fallback(f, current)
                if identity is None:
                    log.info(f"[PreviewAcoustID] {f.name}: blank, no confident fingerprint match")
                    continue
                resolved += 1
                log.info(
                    f"[PreviewAcoustID] {f.name}: would resolve -> "
                    f"artist={identity.artist!r} title={identity.title!r} "
                    f"album={identity.album!r} year={identity.year!r} "
                    f"(score={identity.score:.2f})"
                )
            log.info(
                f"[PreviewAcoustID] scanned={scanned} blank={blank} "
                f"would_resolve={resolved} (nothing written — preview only)"
            )
            return 0
        except Exception as e:
            log.error(f"[PreviewAcoustID] Failed: {e}", exc_info=True)
            return 1

    # ── preview-listenbrainz-discovery: Task 13, read-only ───────────────────
    # Shows what fetch_discovery_queue() would return for a given username,
    # WITHOUT downloading, tagging, or queuing anything. Calls the two
    # lower-level functions directly (not fetch_discovery_queue() itself,
    # which short-circuits on config.LISTENBRAINZ_DISCOVERY_ENABLED) so this
    # can be reviewed BEFORE flipping that flag on — same "preview before
    # enabling" pattern as preview-genre-consolidation/preview-acoustid-
    # corrections. Nothing here is wired into the download queue; that's a
    # separate, not-yet-made decision once the user's own account is
    # confirmed to resolve correctly.
    if mode == "preview-listenbrainz-discovery":
        try:
            import config as _cfg
            import listenbrainz_client as _lb

            username_arg = next(
                (sys.argv[i + 1] for i, a in enumerate(sys.argv)
                 if a == "--username" and i + 1 < len(sys.argv)),
                None,
            )
            username = username_arg or getattr(_cfg, "LISTENBRAINZ_USERNAME", "")
            if not username:
                log.error(
                    "[PreviewListenBrainz] No username given — pass --username NAME "
                    "or set config.LISTENBRAINZ_USERNAME"
                )
                return 2

            playlists = _lb.fetch_recommendation_playlists(username)
            if not playlists:
                log.info(
                    f"[PreviewListenBrainz] No recommendation playlists found for "
                    f"{username!r} (wrong username, no listening history yet, or "
                    f"ListenBrainz hasn't generated this week's playlists)"
                )
                return 0

            seen: set[tuple[str, str]] = set()
            total = 0
            for pl in playlists:
                tracks = _lb.fetch_playlist_tracks(pl["mbid"])
                new_in_playlist = 0
                for t in tracks:
                    key = (t["artist"].lower(), t["title"].lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    new_in_playlist += 1
                    total += 1
                log.info(
                    f"[PreviewListenBrainz] {pl['title']!r}: {len(tracks)} track(s), "
                    f"{new_in_playlist} not seen in an earlier playlist this run"
                )
            log.info(
                f"[PreviewListenBrainz] {len(playlists)} playlist(s), {total} unique "
                f"track(s) total — nothing downloaded or queued, preview only"
            )
            return 0
        except Exception as e:
            log.error(f"[PreviewListenBrainz] Failed: {e}", exc_info=True)
            return 1

    # ── merge-splintered-folders: one-time on-device cleanup (Issue 3) ───────
    # Groups Music/ folders whose (artist, album-prefix) normalize to the same
    # key (i.e. they're truncation variants of the same album — see
    # walkman_dedup._prefix_normalize_truncated / _strip_trailing_truncation),
    # picks the folder with the most files as canonical, and for every other
    # folder in the group: deletes files that already exist in canonical
    # (duplicates), or moves (pull -> push -> delete-original) files that
    # exist ONLY in the splinter folder so nothing unique is lost.
    # Dry-run by default — review the plan before --execute on a device this
    # close to full.
    if mode == "merge-splintered-folders":
        execute = "--execute" in sys.argv
        try:
            import tempfile, pathlib
            import config as _cfg
            import walkman_dedup as _dedup
            import mtp_transfer as _mtp
            import mk37_wiring as _wiring

            files = _dedup.snapshot_to_file_list()
            if not files:
                log.error("[MergeFolders] No walkman_snapshot.json data — run a scan first")
                return 1

            def _del_or_raise(storage: str, parts: list, fn: str) -> None:
                ok = _mtp.delete_file_on_device(
                    device_name=getattr(_cfg, "DEVICE_NAME", "WALKMAN"),
                    storage_name=storage, folder_parts=parts, filename=fn,
                )
                if not ok:
                    raise RuntimeError(f"delete failed for {fn!r}")

            # storage -> folder(parts tuple) -> [filenames]
            folders: dict = {}
            for f in files:
                key = (f["storage"], tuple(f["parts"]))
                folders.setdefault(key, []).append(f["filename"])

            def _group_key(storage: str, parts: tuple) -> tuple:
                if not parts:
                    return (storage, "", "")
                name = parts[-1]
                if " - " in name:
                    artist, album = name.split(" - ", 1)
                else:
                    artist, album = "", name
                artist_n = _dedup._norm(artist.strip())
                album = album.strip()
                album_n = (_dedup._norm(_dedup._prefix_normalize_truncated(album))
                           if len(album) >= 32 else _dedup._norm(album))
                return (storage, artist_n, album_n)

            groups: dict = {}
            for (storage, parts), fnames in folders.items():
                gk = _group_key(storage, parts)
                groups.setdefault(gk, []).append((parts, fnames))

            splinter_groups = {gk: v for gk, v in groups.items() if len(v) > 1}
            log.info(f"[MergeFolders] {len(splinter_groups)} splintered album group(s) found")

            n_move = n_dup = n_err = 0
            for gk, variants in splinter_groups.items():
                storage = gk[0]
                variants.sort(key=lambda v: (len(v[1]), len("/".join(v[0]))), reverse=True)
                (canon_parts, canon_fnames), *others = variants
                canon_stems = {pathlib.Path(fn).stem.lower(): fn for fn in canon_fnames}
                log.info(
                    f"[MergeFolders] Canonical: '{storage}'/{'/'.join(canon_parts)} "
                    f"({len(canon_fnames)} files) <- {len(others)} splinter folder(s)"
                )
                for splinter_parts, splinter_fnames in others:
                    for fn in splinter_fnames:
                        stem = pathlib.Path(fn).stem.lower()
                        is_dup = stem in canon_stems
                        label = f"'{storage}'/{'/'.join(splinter_parts)}/{fn}"
                        if not execute:
                            log.info(f"[MergeFolders] DRY-RUN {'DELETE (dup)' if is_dup else 'MOVE'}: {label}")
                            continue
                        if is_dup:
                            res = _wiring.idempotent_destructive_op(
                                f"{storage}|{'|'.join(splinter_parts)}|{fn}", "delete",
                                lambda sp=splinter_parts, f=fn: _del_or_raise(storage, list(sp), f),
                                actor="merge_splintered_folders",
                            )
                            if res.get("executed") or res.get("duplicate"):
                                n_dup += 1
                            else:
                                n_err += 1
                                log.warning(f"[MergeFolders] Delete failed: {label}: {res.get('error')}")
                        else:
                            tmp_dir = pathlib.Path(tempfile.mkdtemp())
                            local_file = tmp_dir / fn
                            try:
                                pulled = _mtp.read_file_from_device(
                                    src_parts=list(splinter_parts), filename=fn,
                                    dest_local=local_file,
                                    device_name=getattr(_cfg, "DEVICE_NAME", "WALKMAN"),
                                    storage_name=storage,
                                )
                                if not (pulled and local_file.exists()):
                                    log.warning(f"[MergeFolders] Pull failed: {label}")
                                    n_err += 1
                                    continue
                                res = _wiring.transactional_dedup(
                                    source_local_path=str(local_file),
                                    device_relpath="/".join(list(canon_parts) + [fn]),
                                    transfer_fn=lambda: {"completed": _mtp.copy_file_to_device(
                                        src=local_file, dest_parts=list(canon_parts),
                                        device_name=getattr(_cfg, "DEVICE_NAME", "WALKMAN"),
                                        storage_name=storage, overwrite=False,
                                    ), "accepted": True, "effective_tier": "MERGE"},
                                    delete_source_fn=lambda: _mtp.delete_file_on_device(
                                        device_name=getattr(_cfg, "DEVICE_NAME", "WALKMAN"),
                                        storage_name=storage, folder_parts=list(splinter_parts),
                                        filename=fn,
                                    ),
                                )
                                if res.get("committed") and res.get("source_deleted"):
                                    n_move += 1
                                else:
                                    n_err += 1
                                    log.warning(f"[MergeFolders] Move incomplete: {label}: {res.get('reason')}")
                            finally:
                                import shutil as _sh
                                _sh.rmtree(tmp_dir, ignore_errors=True)

            if not execute:
                log.info("[MergeFolders] DRY-RUN complete — pass --execute to apply")
                return 0
            log.info(f"[MergeFolders] Done — moved={n_move} deleted_dupes={n_dup} errors={n_err}")
            return 0
        except Exception as e:
            log.error(f"[MergeFolders] Failed: {e}")
            return 1

    # ── purge-genre-cache: un-poison the genre inference cache (Issue 2) ─────
    # Pass --artist "The Weeknd" to purge just that artist; omit for the whole
    # cache. Safe default: dry-run shows the count without deleting.
    if mode == "purge-genre-cache":
        execute = "--execute" in sys.argv
        artist_arg = next(
            (sys.argv[i + 1] for i, a in enumerate(sys.argv)
             if a == "--artist" and i + 1 < len(sys.argv)), None,
        )
        try:
            import genre_client as _gc
            import mk37_wiring as _wiring
            _target = f"artist={artist_arg!r}" if artist_arg else "ENTIRE CACHE"
            if not execute:
                log.info(f"[PurgeGenreCache] DRY-RUN — would purge {_target}. Pass --execute to apply.")
                return 0
            _n_box: list = []
            _res = _wiring.idempotent_destructive_op(
                f"genre_cache:{artist_arg or '__all__'}", "purge",
                lambda: _n_box.append(_gc.purge_genre_cache(artist_arg)),
                actor="purge_genre_cache",
            )
            if not _res.get("executed") and not _res.get("duplicate"):
                log.error(f"[PurgeGenreCache] Gate blocked purge: {_res.get('error')}")
                return 1
            _n = _n_box[0] if _n_box else 0
            log.info(f"[PurgeGenreCache] Purged {_n} entries ({_target})")
            return 0
        except Exception as e:
            log.error(f"[PurgeGenreCache] Failed: {e}")
            return 1

    # ── purge-deleted: targeted deletion of the .deleted staging folder ──────
    # Bypasses full enumeration — uses ParseName() for O(1) per-level lookup so
    # it works even when the SD card is too full/slow for File Explorer to browse.
    if mode == "purge-deleted":
        try:
            import pythoncom; pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            import config as _cfg
            import wpd_transfer as _wpd
            device_name      = getattr(_cfg, "DEVICE_NAME", "WALKMAN")
            soft_delete_name = getattr(_cfg, "DEDUP_SOFT_DELETE_FOLDER", ".deleted")
            log.info(f"[Purge] Deleting {soft_delete_name!r} folder from all storages on {device_name!r}...")
            if dry_run:
                log.info("[Purge] DRY-RUN — skipping actual delete")
                return 0
            results = _wpd.purge_soft_deleted(device_name, soft_delete_name)
            n_deleted = sum(1 for v in results.values() if v == "deleted")
            n_clean   = sum(1 for v in results.values() if v == "already_clean")
            log.info(f"[Purge] Done — {n_deleted} deleted, {n_clean} already clean: {results}")
            return 0
        except Exception as e:
            log.error(f"[Purge] Failed: {e}")
            return 1

    # ── deep-clean: hard-delete duplicates using the existing snapshot ────────
    # No MTP scan needed — uses walkman_snapshot.json regardless of age.
    # Identifies format duplicates (flac+m4a of same track → keep flac, delete m4a)
    # and other duplicate pairs, then permanently removes the inferior copies via
    # ParseName-navigated PowerShell deletes (O(1) per folder level).
    #
    # Safe default: --dry-run shows the plan without deleting.
    # Pass --execute to actually run the hard deletes.
    if mode == "deep-clean":
        execute  = "--execute" in sys.argv
        use_log  = "--from-log" in sys.argv
        try:
            import pythoncom; pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            import config as _cfg
            from pathlib import Path as _P
            import json as _json_dc
            import wpd_transfer as _wpd2
            import mk37_wiring as _wiring
            import preflight as _preflight
            from collections import Counter as _Ctr, defaultdict as _dd

            if use_log:
                # ── MTP path: delete via WPD IPortableDeviceContent::Delete ──
                # Primary: delete_files_wpd() — direct WPD USB command, no Shell UI,
                #   no confirmation dialog possible at the OS level.
                # Fallback: delete_files_single_ps() — Shell.Application InvokeVerb,
                #   used only if WPD/comtypes is unavailable on this machine.
                # inferior_format: mp3/m4a where FLAC exists — FLAC copy is kept
                # small_file:      4096-byte corrupt stubs — not worth saving to PC
                _log_path = _P(__file__).parent / ".dedup_log.json"
                if not _log_path.exists():
                    log.error("[DeepClean] .dedup_log.json not found")
                    return 1
                # 2026-07-24: was hardcoded to the specific scan_total (~13237)
                # the library had back when this was written — a fresh scan via
                # the new 'scan-duplicates' command (or any scan since the
                # library's size changed, e.g. after deep-clean itself removes
                # thousands of files) would never match that stale range and
                # silently be ignored. Always use the most recent scan instead —
                # that's what "run a scan, then deep-clean" actually means.
                _log_entries = _json_dc.loads(_log_path.read_text(encoding="utf-8"))
                _entry = max(
                    _log_entries, key=lambda e: e.get("timestamp", ""), default=None,
                )
                if not _entry:
                    log.error("[DeepClean] .dedup_log.json has no entries — run 'scan-duplicates' first")
                    return 1
                _cands = _entry["candidates"]

                # 2026-09-04: deep-clean --from-log deletes via its own
                # direct-filesystem / ADB-pull / MTP-WPD paths below, NONE of
                # which go through walkman_dedup.DedupEngine.execute() (the
                # only place that already had a protected-path check, and
                # only for its own _hard_delete/_soft_delete). Filter the
                # candidate list itself here instead, so protection holds no
                # matter which of the three deletion mechanisms below ends up
                # handling a given candidate. Favorites/playlists live in the
                # app's own DB keyed to this exact file -- deleting it orphans
                # that DB state even if a duplicate copy survives. See
                # walkman_dedup.protect_favorited_and_playlisted_paths() and
                # memory project_walkman_hires_player_playlists_favorites.md.
                try:
                    import walkman_dedup as _wd_protect
                    _protected_paths = _wd_protect._load_favplaylist_protected_paths()
                except Exception as _prot_e:
                    log.debug(f"[DeepClean] favorites/playlists protection load skipped: {_prot_e}")
                    _protected_paths = set()
                if _protected_paths:
                    _before_n = len(_cands)
                    _cands = [
                        c for c in _cands
                        if c.get("human_path", "").replace("\\", "/").strip() not in _protected_paths
                    ]
                    _n_protected = _before_n - len(_cands)
                    if _n_protected:
                        log.info(
                            f"[DeepClean] Skipping {_n_protected} candidate(s) that are "
                            f"favorited or playlisted per favorites_playlists_plan.json"
                        )

                _reasons = _Ctr(c["reason"] for c in _cands)
                log.info(
                    f"[DeepClean] Log entry {_entry['timestamp']} — "
                    f"{len(_cands)} files: {dict(_reasons)}"
                )

                if not execute:
                    log.info("[DeepClean] DRY-RUN — pass --execute to delete via MTP")
                    for c in _cands[:20]:
                        log.info(f"  [{c['reason']}] {c.get('human_path', c.get('filename', ''))}")
                    if len(_cands) > 20:
                        log.info(f"  ... and {len(_cands) - 20} more")
                    return 0

                # MTP/Explorer COM state degrades across repeated scan/delete runs
                # (see project findings: stale Shell.Application sessions cause
                # $root.GetFolder.Items() to hang harder on each subsequent run).
                # Clear it before touching the device — cheap, safe, idempotent.
                try:
                    _preflight.ensure_run_once()
                except Exception as _pf_e:
                    log.warning(f"[DeepClean] Preflight cleanup raised (continuing): {_pf_e}")

                # ── Direct filesystem path (SD card in card reader) ───────────
                # Usage: python cli_runner.py deep-clean --from-log --execute --card-path E:
                # Bypasses MTP entirely. Only processes "SD card" storage entries.
                _card_arg = next(
                    (a.split("=", 1)[1] if "=" in a else None
                     for a in sys.argv if a.startswith("--card-path")),
                    next((sys.argv[sys.argv.index("--card-path") + 1]
                          for i, a in enumerate(sys.argv)
                          if a == "--card-path" and i + 1 < len(sys.argv)), None),
                )
                if _card_arg:
                    _card_root = _P(_card_arg)
                    if not _card_root.exists():
                        log.error(f"[DeepClean] Card path not found: {_card_root}")
                        return 1
                    log.info(f"[DeepClean] Direct filesystem mode — card root: {_card_root}")
                    _sd_cands = [c for c in _cands if "sd" in c.get("storage", "").lower()]
                    log.info(f"[DeepClean] {len(_sd_cands)} SD card files to delete")
                    _n_del = _n_nf = _n_err = 0
                    for _c in _sd_cands:
                        _fpath = _card_root.joinpath(*_c["parts"]) / _c["filename"]
                        if _fpath.exists():
                            # Route through MK37/MK39: every destructive op on-device
                            # must be capability-mediated + idempotent (no double-delete
                            # on replay/resume).
                            _res = _wiring.idempotent_destructive_op(
                                str(_fpath), "delete", _fpath.unlink, actor="deep_clean",
                            )
                            if _res.get("executed") or _res.get("duplicate"):
                                _n_del += 1
                                if _n_del % 100 == 0:
                                    log.info(f"[DeepClean] Deleted {_n_del}/{len(_sd_cands)}...")
                            else:
                                log.warning(f"[DeepClean] Could not delete {_fpath.name}: {_res.get('error')}")
                                _n_err += 1
                        else:
                            # Try stem match (filename without extension) in case
                            # the extension was normalised differently
                            _stem = _c["filename"].rsplit(".", 1)[0]
                            _folder = _card_root.joinpath(*_c["parts"])
                            _matches = list(_folder.glob(f"{_stem}.*")) if _folder.exists() else []
                            if _matches:
                                _res = _wiring.idempotent_destructive_op(
                                    str(_matches[0]), "delete", _matches[0].unlink, actor="deep_clean",
                                )
                                if _res.get("executed") or _res.get("duplicate"):
                                    _n_del += 1
                                    if _n_del % 100 == 0:
                                        log.info(f"[DeepClean] Deleted {_n_del}/{len(_sd_cands)}...")
                                else:
                                    log.warning(f"[DeepClean] Could not delete {_matches[0].name}: {_res.get('error')}")
                                    _n_err += 1
                            else:
                                _n_nf += 1
                    log.info(
                        f"[DeepClean] Direct-delete done — "
                        f"deleted={_n_del} not_found={_n_nf} error={_n_err} "
                        f"(of {len(_sd_cands)} SD card files)"
                    )
                    return 0

                # Group by storage → {parts_tuple: [filenames]} for delete_files_single_ps
                _device_name = getattr(_cfg, "DEVICE_NAME", "WALKMAN")
                _by_storage: dict = {}
                for c in _cands:
                    _st = c["storage"]
                    _pt = tuple(c["parts"])
                    _fn = c["filename"]
                    if _st not in _by_storage:
                        _by_storage[_st] = {}
                    _by_storage[_st].setdefault(_pt, []).append(_fn)

                # Load progress from prior interrupted runs so re-runs skip
                # already-deleted files (device GC on 98%-full SD card hangs
                # after ~9 files; multiple short runs accumulate progress).
                _prog_file = _P(__file__).parent / ".deep_clean_progress.json"
                _already_done: set = set()   # "storage|parts|filename" for done (deleted or NF) files
                _hung_parts: set = set()     # parts_tuples that hung last run — deferred to end of queue
                if _prog_file.exists():
                    try:
                        _prog_data = _json_dc.loads(_prog_file.read_text(encoding="utf-8"))
                        # support old format ("deleted"/"skip_parts") and new format ("done"/"hung_parts")
                        _already_done = set(_prog_data.get("done", _prog_data.get("deleted", [])))
                        _hung_parts = {tuple(sp) for sp in _prog_data.get(
                            "hung_parts", _prog_data.get("skip_parts", [])
                        )}
                        log.info(
                            f"[DeepClean] Resuming — {len(_already_done)} done, "
                            f"{len(_hung_parts)} albums deferred to end"
                        )
                    except Exception as _pe:
                        log.warning(f"[DeepClean] Could not load progress file: {_pe}")

                _n_del = _n_nf = _n_err = _n_navfail = 0
                # SD card first — fresh MTP session, 3484 valid audio files
                # Internal storage stubs are Sony-firmware-hidden from MTP (processed last)
                _storage_order = sorted(
                    _by_storage.items(),
                    key=lambda kv: (0 if "sd" in kv[0].lower() else 1),
                )
                # WPD skipped: ZX500 Android MTP stack never exposes storages via
                # WPD content API, and an open device.Open() handle blocks
                # Shell.Application from seeing the device in the PS subprocess.
                for _storage_name, _groups in _storage_order:
                    # Skip files confirmed deleted in prior runs
                    # Build todo: non-hung groups first, previously-hung groups at end.
                    # Hung groups are NOT omitted — they're deferred and retried later
                    # when the SD card has more free space (after bulk deletions).
                    _groups_fresh: dict = {}
                    _groups_deferred: dict = {}
                    for _pt, _fns in _groups.items():
                        _todo = [fn for fn in _fns
                                 if f"{_storage_name}|{'|'.join(_pt)}|{fn}" not in _already_done]
                        if not _todo:
                            continue
                        if _pt in _hung_parts:
                            _groups_deferred[_pt] = _todo
                        else:
                            _groups_fresh[_pt] = _todo
                    _groups_todo = {**_groups_fresh, **_groups_deferred}
                    _n_skip = sum(len(v) for v in _groups.values()) - sum(len(v) for v in _groups_todo.values())
                    _n_this = sum(len(v) for v in _groups_todo.values())
                    _n_deferred = sum(len(v) for v in _groups_deferred.values())
                    if not _groups_todo:
                        log.info(f"[DeepClean] '{_storage_name}': all files processed")
                        continue
                    _detail = f", {_n_deferred} deferred to end" if _n_deferred else ""
                    if _n_skip:
                        log.info(f"[DeepClean] '{_storage_name}': skipping {_n_skip} already done, {_n_this} remaining{_detail}")
                    else:
                        log.info(f"[DeepClean] MTP deleting {_n_this} files from '{_storage_name}'{_detail}...")
                    # MK37/S3 + MK39-gate: the batch delete is a destructive op and
                    # must be capability/idempotency-mediated like any other mutation.
                    # Keyed on (storage, content-hash) so a duplicate invocation of
                    # the exact same pending batch (e.g. two overlapping scheduled
                    # runs) is a no-op instead of a second delete attempt; a batch
                    # with different pending content (the normal case as files get
                    # marked done) is treated as a new operation.
                    import hashlib as _hashlib_dc
                    _batch_key = _storage_name + ":" + _hashlib_dc.sha1(
                        _json_dc.dumps(sorted(_groups_todo.items()), ensure_ascii=False).encode("utf-8")
                    ).hexdigest()[:12]
                    # 2026-07-23: try the ADB direct-path delete first — it bypasses
                    # delete_files_single_ps's whole-Music-folder enumeration (the
                    # confirmed hang point on a near-full/degraded SD card), since
                    # every candidate's path is already known. Falls back to the
                    # PS/MTP path only if ADB itself isn't connected right now.
                    # 2026-07-23: idempotent_destructive_op marks a batch "executed"
                    # on any call that doesn't raise — it has no visibility into
                    # whether the underlying delete actually accomplished anything.
                    # A batch that hangs/errors on every file (e.g. the pre-ADB-fix
                    # SD card enumeration hang) still "succeeds" from its point of
                    # view, permanently marking that batch's content-hash as done —
                    # so a retry against the IDENTICAL still-pending file set is
                    # then silently skipped forever (confirmed live: exactly this
                    # happened to the very first hung attempt the night before this
                    # fix). Raise here when the result shows zero real forward
                    # progress (no "deleted"/"not_found" among the results) so the
                    # gate does NOT mark it executed and a later retry can proceed.
                    def _do_delete_batch(_sn=_storage_name, _gt=_groups_todo):
                        _adb_res = _wpd2.delete_files_adb_by_path(_device_name, _sn, "Music", _gt)
                        if _adb_res:
                            _result = _adb_res
                        else:
                            log.info(f"[DeepClean] '{_sn}': ADB path unavailable — falling back to PS/MTP delete")
                            _result = _wpd2.delete_files_single_ps(_device_name, _sn, "Music", _gt)
                        _made_progress = any(
                            v in ("deleted", "not_found")
                            for k, v in _result.items() if k != ("__HUNG_GI__",)
                        )
                        if not _made_progress:
                            raise RuntimeError(
                                f"'{_sn}' batch made no forward progress "
                                f"({len(_result)} result(s), none deleted/not_found)"
                            )
                        return _result

                    _r_box: list = []
                    _gate_res = _wiring.idempotent_destructive_op(
                        _batch_key, "dedup",
                        lambda: _r_box.append(_do_delete_batch()),
                        actor="deep_clean",
                    )
                    if _gate_res.get("duplicate"):
                        log.info(f"[DeepClean] '{_storage_name}': batch already executed this session (idempotent skip)")
                        continue
                    if not _gate_res.get("executed"):
                        log.warning(f"[DeepClean] '{_storage_name}': gate blocked batch: {_gate_res.get('error')}")
                        continue
                    _r = _r_box[0] if _r_box else {}
                    _hung_gi = None
                    for _key, _v in (_r or {}).items():
                        if _key == ("__HUNG_GI__",):
                            _hung_gi = _v
                            continue
                        _pt_key, _fn_key = _key[:-1], _key[-1]
                        if _v == "deleted":
                            _n_del += 1
                            _already_done.add(f"{_storage_name}|{'|'.join(_pt_key)}|{_fn_key}")
                        elif _v == "not_found":
                            _n_nf += 1
                            _already_done.add(f"{_storage_name}|{'|'.join(_pt_key)}|{_fn_key}")
                        elif _v == "nav_failed":
                            # Root-level navigation failed for the whole batch — we
                            # never actually checked these files. Do NOT mark done,
                            # so the next run retries them instead of silently
                            # skipping real work (2026-07-17 fix — see wpd_transfer.py).
                            _n_navfail += 1
                        else:
                            _n_err += 1
                    if _hung_gi is not None:
                        if _hung_gi == -1:
                            # Items() enumeration never returned — the shared
                            # Explorer.exe COM host (which Shell.Application connects
                            # to) is wedged from a prior killed session, not the
                            # device itself. Attempt the same in-process recovery
                            # that fixes this manually (see project findings):
                            # close stale Explorer windows referencing the device,
                            # and best-effort cycle the WPD service if permitted.
                            log.error(
                                "[DeepClean] Device MTP stack unresponsive (hung before first group) — "
                                "attempting Explorer/WPD recovery before giving up"
                            )
                            try:
                                _rec = _preflight.run()
                                log.info(f"[DeepClean] Recovery attempt: {_rec}")
                            except Exception as _rec_e:
                                log.warning(f"[DeepClean] Recovery attempt raised: {_rec_e}")
                            log.error(
                                "[DeepClean] Re-run deep-clean now — if this repeats, "
                                "unplug Walkman, wait 60s, replug (recovery could not "
                                "reach the device-level MTP session from software alone)"
                            )
                            # Save progress as-is so nothing is lost, then signal the
                            # loop wrapper to stop retrying until device is reconnected.
                            try:
                                _prog_file.write_text(
                                    _json_dc.dumps({
                                        "done": sorted(_already_done),
                                        "hung_parts": [list(sp) for sp in sorted(_hung_parts)],
                                    }, ensure_ascii=False),
                                    encoding="utf-8",
                                )
                            except Exception:
                                pass
                            return 2  # distinct exit code for "reconnect required"
                        _sorted_todo = list(_groups_todo.items())
                        if 0 <= _hung_gi < len(_sorted_todo):
                            _hung_pt, _ = _sorted_todo[_hung_gi]
                            _hung_parts.add(_hung_pt)
                            log.warning(
                                f"[DeepClean] Deferred hanging album to end of queue: {_hung_pt} "
                                f"(will retry when card has more free space)"
                            )

                # Persist progress so next run resumes without re-processing known files
                try:
                    _prog_file.write_text(
                        _json_dc.dumps({
                            "done": sorted(_already_done),
                            "hung_parts": [list(sp) for sp in sorted(_hung_parts)],
                        }, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception as _pe:
                    log.warning(f"[DeepClean] Could not save progress file: {_pe}")

                _total_processed = len(_already_done)
                _total_planned = len(_cands)
                log.info(
                    f"[DeepClean] Done — deleted={_n_del} not_found={_n_nf} "
                    f"error={_n_err} nav_failed={_n_navfail} (of {_n_this} this run) | "
                    f"processed={_total_processed}/{_total_planned} deferred_albums={len(_hung_parts)}"
                )
                if _n_navfail:
                    log.warning(
                        f"[DeepClean] {_n_navfail} file(s) not attempted (device navigation "
                        f"failed) — NOT marked done, will retry next run once the device is "
                        f"healthy again."
                    )
                if _total_processed < _total_planned:
                    log.info(f"[DeepClean] ~{_total_planned - _total_processed} files remain — re-run to continue")
                log.info("[DeepClean] Reconnect device to trigger MediaStore rescan when complete")
                return 0

            else:
                # ── Snapshot path: ADB rm (kept for when ADB is available) ──
                snap_path = getattr(_cfg, "BASE_DIR", None)
                if snap_path:
                    snap_path = _P(snap_path) / "walkman_snapshot.json"
                if snap_path is None or not _P(snap_path).exists():
                    log.error("[DeepClean] No snapshot — run walkman_tree_gen.py first, or use --from-log")
                    return 1

                log.info(f"[DeepClean] Snapshot: {snap_path}")
                _snap_count = len(_json_dc.loads(_P(snap_path).read_text(encoding="utf-8")))
                if _snap_count < 5000:
                    log.error(
                        f"[DeepClean] Snapshot has only {_snap_count} tracks — incomplete. "
                        f"Re-run walkman_tree_gen.py --wait, or use --from-log."
                    )
                    return 1

                import sys as _sys2, unittest.mock as _mock2
                if "ai_helper" not in _sys2.modules:
                    _sys2.modules["ai_helper"] = _mock2.MagicMock()
                import walkman_dedup as _wdd
                delete_list = _wdd.build_delete_list_from_snapshot(snap_path)

                n_planned = len(delete_list)
                if not n_planned:
                    log.info("[DeepClean] No duplicates found in snapshot")
                    return 0

                def _reason_type(r):
                    if r.startswith("exact-dup"):      return "exact-dup"
                    if r.startswith("cross-storage"):  return "cross-storage-dup"
                    return r.split("(")[0].strip()
                log.info(f"[DeepClean] {n_planned} files: {dict(_Ctr(_reason_type(d['reason']) for d in delete_list))}")

                if not execute:
                    log.info("[DeepClean] DRY-RUN — pass --execute to delete via ADB")
                    for d in delete_list[:20]:
                        log.info(f"  [{d['reason']}] {d['_data']}")
                    if n_planned > 20:
                        log.info(f"  ... and {n_planned - 20} more")
                    return 0

                from pathlib import Path as _P3
                _move_to = next(
                    (a.split("=", 1)[1] for a in sys.argv if a.startswith("--move-to=")),
                    str(_P3.home() / "WalkmanRemovedFiles"),
                )
                log.info(f"[DeepClean] ADB pull+delete → {_move_to}")
                results = _wpd2.adb_move_and_delete(delete_list, _move_to)
                if not results:
                    log.error("[DeepClean] ADB unavailable — use --from-log for MTP deletion instead")
                    return 1
                _n_ok  = sum(1 for v in results.values() if v in ("moved", "deleted"))
                _n_fail = sum(1 for v in results.values() if "fail" in v)
                log.info(f"[DeepClean] Done — ok={_n_ok} failed={_n_fail} (of {n_planned})")
                log.info("[DeepClean] Reconnect device to trigger MediaStore rescan")
                return 0

        except Exception as e:
            log.error(f"[DeepClean] Failed: {e}", exc_info=True)
            return 1

    # Dry-run: apply conservative config overrides BEFORE any imports use config
    if dry_run:
        import config as _cfg
        _cfg.AUDIT_BATCH_SIZE      = 50          # limit scope
        _cfg.DEDUP_MODE            = "report"    # no actual deletes
        _cfg.DEDUP_PRE_REORGANIZE  = False       # skip 20-hr dedup pass
        _cfg.DOWNLOAD_MISSING      = False       # plan only, no downloads
        log.info("[CLI] DRY-RUN: AUDIT_BATCH_SIZE=50, DEDUP_MODE=report, "
                 "DEDUP_PRE_REORGANIZE=False, DOWNLOAD_MISSING=False")

    # --device-only: skip the local-files (LOCAL_FOLDERS, e.g. ~/Downloads)
    # phase of `audit` mode entirely — go straight to the device scan/audit.
    # Independent of --dry-run since it's about *scope*, not write-safety.
    if "--device-only" in sys.argv:
        import config as _cfg
        _cfg.AUDIT_DEVICE_ONLY = True
        log.info("[CLI] --device-only: skipping local-file audit phase")

    if "--folder-audit" in sys.argv:
        import config as _cfg
        _cfg.AUDIT_FOLDER_MODE = True
        log.info("[CLI] --folder-audit: folder-wise audit mode")

    # --force-audit / --fresh-audit: the GUI's "Force re-audit" / "Nuke audit
    # state" checkboxes, forwarded here now that the GUI runs the pipeline as
    # this subprocess (main.py::_spiral_run). --force-audit re-checks every
    # file this run; --fresh-audit additionally discards .audit_state.json.
    if "--force-audit" in sys.argv:
        import config as _cfg
        _cfg.AUDIT_STALE_DAYS = 0
        try:
            import meta_key as _mk; _mk.invalidate_all()
        except Exception:
            pass
        log.info("[CLI] --force-audit: AUDIT_STALE_DAYS=0, meta_key cache invalidated")
    if "--fresh-audit" in sys.argv:
        try:
            import meta_key as _mk; _mk.invalidate_all()
        except Exception:
            pass
        try:
            import config as _cfg
            _asf = getattr(_cfg, "AUDIT_STATE_FILE", None)
            if _asf is not None and _asf.exists():
                _asf.unlink()
                log.info(f"[CLI] --fresh-audit: deleted {_asf.name} + invalidated meta_key cache")
        except Exception as _fae:
            log.warning(f"[CLI] --fresh-audit failed (non-fatal): {_fae}")

    log.info(f"[CLI] Starting spiral mode={mode!r} dry_run={dry_run}")

    # COM initialisation for the main thread (required for MTP Shell.Application)
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception as e:
        log.warning(f"[CLI] CoInitialize failed (non-fatal): {e}")

    # Preflight cleanup (Explorer-window close, optional WPD service cycle,
    # etc.) — the GUI path (main.py) already runs this before every mode;
    # the CLI dispatch never did, so every CLI/overnight run got zero
    # automatic session cleanup. See task_plan.md Task 5.
    try:
        import preflight as _preflight
        log.info("[CLI] Running preflight cleanup...")
        _preflight.ensure_run_once()
    except Exception as e:
        log.warning(f"[CLI] Preflight cleanup failed (non-fatal): {e}")

    # 2026-07-26: same one-time ADB reconnect attempt as scan-duplicates —
    # audit/reorganize/full/download all route device I/O through
    # mtp_transfer.py's cached ADB_AVAILABLE flag (see
    # _attempt_adb_reconnect_if_needed()'s docstring), so they're equally
    # exposed to a stale "ADB was down at import time" flag turning a fast
    # run into 20+ minutes of WPD/Shell.Application fallback.
    _attempt_adb_reconnect_if_needed()

    track_map: dict = {}
    liked_album_ids: set = set()
    sp_client = None

    if mode in ("full", "download"):
        try:
            import config as cfg
            if getattr(cfg, "OFFLINE_MODE", False):
                import spotify_local_data as sld
                track_map, _, liked_album_ids = sld.fetch_and_build_offline()
            else:
                import spotify_client as sc
                sp_client = sc.get_spotify_client()
                track_map, _, liked_album_ids = sc.fetch_and_build(sp_client)
            log.info(f"[CLI] Spotify fetch OK — {len(track_map)} tracks")
        except Exception as e:
            log.error(f"[CLI] Spotify fetch failed: {e}")
            return 1

    cancel_flag = [False]

    from spiral_orchestrator import SpiralOrchestrator
    orch = SpiralOrchestrator(
        mode=mode,
        log_callback=_log_cb,
        cancel_flag=cancel_flag,
        track_map=track_map,
        liked_album_ids=liked_album_ids,
        sp_client=sp_client,
        dry_run=dry_run,
    )

    t0 = time.time()
    try:
        stats = orch.run()
    except Exception as e:
        log.error(f"[CLI] Unhandled exception in orch.run(): {type(e).__name__}: {e}", exc_info=True)
        return 1

    elapsed = time.time() - t0
    canary_ok = stats.get("canary_passed", True)
    log.info(f"[CLI] Done — mode={mode} canary={'OK' if canary_ok else 'FAILED'} elapsed={elapsed:.0f}s")
    log.info(f"[CLI] Stats: {stats}")
    return 0 if canary_ok else 1


if __name__ == "__main__":
    sys.exit(main())
