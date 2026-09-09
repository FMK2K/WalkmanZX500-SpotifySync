"""
spiral_orchestrator.py — MK11 Spiral Pipeline Orchestrator

Replaces the four waterfall thread bodies (sync, download, audit, reorganize)
with a single parameterised spiral:

  Rev 0  Canary     — mode-specific transport + API probes; any hard failure
                       halts before touching a single library file.
  Rev 1  Low-risk   — proven paths, cache + 3 genre APIs, no AI, no graph.
  Rev 2  Medium-risk — deeper APIs + genre_graph inference (graph built from
                       Rev 1's confident results).
  Rev 3  High-risk  — AI genre with graph-coherence gate via
                       activate_spiral_rev3() context.

Stage activation per mode is driven by _STAGE_MATRIX.
Genre depth per revolution is driven by _GENRE_DEPTH.
PlayerMediaProvider DB is verified (via verify_db_update) after every
revolution that writes to the device.

Public API:
    SpiralOrchestrator(mode, log_callback, cancel_flag, **kwargs).run() -> dict
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Callable

import config

log = logging.getLogger(__name__)

# Which stages are active per mode.
#
# The "tool" stages (catalog_scoped, mislabel_fix, desplinter, deluxe_merge,
# fav_upgrade, favsync) each shell out to a standalone script via _run_tool()
# — see the 2026-09-06 tool set in
# memory/project_walkman_hires_player_playlists_favorites.md. They run once,
# in run() after the reorganize block, in list order.
_STAGE_MATRIX: dict[str, frozenset] = {
    "full":       frozenset({"dedup_hard", "dedup_soft", "completeness", "download",
                              "transfer", "tag", "audit", "reorganize",
                              "catalog_scoped", "duration_verify", "mislabel_fix",
                              "desplinter", "deluxe_merge", "fav_upgrade", "covers",
                              "year_backfill", "storage_rebalance", "album_normalize",
                              "favsync"}),
    # "covers" + "year_backfill" are on every device-touching mode (user
    # 2026-09-06: "embed the cover whenever we run anything via the GUI"). Both
    # tools are resumable and record already-tagged files so repeat runs are
    # cheap. "storage_rebalance" is heavier (breaks M3U8 lines until favsync
    # regenerates them) so it's on the reorg-class modes only.
    "download":   frozenset({"completeness", "download", "transfer", "tag",
                             "covers", "year_backfill"}),
    "audit":      frozenset({"tag", "audit", "covers", "year_backfill"}),
    "reorganize": frozenset({"dedup_soft", "reorganize", "audit", "covers",
                             "year_backfill", "storage_rebalance"}),
    # Full-library curation: fills + reorg + dedup + cover art + year + storage
    # balance + favorites/playlists sync, no Spotify download-planning cohort.
    "curate":     frozenset({"catalog_scoped", "duration_verify", "mislabel_fix",
                             "desplinter", "deluxe_merge", "dedup_soft", "fav_upgrade",
                             "covers", "year_backfill", "storage_rebalance",
                             "album_normalize", "favsync"}),
}
# duration_verify: header-only duration cross-check vs Deezer for EVERY
# (artist,album) group — catches wrong-track swaps + full-mix-as-single-track
# downloads that title matching misses. Resumable (state file), runs for
# hours; its output feeds the mislabel_fix re-download stage right after.

# Genre API waterfall depth unlocked per revolution.
# Full waterfall: override_dict → cache → Last.fm → TIDAL →
#                 Discogs → MusicBrainz → genre_graph → AI
_GENRE_DEPTH: dict[int, int] = {1: 4, 2: 7, 3: 8}


class SpiralOrchestrator:
    """
    Parameterised spiral pipeline for all four sync modes.

    Usage from main.py thread methods:

        orch = SpiralOrchestrator(
            mode="audit",
            log_callback=self._on_log,
            cancel_flag=self._cancel_flag,
        )
        stats = orch.run()
    """

    def __init__(
        self,
        mode: str,
        log_callback: Callable[[str, str], None] | None = None,
        cancel_flag:  list[bool] | None = None,
        track_map:    dict | None = None,
        liked_album_ids: set | None = None,
        sp_client=None,
        dry_run:      bool = False,
    ) -> None:
        if mode not in _STAGE_MATRIX:
            raise ValueError(f"Unknown spiral mode {mode!r}; expected one of {sorted(_STAGE_MATRIX)}")
        self.mode        = mode
        self._log        = log_callback or (lambda m, lv="INFO": log.info(m))
        self._cancel     = cancel_flag or [False]
        self._track_map  = track_map or {}
        self._liked_ids  = liked_album_ids or set()
        self._sp         = sp_client
        # NOTE: dry_run guarantees zero real writes for `reorganize`
        # (reorganize_device's execute=False path is fully no-op, verified) and
        # for `download` (2026-07-18: the stage is skipped entirely rather than
        # calling downloader.run_downloads(), which has no dry_run awareness of
        # its own — see _run_revolution). `audit`: until 2026-08-20, dry_run was
        # passed through (auditor.dry_run) but only gated one narrow
        # cross-format-delete path — confirmed live that `audit --dry-run` made
        # 100+ real tag/reorg writes to the device. Now also gates the three
        # real-write sites inside LibraryAuditor._audit_one_device_file()
        # (push, canonical move+delete, rename, corrupt-file delete) — a real
        # `audit --dry-run` should make zero device writes as of this fix.
        self._dry_run    = bool(dry_run)
        self._cp_ok      = False   # set True by canary if ContentProvider is queryable
        self._graph      = None    # GenreGraph; built after Rev 1

        # Effective stage set for this run. 2026-09-03: "DOWNLOAD ONLY"
        # (mode="download") drops the full-library "tag"/audit sweep unless
        # AUDIT_IN_DOWNLOAD_MODE is on — see _run_revolution's note. Use
        # self._stages everywhere instead of _STAGE_MATRIX[self.mode] so the
        # graph-build and reorganize gates agree with the revolution.
        self._stages = set(_STAGE_MATRIX[mode])
        if (mode == "download" and "tag" in self._stages
                and not getattr(config, "AUDIT_IN_DOWNLOAD_MODE", False)):
            self._stages.discard("tag")
            self._log(
                "[Spiral] download mode: skipping the full-library audit/tag "
                "stage (AUDIT_IN_DOWNLOAD_MODE=False). New tracks are tagged "
                "inline; run START SYNC or AUDIT ONLY for genre/cover/ID3-v2.3 "
                "enrichment.", "INFO",
            )

    # ── public entry point ────────────────────────────────────────────────────

    def run(self) -> dict:
        self._log(f"[Spiral] Mode={self.mode!r} pipeline starting", "INFO")
        stats: dict = {"mode": self.mode, "revolutions": {}}

        # ── Rev 0: canary ────────────────────────────────────────────────────
        self._log("[Spiral] Rev 0: canary probes", "INFO")
        ok, canary_detail = self._canary()
        stats["canary"] = canary_detail
        stats["canary_passed"] = ok   # fatal-only boolean for UI summary
        if not ok:
            self._log(
                f"[Spiral] Canary FAILED — aborting before touching library. "
                f"Detail: {canary_detail}", "ERROR",
            )
            return stats
        self._log("[Spiral] Canary passed — classifying cohorts", "INFO")

        # 2026-09-03 — internal storage is a write target again
        # (STORAGE_BALANCED_PLACEMENT) but the peer's rooting fastboot-unlock
        # attempts wipe it. Warn loudly if the last-run manifest shows tracks
        # on internal that are now gone.
        try:
            self._internal_manifest("check", stats)
        except Exception as e:
            self._log(f"[Spiral] internal-manifest check skipped: {e}", "DEBUG")

        # ── Cohort classification ─────────────────────────────────────────────
        from cohort_classifier import classify
        cohorts = classify(self.mode, self._track_map)
        stats["cohorts"] = {k: len(v) for k, v in cohorts.items()}
        self._log(
            f"[Spiral] Cohorts — low={len(cohorts.get('low', []))}, "
            f"medium={len(cohorts.get('medium', []))}, "
            f"high={len(cohorts.get('high', []))}, "
            f"skip={len(cohorts.get('skip', []))}, "
            f"quarantine={len(cohorts.get('quarantine', []))}",
            "INFO",
        )

        # ── Rev 1–3 ──────────────────────────────────────────────────────────
        for rev, tier in ((1, "low"), (2, "medium"), (3, "high")):
            if self._cancel[0]:
                break
            cohort = cohorts.get(tier, [])
            if not cohort:
                self._log(f"[Spiral] Rev {rev} ({tier}) — empty cohort, skipping", "INFO")
            else:
                self._log(f"[Spiral] Rev {rev} ({tier}) — {len(cohort)} item(s)", "INFO")
                self._configure_revolution(rev)
                rev_stats = self._run_revolution(rev, tier, cohort)
                stats["revolutions"][rev] = rev_stats

                # After any revolution that writes to device: verify DB
                if self._stages & {"transfer", "reorganize", "audit"}:
                    rev_stats["db_verify"] = self._verify_db()

                if not self._gate(rev, rev_stats):
                    self._log(f"[Spiral] Rev {rev} gate failed — stopping spiral", "ERROR")
                    break

            # After Rev 1 — build genre graph from confident cache entries.
            # 2026-08-23: this used to live inside the `if cohort:` block above,
            # so an empty Rev 1 ("low" tier) cohort — the common case, most
            # libraries don't have much already-basically-fine work — skipped
            # graph construction for the ENTIRE run via the old `continue`.
            # _build_graph() only needs self._track_map (already fetched before
            # this loop starts) and the persistent genre cache, NOT Rev 1's own
            # cohort, so there was never a real data dependency here — confirmed
            # live: tonight's real audit run logged "graph=off" every revolution
            # because low=0. Moved outside the cohort branch so it always runs.
            if rev == 1 and self._stages & {"tag", "audit"}:
                self._build_graph()

        # ── Tool stages (2026-09-06) — each shells out to a standalone script.
        # Downloads first (they add files), then folder reorg, then dedup,
        # then favorites/playlists reconciliation.
        if "catalog_scoped" in self._stages and not self._cancel[0]:
            # refresh the completeness data catalog_fill_scoped reads, then fill.
            # --top=60 matches the manual runs; --max-queue 1500 keeps a GUI
            # button press from kicking off a multi-thousand-track download of
            # every deep-catalog album a saved artist has — partials + the
            # highest-intent absents come first in the queue order.
            self._run_tool("Artist-completeness report", "cli_runner.py",
                           extra=["artist-completeness-report", "--top=60"], exec_flag=None)
            self._run_tool("Filter completeness -> actionable", "artist_completeness_filter.py",
                           exec_flag=None)
            stats["catalog_scoped"] = self._run_tool(
                "Catalog-fill (partial + high-intent absent albums)", "catalog_fill_scoped.py",
                extra=["--max-queue", "1500"], exec_flag=None, dry_flag="--dry-run")
        # duration_verify — header-only duration cross-check vs Deezer for every
        # (artist,album) group; --write-pending emits mislabel_redownload_pending
        # .json that the mislabel_fix stage below consumes. The sweep is
        # resumable and slow (hours) — run it before mislabel_fix so the
        # re-download stage acts on fresh flags.
        if "duration_verify" in self._stages and not self._cancel[0]:
            # --fresh on a real run: catalog_scoped just added files, so the
            # per-group done-flags from a prior sweep are stale. On a dry run
            # skip the (hours-long, device-heavy) sweep and just re-categorize
            # whatever report already exists.
            if not self._dry_run:
                self._run_tool("Duration cross-check sweep", "duration_verify.py",
                               extra=["sweep", "--fresh"], exec_flag=None)
            stats["duration_verify"] = self._run_tool(
                "Categorize duration suspects -> pending", "duration_verify_categorize.py",
                extra=["--write-pending"], exec_flag=None)

        if "mislabel_fix" in self._stages and not self._cancel[0]:
            stats["mislabel_fix"] = self._run_tool(
                "Mislabel redownload", "mislabel_redownload.py",
                exec_flag=None, dry_flag="--dry-run")

        # Reorganize once, after all revolutions' downloads have landed —
        # it doesn't depend on genre-tier state, so running it per-revolution
        # only paid for 2 extra full-device scans+pulls with no benefit.
        if "reorganize" in self._stages and not self._cancel[0]:
            self._log("[Spiral] Reorganize", "INFO")
            try:
                from walkman_reorganize import reorganize_device
                stats["reorganize"] = reorganize_device(
                    execute=not self._dry_run,
                    log_callback=self._log,
                    cancelled_flag=self._cancel,
                )
            except Exception as e:
                self._log(f"[Spiral] Reorganize: {e}", "WARNING")
                stats["reorganize"] = {"error": str(e)}

        # De-splinter + deluxe-merge run right after the folder reorg so the
        # per-file dedup below sees one folder per album.
        if "desplinter" in self._stages and not self._cancel[0]:
            stats["desplinter"] = self._run_tool(
                "De-splinter album folders", "walkman_desplinter.py")
        if "deluxe_merge" in self._stages and not self._cancel[0]:
            stats["deluxe_merge"] = self._run_tool(
                "Merge deluxe/base folders + renumber", "walkman_deluxe_merge.py")

        # Metadata-completeness dedup (Pass 3) — once, after everything else.
        # 2026-08-22: Pass 3 (_pass_metadata_completeness — the only dedup
        # pass that can match same-format files with different filenames,
        # e.g. a canonical-renamed copy + the original plain-named copy) was
        # never given a score_lookup by any automatic caller, so it silently
        # no-op'd everywhere it was "run" (see the per-revolution dedup_soft
        # calls above, Pass 1+2 only). Deliberately NOT folded into that
        # per-revolution pre-pass: Pass 3 pulls each candidate over MTP/ADB
        # to score it, real device cost, and a pre-pass can't catch
        # duplicates audit/reorganize create during the same run anyway.
        # Runs once here instead, after reorganize has landed everything.
        if "dedup_soft" in self._stages and not self._cancel[0]:
            self._log("[Spiral] Metadata-completeness dedup pass (Pass 3)", "INFO")
            try:
                import walkman_dedup as _dd3
                _p3 = _dd3.prune_during_audit(
                    mode="report" if self._dry_run else None,
                    metadata_score_lookup=_dd3.device_metadata_score_lookup,
                )
                stats["dedup_metadata"] = _p3
                self._log(
                    f"[Spiral] Metadata dedup done — planned={_p3.get('planned',0)} "
                    f"moved={_p3.get('deleted',0)} failed={_p3.get('failed',0)} "
                    f"mode={_p3.get('mode','?')}",
                    "INFO",
                )
            except Exception as e:
                self._log(f"[Spiral] Metadata dedup: {e}", "WARNING")
                stats["dedup_metadata"] = {"error": str(e)}

        # fav_upgrade — clear duplicate pairs sitting in favorited/playlisted
        # albums (plain dedup skips those). Needs a fresh .dedup_plan.json,
        # so it runs its own scan-duplicates first.
        if "fav_upgrade" in self._stages and not self._cancel[0]:
            self._run_tool("Dedup scan (for fav-upgrade)", "cli_runner.py",
                           extra=["scan-duplicates"], exec_flag=None)
            stats["fav_upgrade"] = self._run_tool(
                "Upgrade favorited/playlisted dupes", "walkman_fav_upgrade.py")

        # covers / year — embed the album art and release year on files that
        # have none, in place (same path -> DB state undisturbed). Resumable;
        # long runs pick up where they left off.
        if "covers" in self._stages and not self._cancel[0]:
            stats["covers"] = self._run_tool(
                "Fill missing album covers", "walkman_cover_fill.py",
                exec_flag=None, dry_flag="--dry-run")
        if "year_backfill" in self._stages and not self._cancel[0]:
            stats["year_backfill"] = self._run_tool(
                "Backfill missing release-year tags", "walkman_year_backfill.py",
                exec_flag=None, dry_flag="--dry-run")

        # storage_rebalance — move whole album folders SD -> internal toward a
        # capacity-proportional music split. Breaks M3U8 lines for moved
        # playlisted tracks (favorites survive a move) -> MUST run before
        # favsync so the M3U8s get regenerated.
        if "storage_rebalance" in self._stages and not self._cancel[0]:
            stats["storage_rebalance"] = self._run_tool(
                "Rebalance storage (SD -> internal)", "walkman_storage_rebalance.py")

        # ── Post-rebalance cleanup (2026-09-06) ──────────────────────────────
        # storage_rebalance moves whole album folders SD -> internal; if an
        # album was split across both storages the `cp` merges into the
        # existing folder, which can leave same-song / mis-numbered files side
        # by side. album_normalize + a final dedup pass close that gap, and a
        # second covers pass re-embeds anything year_backfill's in-place rewrite
        # (or the earlier ADB-flap) left art-less. All three are resumable /
        # idempotent, so they're cheap when there's nothing to do.
        if "album_normalize" in self._stages and not self._cancel[0]:
            # second covers pass — cheap (state file skips done + art-present)
            if "covers" in self._stages:
                self._run_tool("Fill missing album covers (pass 2)",
                               "walkman_cover_fill.py",
                               exec_flag=None, dry_flag="--dry-run")
            # re-consolidate any folders the rebalance/earlier stages left split,
            # so album_normalize sees ONE folder per album (else a splinter's
            # tracks add on top of the normalized count in the player)
            if not self._cancel[0]:
                self._run_tool("De-splinter album folders (pre-normalize)",
                               "walkman_desplinter.py")
            if not self._cancel[0]:
                self._run_tool("Merge deluxe/base folders (pre-normalize)",
                               "walkman_deluxe_merge.py")
            # --exhaustive (implies --fix-tags): check EVERY album folder's
            # track-number TAGs against Deezer, not just folders with a local
            # filename dup-signal — a clean 1..N filename set can still hide
            # scrambled TRCK tags (Tyler IGOR), and the Sony player orders by
            # the tag. Probe-gated (cheap 2-file check) so clean folders cost
            # ~nothing; resumable via album_normalize_tagfix_state.json.
            stats["album_normalize"] = self._run_tool(
                "Normalize album folders (dedup + renumber + fix track-number tags)",
                "walkman_album_normalize.py", extra=["--exhaustive"])
            if not self._cancel[0]:
                self._log("[Spiral] Final metadata-completeness dedup pass", "INFO")
                try:
                    import walkman_dedup as _dd4
                    _p4 = _dd4.prune_during_audit(
                        mode="report" if self._dry_run else None,
                        metadata_score_lookup=_dd4.device_metadata_score_lookup,
                    )
                    stats["dedup_metadata_post"] = _p4
                    self._log(
                        f"[Spiral] Final metadata dedup done — "
                        f"planned={_p4.get('planned',0)} moved={_p4.get('deleted',0)} "
                        f"failed={_p4.get('failed',0)} mode={_p4.get('mode','?')}",
                        "INFO",
                    )
                except Exception as e:
                    self._log(f"[Spiral] Final metadata dedup: {e}", "WARNING")
                    stats["dedup_metadata_post"] = {"error": str(e)}

        # favsync — regenerate the favorites/playlists plan against the now-final
        # library and rewrite the M3U8 files. A device "Update database" rescan
        # still has to be triggered by hand (UI tap) to load them — logged loud.
        if "favsync" in self._stages and not self._cancel[0]:
            self._run_tool("Rebuild favorites/playlists plan", "cli_runner.py",
                           extra=["build-favorites-playlists-plan"], exec_flag=None)
            if not self._dry_run:
                self._run_tool("Import playlists (M3U8)", "walkman_playlist_import.py",
                               exec_flag=None)
                self._log(
                    "[Spiral] favsync: playlist M3U8s written. TRIGGER A DEVICE "
                    "'Update database' RESCAN (Library top -> refresh -> YES) to load "
                    "them, then re-star deleted favorites with "
                    "`WALKMAN_FAVPLAYLIST_PLAN=<queue>.json python "
                    "walkman_favplaylist_apply.py favorites`.", "WARNING")

        # Refresh the internal-storage manifest so the next run can detect a
        # wipe (see the "check" call after canary).
        if not self._dry_run:
            try:
                self._internal_manifest("snapshot", stats)
            except Exception as e:
                self._log(f"[Spiral] internal-manifest snapshot skipped: {e}", "DEBUG")

        return stats

    # ── canary ────────────────────────────────────────────────────────────────

    def _canary(self) -> tuple[bool, dict]:
        detail: dict = {}
        mode = self.mode

        # ADB probe — all modes that touch device files
        if mode in ("full", "audit", "reorganize", "curate"):
            detail["adb"] = self._probe_adb()
            if not detail["adb"]["ok"]:
                return False, detail

        # MTP + Shell.Application probe — modes that move files
        if mode in ("full", "reorganize"):
            detail["mtp"] = self._probe_mtp()
            if not detail["mtp"]["ok"]:
                return False, detail
            detail["shell_resolve"] = self._probe_shell_resolve()
            if not detail["shell_resolve"]["ok"]:
                self._log(
                    "[Spiral/Canary] Shell.Application degraded — "
                    "reorganize will use ADB-only path", "WARNING",
                )

        # ContentProvider probe — all device modes (non-fatal)
        if mode in ("full", "audit", "reorganize"):
            detail["content_provider"] = self._probe_content_provider()

        # Tag round-trip — modes that read/write tags.
        # Non-fatal when ADB is degraded (offline/unauthorized): the audit
        # uses MTP/COM for device file access, not ADB pull.
        if mode in ("full", "audit"):
            adb_degraded = detail.get("adb", {}).get("adb_degraded", False)
            detail["tag_roundtrip"] = self._probe_tag_roundtrip()
            if not detail["tag_roundtrip"]["ok"]:
                if adb_degraded:
                    self._log(
                        "[Spiral/Canary] Tag round-trip skipped (ADB offline) — "
                        "audit will use MTP path for device files.",
                        "WARNING",
                    )
                else:
                    return False, detail

        # Spotify API + download provider — download-side modes
        if mode in ("full", "download"):
            detail["spotify"] = self._probe_spotify()
            if not detail["spotify"]["ok"]:
                return False, detail
            detail["provider"] = self._probe_download_provider()
            if not detail["provider"]["ok"]:
                self._log("[Spiral/Canary] No download provider available", "WARNING")

        return True, detail

    def _probe_adb(self) -> dict:
        if not getattr(config, "USE_ADB", True):
            return {"ok": True, "detail": "ADB disabled (USE_ADB=False) — COM/MTP primary"}
        try:
            from adb_transfer import _adb_cmd
            out = _adb_cmd(["devices"], timeout=10, check=False) or ""
            n = sum(1 for ln in out.splitlines() if "\tdevice" in ln)
            if n > 0:
                return {"ok": True, "detail": f"{n} device(s)"}
            n_bad = sum(1 for ln in out.splitlines()
                        if "\toffline" in ln or "\tunauthorized" in ln)
            if n_bad > 0:
                self._log(
                    "[Spiral/Canary] ADB device is offline/unauthorized — "
                    "ADB ops unavailable, MTP path active. "
                    "To fix: replug USB or accept authorization on the Walkman.",
                    "WARNING",
                )
                return {"ok": True, "detail": "offline (MTP fallback active)", "adb_degraded": True}
            return {"ok": False, "detail": out.strip()[:80]}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _probe_mtp(self) -> dict:
        try:
            import mtp_transfer_com as _mtp
            device = getattr(config, "DEVICE_NAME", "WalkmanZX500")
            roots = _mtp.list_storage_roots(device)
            return {"ok": bool(roots), "detail": f"{len(roots)} storage(s)"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _probe_shell_resolve(self) -> dict:
        try:
            import tempfile
            from pathlib import Path
            from mtp_transfer_com import _shell
            probe = Path(tempfile.gettempdir()) / "spiral_canary_probe.txt"
            probe.write_text("x", encoding="utf-8")
            try:
                ns   = _shell().Namespace(str(probe.parent))
                item = ns.ParseName(probe.name) if ns else None
                return {"ok": item is not None,
                        "detail": "ParseName OK" if item else "ParseName returned None"}
            finally:
                probe.unlink(missing_ok=True)
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _probe_content_provider(self) -> dict:
        if not getattr(config, "USE_ADB", True):
            self._cp_ok = False
            return {"ok": False, "detail": "ADB disabled — ContentProvider unavailable (non-fatal)"}
        try:
            from adb_transfer import _adb_cmd
            uri = getattr(config, "WALKMAN_CONTENT_PROVIDER_URI",
                          "content://com.sony.nwz.walkmanplayer.provider/audio")
            out = _adb_cmd(
                ["shell", "content", "query", "--uri", uri,
                 "--projection", "_id", "--limit", "1"],
                timeout=10, check=False,
            ) or ""
            ok = "Row:" in out or "No result found" in out
            self._cp_ok = ok
            return {"ok": ok,
                    "detail": "ContentProvider exported" if ok else "not exported (fallback active)"}
        except Exception as e:
            self._cp_ok = False
            return {"ok": False, "detail": str(e)}

    def _probe_tag_roundtrip(self) -> dict:
        if not getattr(config, "USE_ADB", True):
            # COM/MTP mode: _probe_mtp already verified device access.
            # Tag reads go to local library files; no ADB pull needed.
            return {"ok": True, "detail": "ADB disabled — MTP storage probe covers device access"}
        try:
            import tempfile
            import shlex as _shlex
            from pathlib import Path
            from adb_transfer import _adb_cmd
            from walkman_tag_scan import adb_pull_head
            # 2026-09-03: the library now lives on the SD card and internal
            # storage is near-empty (post-wipe), so probing only
            # /storage/emulated/0/Music found zero .mp3 and aborted every
            # audit. Probe the real storage roots (SD first) and fall back to
            # a device-wide search.
            search_dirs = []
            for _sn, _sub in getattr(config, "STORAGE_ROOTS", []):
                try:
                    import mtp_transfer as _mtp
                    _root = _mtp._get_storage_root(config.DEVICE_NAME, _sn)
                    if _root:
                        search_dirs.append(f"{str(_root).rstrip('/')}/{_sub}".rstrip("/"))
                except Exception:
                    pass
            search_dirs += ["/storage/emulated/0/Music"]
            # de-dupe, keep order
            search_dirs = list(dict.fromkeys(d for d in search_dirs if d))

            lines: list[str] = []
            for d in search_dirs:
                # 2026-09-05: a bare `find -name *.mp3 -maxdepth 4` enumerates
                # every match before returning -- ~49s live on the SD card's
                # slow FUSE mount with ~4000 mp3s across thousands of folders,
                # so it always blew the 15s timeout and aborted every audit.
                # The probe only needs ONE mp3: pipe through `head -1` so find
                # stops at the first hit (near-instant), with a roomier
                # timeout as backstop for a cold directory cache.
                out = (_adb_cmd(
                    ["shell", "sh", "-c",
                     f"find {_shlex.quote(d)} -name '*.mp3' -maxdepth 4 2>/dev/null | head -1"],
                    timeout=40, check=False,
                ) or "").strip()
                lines = [ln for ln in out.splitlines() if ln.endswith(".mp3")]
                if lines:
                    break
            if not lines:
                # Last resort: glob any storage mount.
                out = (_adb_cmd(
                    ["shell", "sh", "-c",
                     "find /storage -maxdepth 5 -name '*.mp3' 2>/dev/null | head -20"],
                    timeout=20, check=False,
                ) or "").strip()
                lines = [ln for ln in out.splitlines() if ln.endswith(".mp3")]
            if not lines:
                return {"ok": False, "detail": "No .mp3 found for probe (SD + internal + /storage)"}

            # 2026-07-26: previously only tried lines[0] — a single genuinely
            # corrupt file on the device (confirmed live: an entire 16-track
            # album of 0-byte stubs) would permanently abort every audit run
            # before it even started, despite 1200+ other files being fine.
            # Try a handful of candidates, succeeding on the first real pull.
            last_detail = "no candidates tried"
            tried = 0
            for candidate in lines[:5]:
                tried += 1
                with tempfile.TemporaryDirectory() as tmp:
                    dest = Path(tmp) / "canary.mp3"
                    ok = adb_pull_head(candidate, dest)
                    sz = dest.stat().st_size if dest.exists() else 0
                    if ok and sz > 0:
                        return {"ok": True, "detail": f"pulled {sz}B from {candidate[-50:]}"}
                    last_detail = f"pulled {sz}B from {candidate[-50:]}"
            return {"ok": False,
                    "detail": f"tried {tried} file(s), all failed — last: {last_detail}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _probe_spotify(self) -> dict:
        try:
            if getattr(config, "OFFLINE_MODE", False):
                return {"ok": True, "detail": "offline mode — Spotify skipped"}
            if self._sp:
                self._sp.current_user()
                return {"ok": True, "detail": "Spotify auth cached"}
            import spotify_client as sc
            self._sp = sc.get_spotify_client()
            self._sp.current_user()
            return {"ok": True, "detail": "Spotify auth OK"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _probe_download_provider(self) -> dict:
        import subprocess
        try:
            r = subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=8)
            ver = r.stdout.decode().strip() if r.returncode == 0 else ""
            return {"ok": r.returncode == 0,
                    "detail": f"yt-dlp {ver}" if ver else "yt-dlp not found"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    # ── per-revolution configuration ──────────────────────────────────────────

    def _configure_revolution(self, rev: int) -> None:
        depth = _GENRE_DEPTH.get(rev, 8)
        config.GENRE_WATERFALL_DEPTH = depth
        config.AI_GENRE_ENABLED      = (rev == 3)
        # Graph available from Rev 2 onward — only after Rev 1 built it
        config.GENRE_GRAPH_ENABLED   = (rev >= 2 and self._graph is not None)
        if rev == 3 and self._graph is not None:
            config._SPIRAL_GENRE_GRAPH = self._graph
        self._log(
            f"[Spiral] Rev {rev} config: genre_depth={depth}, "
            f"graph={'on' if config.GENRE_GRAPH_ENABLED else 'off'}, "
            f"ai={'on (graph-gated)' if rev == 3 else 'off'}",
            "INFO",
        )

    # ── revolution execution ───────────────────────────────────────────────────

    def _run_revolution(self, rev: int, tier: str, cohort: list) -> dict:
        stages = self._stages
        stats: dict = {"tier": tier, "rev": rev, "size": len(cohort)}

        if self._cancel[0]:
            return stats

        # Hard dedup — full mode, Rev 1 only (clear garbage before downloading)
        # 2026-08-22: was called bare — prune_before_sync() gates its own
        # destructive behavior on config.DEDUP_MODE only, with zero awareness
        # of self._dry_run. DEDUP_MODE="active" (the current config value)
        # meant a `full --dry-run` spiral run would still perform real HARD
        # (non-recoverable) deletes at Rev 1. Same failure class as the two
        # 2026-08-20 audit.py incidents — force "report" mode here whenever
        # the spiral run itself is a dry-run, via the mode= override both
        # dedup entry points already accept for exactly this purpose.
        if "dedup_hard" in stages and rev == 1:
            self._log(f"[Spiral/Rev{rev}] Dedup hard pass", "INFO")
            try:
                import walkman_dedup as _dd
                stats["dedup_hard"] = _dd.prune_before_sync(
                    mode="report" if self._dry_run else None
                )
            except Exception as e:
                self._log(f"[Spiral/Rev{rev}] Dedup hard: {e}", "WARNING")

        # Soft dedup — before reorganize
        if "dedup_soft" in stages:
            self._log(f"[Spiral/Rev{rev}] Dedup soft pass", "INFO")
            try:
                import walkman_dedup as _dd
                _ds = _dd.prune_during_audit(
                    mode="report" if self._dry_run else None
                )
                stats["dedup_soft"] = _ds
                self._log(
                    f"[Spiral/Rev{rev}] Dedup soft done — "
                    f"planned={_ds.get('planned',0)}  "
                    f"moved={_ds.get('deleted',0)}  "
                    f"failed={_ds.get('failed',0)}  "
                    f"mode={_ds.get('mode','?')}",
                    "INFO"
                )
            except Exception as e:
                self._log(f"[Spiral/Rev{rev}] Dedup soft: {e}", "WARNING")

        # Album completeness — a/b/c rule (2026-09-02). Runs ONCE (Rev 1 only),
        # before the per-cohort download stage. Builds the missing-album track
        # list (a/b/c rules; online API or offline Spotify embed scrape), then
        # hands it straight to the downloader as a synthetic "__completeness_fill__"
        # playlist (same pattern as legacy walkman_sync Stage 0.7 — a direct
        # run_downloads call, NOT relying on cohort filtering). Also merges the
        # fill tracks into self._track_map so the later audit/tag/reorganize
        # stages see them.
        if ("completeness" in stages and rev == 1 and self._track_map
                and not self._cancel[0]):
            if self._dry_run:
                self._log(f"[Spiral/Rev{rev}] Album completeness — SKIPPED (dry-run)", "INFO")
                stats["completeness"] = {"skipped": True, "reason": "dry_run"}
            else:
                self._log(f"[Spiral/Rev{rev}] Album completeness (a/b/c) ...", "INFO")
                try:
                    import album_completeness as _ac
                    _fill = _ac.run_completeness_pass(
                        self._track_map,
                        self._liked_ids or set(),
                        sp_client=self._sp,
                        dev_lib=getattr(self, "_device_lib", None),
                    )
                    if _fill:
                        self._track_map.setdefault("__completeness_fill__", []).extend(_fill)
                        import downloader as _dl_c
                        _cstats = _dl_c.run_downloads(
                            track_map={"__completeness_fill__": _fill},
                            cancelled=self._cancel,
                            liked_album_ids=self._liked_ids,
                            sp_client=self._sp,
                            log_callback=self._log,
                        )
                        stats["completeness"] = {
                            "fill_tracks": len(_fill),
                            "downloaded": _cstats.get("downloaded", 0),
                            "failed": _cstats.get("failed", 0),
                        }
                        self._log(
                            f"[Spiral/Rev{rev}] Completeness: {len(_fill)} fill track(s) -> "
                            f"{_cstats.get('downloaded', 0)} new, {_cstats.get('failed', 0)} unfindable",
                            "INFO",
                        )
                    else:
                        stats["completeness"] = {"fill_tracks": 0}
                except Exception as e:
                    import traceback as _tb_c
                    self._log(f"[Spiral/Rev{rev}] Completeness: {e}\n{_tb_c.format_exc()}", "WARNING")
                    stats["completeness"] = {"error": str(e)}

        # Download — filtered to this revolution's cohort
        if "download" in stages and self._track_map:
            if self._dry_run:
                # 2026-07-18: downloader.run_downloads() has no dry_run parameter
                # and performs real downloads + real device transfers internally —
                # cli_runner.py's config.DOWNLOAD_MISSING=False override never
                # reached this call site (that flag is only checked inside
                # walkman_sync.WalkmanSyncAgent, a different orchestrator this CLI
                # pipeline doesn't use), so --dry-run full/download was silently
                # downloading for real. Matches the same skip-the-stage pattern
                # already used for reorganize (self._dry_run, line ~157) rather
                # than threading dry_run deep into downloader.py's internals.
                self._log(
                    f"[Spiral/Rev{rev}] Download ({tier} tier) — SKIPPED (dry-run): "
                    f"would download for {len(_filter_track_map(self._track_map, cohort))} "
                    f"track(s) in this revolution's cohort",
                    "INFO",
                )
                stats["download"] = {"skipped": True, "reason": "dry_run"}
            else:
                self._log(f"[Spiral/Rev{rev}] Download ({tier} tier)", "INFO")
                try:
                    import downloader as dl
                    filtered = _filter_track_map(self._track_map, cohort)
                    stats["download"] = dl.run_downloads(
                        track_map=filtered,
                        cancelled=self._cancel,
                        liked_album_ids=self._liked_ids,
                        sp_client=self._sp,
                        log_callback=self._log,
                    )
                except Exception as e:
                    self._log(f"[Spiral/Rev{rev}] Download: {e}", "WARNING")
                    stats["download"] = {"error": str(e)}

        # Reorganize has no dependency on genre-tier/revolution state, so it
        # runs once after all revolutions (see run()) instead of once per rev.

        # Audit / tag — Rev 3 uses graph-gated AI context
        if stages & {"tag", "audit"}:
            ai_note = " (AI graph-gated)" if rev == 3 else ""
            self._log(f"[Spiral/Rev{rev}] Audit/tag{ai_note}", "INFO")
            try:
                ctx = _ai_rev3_ctx() if rev == 3 else _noop_ctx()
                from audit import LibraryAuditor
                with ctx:
                    auditor = LibraryAuditor(log_callback=self._log)
                    auditor.dry_run = self._dry_run
                    stats["audit"] = auditor.run(cancelled_flag=self._cancel)
            except Exception as e:
                # 2026-08-22: log the full traceback, not just str(e) — this
                # handler was silently swallowing the actual origin of audit
                # errors, making them impossible to root-cause from the GUI
                # log alone (e.g. a live WinError 3 gave no clue which line
                # raised it).
                import traceback as _tb_audit
                self._log(f"[Spiral/Rev{rev}] Audit: {e}\n{_tb_audit.format_exc()}", "WARNING")
                stats["audit"] = {"error": str(e)}

            # Post-audit device scan: refresh MediaStore snapshot, cross-check
            # findings, then drive genre-cache normalization + album consensus.
            if not self._cancel[0]:
                try:
                    stats["device_scan"] = self._run_device_scan(rev)
                except Exception as e:
                    self._log(f"[Spiral/Rev{rev}] Device scan: {e}", "WARNING")

        return stats

    def _run_device_scan(self, rev: int) -> dict:
        """
        After each audit stage: refresh the MediaStore snapshot via ADB (fast,
        ~5s), run a cross-check against all caches, then normalize the genre
        cache and enforce album-level genre consensus from the live device data.
        Does NOT run walkman_tag_scan (slow) — uses existing tag snapshot if
        present, otherwise cross-check falls back to MediaStore values.
        """
        import json as _json
        result: dict = {}

        # 1. Refresh walkman_snapshot.json (ADB content-provider query, ~5s)
        rows: list = []  # populated below when USE_ADB=True; stays empty otherwise
        if not getattr(config, "USE_ADB", True):
            self._log("[Scan] MediaStore snapshot skipped (USE_ADB=False)", "DEBUG")
        else:
            try:
                import walkman_tree_gen as _wtg
                self._log("[Scan] Refreshing MediaStore snapshot...", "INFO")
                raw         = _wtg.query_mediastore()
                audio_genre = _wtg.query_genres()
                rows        = _wtg.parse_rows(raw, audio_genre)
                _wtg.OUT.write_text(
                    _json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                result["snapshot_tracks"] = len(rows)
                self._log(f"[Scan] {len(rows)} tracks in MediaStore", "INFO")
            except Exception as e:
                self._log(f"[Scan] MediaStore refresh failed: {e}", "WARNING")
                # ADB down — still run genre-cache normalization (no ADB needed),
                # but skip album-consensus which depends on a fresh snapshot.
                try:
                    from audit import _normalize_genre_cache
                    n_norm = _normalize_genre_cache()
                    if n_norm:
                        self._log(f"[Scan] Genre cache: {n_norm} normalized (ADB-less path)", "INFO")
                    result["genre_norm_fixes"] = n_norm
                except Exception as ne:
                    self._log(f"[Scan] Genre cache norm failed: {ne}", "WARNING")
                return result

        # 2. Cross-check (pure JSON — skip ADB playlist query for speed)
        try:
            import snapshot_cross_check as _scc
            tag_snap = _json.loads(_scc.TAG_SNAPSHOT.read_text(encoding="utf-8")) \
                       if _scc.TAG_SNAPSHOT.exists() else {}
            gc_raw   = _json.loads(_scc.GENRE_CACHE.read_text(encoding="utf-8")) \
                       if _scc.GENRE_CACHE.exists() else {}
            dupes      = _scc.check_duplicates(rows, tag_snap)
            mismatches = _scc.check_genre_mismatches(rows, gc_raw, tag_snap)
            hires      = _scc.check_hires_player_issues(rows, tag_snap)
            result.update({
                "duplicate_sets":   len(dupes),
                "genre_mismatches": len(mismatches),
                "id3v4_tracks":     len(hires["id3v4_blank_in_player"]),
                "no_art_tracks":    len(hires["no_embedded_art"]),
            })
            self._log(
                f"[Scan] Dupes:{len(dupes)}  Genre-drift:{len(mismatches)}"
                f"  ID3v2.4:{len(hires['id3v4_blank_in_player'])}"
                f"  No-art:{len(hires['no_embedded_art'])}",
                "INFO",
            )
        except Exception as e:
            self._log(f"[Scan] Cross-check failed: {e}", "WARNING")

        # 3. Genre cache normalization + album consensus (invalidates stale metakeys)
        try:
            from audit import _normalize_genre_cache, _enforce_album_genre_consensus
            n_norm      = _normalize_genre_cache()
            n_consensus = _enforce_album_genre_consensus()
            total = n_norm + n_consensus
            if total:
                self._log(
                    f"[Scan] Genre cache: {n_norm} normalized + {n_consensus} "
                    "album-consensus fixes — affected files re-tagged next audit",
                    "INFO",
                )
            result["genre_norm_fixes"]      = n_norm
            result["genre_consensus_fixes"] = n_consensus
        except Exception as e:
            self._log(f"[Scan] Genre cache fix failed: {e}", "WARNING")

        return result

    # ── post-revolution: genre graph ───────────────────────────────────────────

    def _build_graph(self) -> None:
        """Build GenreGraph from confident cache entries after Rev 1."""
        try:
            from genre_client import _get_genre_cache
            from genre_graph import build_graph
            cache = _get_genre_cache()
            # 2026-09-04: this source filter enumerated only OLD per-provider
            # tags. genre_client's actual cache-writer stamps almost every
            # entry source="phase1" (its combined Last.fm+Deezer pass), which
            # was never in this set — so the filter matched ZERO real cache
            # entries and this graph has been built empty on every run.
            # confidence>=0.7 already isolates real hits from failed-lookup
            # stubs; widen the source set to match what the cache actually
            # writes rather than trust it alone. Confirmed live: 4,599
            # confident phase1 entries vs. 100 failed (confidence=0) stubs.
            confident = [
                {"artist": k.split("|")[0], "genre": v.get("genre", "")}
                for k, v in cache.items()
                if isinstance(v, dict)
                   and v.get("confidence", 0) >= 0.7
                   and v.get("source") in ("lastfm", "tidal", "discogs",
                                           "musicbrainz", "override", "phase1")
                   and v.get("genre")
            ]
            if confident:
                # _configure_revolution(1) left GENRE_GRAPH_ENABLED False (graph
                # only turns on for rev>=2), which made build_graph()'s own
                # kill-switch silently return an empty graph here every time.
                # Force it on for this call only; _configure_revolution(2) sets
                # the real value right after based on self._graph.
                prev_enabled = config.GENRE_GRAPH_ENABLED
                config.GENRE_GRAPH_ENABLED = True
                try:
                    # Pass track_map too: confident-cache stubs are single-artist
                    # (no real collab edges possible), so without it the graph
                    # always fell back to the weak same-genre starvation proxy
                    # instead of real feature/playlist co-occurrence signal.
                    self._graph = build_graph(
                        track_map=self._track_map,
                        tracks=iter(confident),
                        _override_purity=0.4,
                    )
                finally:
                    config.GENRE_GRAPH_ENABLED = prev_enabled
                n = len(getattr(self._graph, "adjacency", {}))
                self._log(f"[Spiral] Genre graph built: {n} artist node(s)", "INFO")
            else:
                self._log("[Spiral] Genre graph: no confident cache entries — graph empty", "WARNING")
        except Exception as e:
            self._log(f"[Spiral] Genre graph build error (non-fatal): {e}", "WARNING")

    # ── post-revolution: PlayerMediaProvider verification ─────────────────────

    _INTERNAL_MUSIC_DIR = "/storage/emulated/0/Music"
    _INTERNAL_MANIFEST  = "internal_music_manifest.json"

    def _run_tool(self, label: str, script: str, *, extra=(),
                  exec_flag: "str | None" = "--execute",
                  dry_flag: "str | None" = None) -> dict:
        """
        Shell out to one of the standalone curation scripts and stream its
        stdout into the log pane. Honors self._dry_run (appends `exec_flag`
        only on a real run; appends `dry_flag` on a dry run if the script has
        one) and self._cancel (terminates the child).

        Returns {"returncode": int} (or {"error": str} / {"cancelled": True}).
        """
        import subprocess as _sp
        import sys as _sys
        # 2026-09-08 src/ reorg: the standalone scripts live in <repo>/src/
        # alongside this file — run them from there so a bare `script` name
        # resolves and the child's own `import config` works (Python puts the
        # script's dir on sys.path[0]).
        import os.path as _osp
        base = str(getattr(config, "SRC_DIR", _osp.dirname(_osp.abspath(__file__))))
        cmd = [_sys.executable, "-u", script, *extra]
        if self._dry_run:
            if dry_flag:
                cmd.append(dry_flag)
        elif exec_flag:
            cmd.append(exec_flag)
        self._log(f"[Spiral] {label}: {' '.join(cmd[2:])}", "INFO")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                   MSYS_NO_PATHCONV="1")
        try:
            proc = _sp.Popen(cmd, cwd=base, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                             text=True, encoding="utf-8", errors="replace", env=env)
        except Exception as e:
            self._log(f"[Spiral] {label}: launch failed — {e}", "WARNING")
            return {"error": str(e)}
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self._log(f"  {line}", "INFO")
                if self._cancel[0]:
                    proc.terminate()
                    self._log(f"[Spiral] {label}: cancelled", "WARNING")
                    return {"cancelled": True}
            proc.wait()
        except Exception as e:
            self._log(f"[Spiral] {label}: {e}", "WARNING")
            return {"error": str(e)}
        rc = proc.returncode
        self._log(f"[Spiral] {label}: exit {rc}", "INFO" if rc == 0 else "WARNING")
        return {"returncode": rc}

    def _internal_manifest(self, action: str, stats: dict) -> None:
        """
        action="check":   compare the last snapshot's file count against the
                          live internal count; loud WARNING if the library
                          looks wiped (rooting fastboot-unlock destroys
                          /storage/emulated/0). Non-fatal — informational.
        action="snapshot": rewrite the manifest from the live internal listing.

        Only meaningful while STORAGE_BALANCED_PLACEMENT routes writes to
        internal; skipped otherwise.
        """
        if not getattr(config, "STORAGE_BALANCED_PLACEMENT", False):
            return
        import json as _json, time as _time, pathlib as _pl
        from adb_transfer import _adb_cmd
        mpath = _pl.Path(config.BASE_DIR) / self._INTERNAL_MANIFEST

        _audio_ext = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma")

        def _live_files() -> list[str]:
            out = _adb_cmd(["shell", "find", self._INTERNAL_MUSIC_DIR,
                            "-type", "f"]) or ""
            return [l.strip() for l in out.splitlines()
                    if l.strip().startswith(self._INTERNAL_MUSIC_DIR)
                    and l.strip().lower().endswith(_audio_ext)]

        if action == "check":
            if not mpath.exists():
                return
            try:
                prev = _json.loads(mpath.read_text(encoding="utf-8"))
            except Exception:
                return
            prev_n = int(prev.get("count", 0))
            if prev_n < 50:
                return
            live = _live_files()
            live_n = len(live)
            stats["internal_manifest"] = {"prev": prev_n, "live": live_n,
                                          "snapshot_at": prev.get("at")}
            if live_n < max(25, prev_n // 4):
                self._log(
                    "════════════════════════════════════════════════════════\n"
                    f"[Spiral] ⚠ INTERNAL STORAGE MAY HAVE BEEN WIPED — last "
                    f"snapshot had {prev_n} track(s) on {self._INTERNAL_MUSIC_DIR}, "
                    f"only {live_n} present now.\n"
                    f"    Likely cause: a rooting fastboot-unlock attempt "
                    f"(peer session). SD-card content is unaffected.\n"
                    f"    Recovery: re-run START SYNC / DOWNLOAD ONLY — the "
                    f"device diff will re-fetch the missing tracks.\n"
                    "════════════════════════════════════════════════════════",
                    "ERROR",
                )
            return

        if action == "snapshot":
            live = _live_files()
            try:
                mpath.write_text(_json.dumps({
                    "at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "count": len(live),
                    "dir": self._INTERNAL_MUSIC_DIR,
                    "files": sorted(live),
                }, ensure_ascii=False, indent=1), encoding="utf-8")
                self._log(f"[Spiral] internal-storage manifest updated "
                          f"({len(live)} track(s) on internal)", "INFO")
            except Exception as e:
                self._log(f"[Spiral] could not write internal manifest: {e}", "DEBUG")

    def _verify_db(self) -> dict:
        """Trigger Walkman DB rescan, then verify via count delta or ContentProvider."""
        try:
            from walkman_app_refresh import refresh, verify_db_update
            self._log("[Spiral] Triggering Walkman DB rescan...", "INFO")
            if not refresh():
                # 2026-07-18: refresh() already logs the specific reason (ADB
                # unreachable, mode=off, etc.) at WARNING. Polling verify_db_update
                # for up to SPIRAL_DB_VERIFY_TIMEOUT seconds here would just waste
                # time confirming what we already know — nothing was triggered.
                self._log(
                    "[Spiral] DB verify: skipped — refresh did not run (see prior warning). "
                    "Newly synced content may not be visible in the Walkman app yet.",
                    "WARNING",
                )
                return {"verified": False, "reason": "refresh_not_triggered"}
            result = verify_db_update(
                timeout=getattr(config, "SPIRAL_DB_VERIFY_TIMEOUT", 90),
                use_content_provider=self._cp_ok,
            )
            lvl = "INFO" if result.get("verified") else "WARNING"
            delta = result.get("count_after", 0) - result.get("count_before", 0)
            self._log(
                f"[Spiral] DB verify: verified={result.get('verified')}, "
                f"delta={delta:+d}, elapsed={result.get('elapsed', 0):.1f}s",
                lvl,
            )
            return result
        except Exception as e:
            self._log(f"[Spiral] DB verify error (non-fatal): {e}", "WARNING")
            return {"verified": False, "error": str(e)}

    # ── gate ──────────────────────────────────────────────────────────────────

    def _gate(self, rev: int, stats: dict) -> bool:
        """Return False to halt spiral after this revolution."""
        dl    = stats.get("download", {})
        total = dl.get("downloaded", 0) + dl.get("failed", 0)
        if total > 0:
            rate = dl.get("downloaded", 0) / total
            thr  = getattr(config, "SPIRAL_GATE_TRANSFER_OK_MIN", 0.80)
            if rate < thr:
                self._log(
                    f"[Spiral/Gate] Rev {rev}: transfer {rate:.0%} < {thr:.0%} — halting",
                    "ERROR",
                )
                return False
        if "error" in stats.get("audit", {}):
            self._log(f"[Spiral/Gate] Rev {rev}: audit error — halting", "ERROR")
            return False
        return True


# ── module-level helpers ───────────────────────────────────────────────────────

def _filter_track_map(track_map: dict, cohort: list) -> dict:
    """Return track_map containing only tracks present in the cohort by Spotify ID."""
    ids = {
        item.get("id") if isinstance(item, dict) else getattr(item, "spotify_id", None)
        for item in cohort
    }
    ids.discard(None)
    if not ids:
        return track_map   # cohort keys don't match — pass full map
    return {
        pl: [t for t in tracks if t.get("id") in ids]
        for pl, tracks in track_map.items()
    }


@contextmanager
def _ai_rev3_ctx():
    """Activate the write barrier's Rev-3 AI path for the duration of this block."""
    try:
        from mk38_0_semantic_write_barrier import activate_spiral_rev3
        with activate_spiral_rev3():
            yield
    except Exception:
        yield   # barrier unavailable or hardened — proceed without AI writes


@contextmanager
def _noop_ctx():
    yield
