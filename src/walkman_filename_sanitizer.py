"""
walkman_filename_sanitizer.py  —  Filename Artifact Cleaner  (MK14)
====================================================================
Addresses CTO finding from live inventory of 14,269 device files:
  - 631 double-feat files: "(feat. X) (feat. X)" in filename
  - 26 YouTube artifacts: "(Official Audio)", "(Official Video)",
    "(Lyric Video)", "(Not Official Audio)", "[41]", etc.
  - 2 "[NN]" YouTube ID suffixes

This module provides two functions:
    scan_for_artifacts(audio_files)  → list[ArtifactFile]  (dry-run safe)
    rename_artifacts_on_device(artifacts, dry_run=True)   → RenameReport

Works via ADB rename (mv command) for both internal and SD storage.
Called from walkman_reorganize.py as a post-pass step.

CTO directive: "Ruthless String Sanitization (The Apple Music Approach) —
before a file is committed to Walkman storage, it must pass through a
sanitization pipeline that strips YouTube artifacts and moves feat. data
to ID3 metadata so the UI can read it natively."
"""
from __future__ import annotations

import logging
import posixpath
import re
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns for artifact detection
# ---------------------------------------------------------------------------

# YouTube-style garbage appended to titles
_YOUTUBE_ARTIFACTS = re.compile(
    r"\s*[\(\[]\s*"
    r"(?:"
    r"Official\s+(?:Music\s+)?(?:Audio|Video)|"
    r"Lyric\s+Video|"
    r"Lyrics?\s+Video|"
    r"Audio\s+Only|"
    r"Not\s+Official\s+Audio|"
    r"Fan\s+(?:Made|Lyric)|"
    r"HD\s+(?:Audio|Video)|"
    r"HQ\s+(?:Audio|Video)|"
    r"Full\s+(?:Audio|Song|Version)|"
    r"Visualizer|"
    r"Animated\s+Video|"
    r"\d{1,3}"          # [41], [1], [123] — YouTube numeric IDs
    r")"
    r"\s*[\)\]]",
    re.IGNORECASE,
)

# Double-feat: "(feat. X) (feat. X)" or "(feat. X) (feat. Y)"
# Keeps first occurrence, removes second
_DOUBLE_FEAT = re.compile(
    r"(\s*[\(\[]\s*(?:feat|ft|featuring)[^)\]]*[\)\]])"   # first feat group
    r"(\s*[\(\[]\s*(?:feat|ft|featuring)[^)\]]*[\)\]])+", # one or more additional
    re.IGNORECASE,
)

# Parenthetical "feat. X" embedded in title (to extract for metadata, not strip)
_EMBEDDED_FEAT = re.compile(
    r"\s*[\(\[]\s*(?:feat\.|ft\.|featuring)\s+([^\)\]]+)[\)\]]",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ArtifactFile:
    path: str           # full device path
    filename: str       # current basename (with ext)
    clean_filename: str # what it should be renamed to
    artifact_type: str  # "double_feat" | "youtube" | "both"
    storage: str        # "internal" | "sd"


@dataclass
class RenameReport:
    dry_run: bool
    files_scanned: int
    artifacts_found: int
    renamed_ok: int
    rename_failed: int
    skipped: int
    details: list = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{'[DRY RUN] ' if self.dry_run else ''}"
            f"Scanned {self.files_scanned} files, found {self.artifacts_found} artifacts, "
            f"renamed {self.renamed_ok}, failed {self.rename_failed}, "
            f"skipped {self.skipped}."
        )


# ---------------------------------------------------------------------------
# Sanitization logic
# ---------------------------------------------------------------------------

def _sanitize_stem(stem: str) -> tuple[str, str]:
    """
    Apply all artifact rules to a filename stem.
    Returns (clean_stem, artifact_type) where artifact_type is '' if unchanged.
    """
    original = stem
    artifact_types = []

    # 1. Remove double-feat (keep first occurrence only)
    cleaned = _DOUBLE_FEAT.sub(r"\1", stem)
    if cleaned != stem:
        artifact_types.append("double_feat")
        stem = cleaned

    # 2. Strip YouTube artifacts
    cleaned = _YOUTUBE_ARTIFACTS.sub("", stem).strip()
    if cleaned != stem:
        artifact_types.append("youtube")
        stem = cleaned

    # 3. Collapse multiple spaces
    stem = re.sub(r" {2,}", " ", stem).strip(" -_.")

    if stem != original:
        return stem, "+".join(artifact_types)
    return stem, ""


def scan_for_artifacts(audio_files: list[str]) -> list[ArtifactFile]:
    """
    Scan a list of device file paths and return those with filename artifacts.

    Parameters
    ----------
    audio_files : list of /storage/... paths (from device_inventory or ADB find)

    Returns
    -------
    list[ArtifactFile] — files that need renaming (empty = clean library)
    """
    results: list[ArtifactFile] = []
    for path in audio_files:
        filename = posixpath.basename(path)
        stem, ext = posixpath.splitext(filename)
        clean_stem, artifact_type = _sanitize_stem(stem)
        if artifact_type:
            clean_filename = clean_stem + ext
            storage = "sd" if "4A21" in path or (
                not path.startswith("/storage/emulated")
                and re.search(r"/storage/[A-F0-9]{4}-[A-F0-9]{4}", path, re.I)
            ) else "internal"
            results.append(ArtifactFile(
                path=path,
                filename=filename,
                clean_filename=clean_filename,
                artifact_type=artifact_type,
                storage=storage,
            ))
    log.info(
        f"[Sanitizer] Scanned {len(audio_files)} files, "
        f"found {len(results)} with artifacts "
        f"({sum(1 for r in results if 'double_feat' in r.artifact_type)} double-feat, "
        f"{sum(1 for r in results if 'youtube' in r.artifact_type)} YouTube)"
    )
    return results


def rename_artifacts_on_device(
    artifacts: list[ArtifactFile],
    dry_run: bool = True,
    adb_exe: str = "adb",
) -> RenameReport:
    """
    Rename artifact files on device via ADB mv.

    Parameters
    ----------
    artifacts : list from scan_for_artifacts()
    dry_run   : if True, log what would happen but don't rename
    adb_exe   : path to adb binary

    Returns RenameReport.
    """
    report = RenameReport(
        dry_run=dry_run,
        files_scanned=len(artifacts),
        artifacts_found=len(artifacts),
        renamed_ok=0, rename_failed=0, skipped=0,
    )

    for af in artifacts:
        parent = posixpath.dirname(af.path)
        new_path = posixpath.join(parent, af.clean_filename)

        if af.filename == af.clean_filename:
            report.skipped += 1
            continue

        log.info(
            f"  [Sanitizer{'/ DRY RUN' if dry_run else ''}] "
            f"{af.artifact_type}: "
            f"{af.filename!r} → {af.clean_filename!r}"
        )
        report.details.append({
            "old": af.path,
            "new": new_path,
            "type": af.artifact_type,
            "storage": af.storage,
        })

        if dry_run:
            report.renamed_ok += 1
            continue

        # Execute rename via ADB
        try:
            result = subprocess.run(
                [adb_exe, "shell", f"mv '{af.path}' '{new_path}'"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                report.renamed_ok += 1
            else:
                log.warning(
                    f"  [Sanitizer] mv failed for {af.filename!r}: "
                    f"{result.stderr.strip()[:100]}"
                )
                report.rename_failed += 1
        except Exception as e:
            log.warning(f"  [Sanitizer] ADB rename error: {e}")
            report.rename_failed += 1

    log.info(f"[Sanitizer] {report.summary()}")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import argparse
    import pathlib

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Scan and fix filename artifacts on Walkman device"
    )
    parser.add_argument("--execute", action="store_true",
                        help="Actually rename files (default: dry run)")
    parser.add_argument("--inventory", type=pathlib.Path,
                        help="Path to zx500_all_files.txt (UTF-16-LE from ADB)")
    args = parser.parse_args()

    # Load file list
    if args.inventory and args.inventory.exists():
        raw = args.inventory.read_bytes()
        text = raw.decode("utf-16-le", errors="replace")
        all_files = [l.strip() for l in text.splitlines() if l.strip()]
    else:
        print("No inventory file provided — run ADB find first:")
        print('  adb shell "find /storage/emulated/0 /storage/4A21-0000 '
              '-maxdepth 6 -type f 2>/dev/null | sort" > zx500_all_files.txt')
        sys.exit(1)

    artifacts = scan_for_artifacts(all_files)
    report = rename_artifacts_on_device(
        artifacts,
        dry_run=not args.execute,
    )
    print(report.summary())
    if not args.execute and artifacts:
        print(f"\nFirst 10 renames (dry run):")
        for d in report.details[:10]:
            import os
            print(f"  {os.path.basename(d['old'])!r}")
            print(f"  → {os.path.basename(d['new'])!r}")
            print()
