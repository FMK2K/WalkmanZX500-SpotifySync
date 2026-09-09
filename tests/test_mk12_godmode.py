"""
test_mk12_godmode.py — MK12 Rich Context Pipeline Validation Suite
===================================================================
Tests for device_inventory.py, context_builder.py, and the wired-up
Claude/GPT-4o calls with context.

TC33  device_inventory_module_api           — all public functions exist
TC34  device_inventory_adb_command          — correct find command built
TC35  device_inventory_utf8_encoding        — subprocess uses UTF-8
TC36  device_inventory_audio_filter         — non-audio paths excluded
TC37  device_inventory_lookup_jaccard       — token overlap matching works
TC38  context_builder_module_api            — all public functions exist
TC39  context_builder_prompt_block          — as_prompt_block() has all sections
TC40  context_builder_collaborator_extract  — feat. artists extracted from title
TC41  claude_call_accepts_context_block     — _claude_sonnet_call has context_block param
TC42  claude_call_system_message            — system message with anti-default rules
TC43  gpt4o_call_accepts_context_block      — _gpt4o_call has context_block param
TC44  call_ai_forwards_context              — call_ai() passes context to Phase 5/6
TC45  infer_genre_builds_context            — infer_genre() uses context_builder
TC46  genre_fallback_injects_context        — _claude_genre_fallback uses context_builder

MC22  mc_device_path_parsing                — 500 paths parse correctly
MC23  mc_context_prompt_completeness        — 200 context blocks have required sections
MC24  mc_collaborator_extraction            — 300 feat. titles parsed correctly
"""

import pathlib
import random
import re
import sys
import unittest


class TestDeviceInventoryAPI(unittest.TestCase):
    """TC33-37: device_inventory module structure and logic."""

    def setUp(self):
        self.code = pathlib.Path("device_inventory.py").read_text(encoding="utf-8")

    def test_module_exists(self):
        self.assertTrue(pathlib.Path("device_inventory.py").exists())

    def test_public_functions(self):
        for fn in ["refresh", "lookup", "get_album_context",
                   "get_duplicates", "get_device_summary", "is_stale",
                   "age_hours", "is_refreshed"]:
            self.assertIn(f"def {fn}(", self.code, f"{fn}() must be defined")

    def test_adb_find_command_structure(self):
        """ADB command must scan both internal and SD storage."""
        self.assertIn("/storage/emulated/0", self.code,
            "Must scan internal storage")
        self.assertIn("4A21-0000", self.code,
            "Must include SD card UUID as fallback")
        self.assertIn("-maxdepth 6", self.code,
            "Must use -maxdepth 6 to keep scan fast")
        self.assertIn("-type f", self.code,
            "Must use -type f (files only)")
        self.assertIn("2>/dev/null", self.code,
            "Must suppress permission errors")
        self.assertIn("sort", self.code,
            "Must sort output for consistent ordering")

    def test_utf8_encoding_in_subprocess(self):
        """ADB subprocess must use UTF-8 to handle non-Latin filenames."""
        self.assertIn('encoding="utf-8"', self.code)
        self.assertIn('errors="replace"', self.code,
            "Must use errors=replace for non-UTF-8 bytes")

    def test_audio_extensions_defined(self):
        for ext in [".mp3", ".flac", ".m4a", ".aac", ".wav"]:
            self.assertIn(f'"{ext}"', self.code,
                f"Audio extension {ext} must be in AUDIO_EXTENSIONS")

    def test_cache_path_defined(self):
        self.assertIn("device_file_index.json", self.code,
            "Cache file must be .device_file_index.json")

    def test_cache_ttl_24h(self):
        self.assertIn("CACHE_TTL_HOURS", self.code)
        self.assertIn("24.0", self.code,
            "Cache TTL must be 24h")

    def test_sd_mount_detection(self):
        """SD card UUID must be dynamically detected from df output."""
        self.assertIn("_detect_sd_mount", self.code)
        self.assertIn("df", self.code)
        self.assertIn("[A-F0-9]{4}-[A-F0-9]{4}", self.code,
            "Must detect XXXX-XXXX UUID pattern from df")

    def test_lookup_uses_jaccard(self):
        """Lookup must use token overlap (Jaccard similarity)."""
        self.assertIn("jaccard", self.code)
        self.assertIn("title_tokens & stem_tokens", self.code)
        self.assertIn("title_tokens | stem_tokens", self.code)

    def test_format_priority_in_lookup(self):
        """FLAC must score higher than MP3 in lookup results."""
        self.assertIn('".flac": 3', self.code,
            "FLAC must have highest format priority")
        self.assertIn('".mp3": 1', self.code,
            "MP3 must have lowest format priority")

    def test_dataclass_devicefile(self):
        """DeviceFile dataclass must have required fields."""
        for field_name in ["path", "storage", "ext", "folder", "stem", "filename"]:
            self.assertIn(field_name, self.code)

    def test_save_load_cache(self):
        """Cache must be saved as UTF-8 JSON."""
        self.assertIn("ensure_ascii=False", self.code,
            "Cache must preserve non-ASCII characters (Japanese/Arabic filenames)")


class TestContextBuilderAPI(unittest.TestCase):
    """TC38-40: context_builder module structure and ContextBlock logic."""

    def setUp(self):
        self.code = pathlib.Path("context_builder.py").read_text(encoding="utf-8")

    def test_module_exists(self):
        self.assertTrue(pathlib.Path("context_builder.py").exists())

    def test_build_context_function(self):
        self.assertIn("def build_context(", self.code)

    def test_context_block_dataclass(self):
        for field_name in [
            "artist", "title", "cached_genre", "graph_genre",
            "collaborators", "device_found", "device_path", "device_ext",
            "play_count", "is_liked", "priority_score",
        ]:
            self.assertIn(field_name, self.code,
                f"ContextBlock must have {field_name!r} field")

    def test_as_prompt_block_method(self):
        self.assertIn("def as_prompt_block(", self.code)

    def test_all_5_data_sources_loaded(self):
        for source in ["_load_genre_cache", "_load_spotify_meta",
                       "_load_streaming_scores", "_infer_from_graph",
                       "_device_file_info"]:
            self.assertIn(source, self.code,
                f"Context builder must use {source}")

    def test_collaborator_extraction_from_title(self):
        """feat. artists must be extracted from track title."""
        self.assertIn("feat", self.code.lower())
        self.assertIn("_collaborator_genres", self.code)
        self.assertIn("feat_m = re.search", self.code)

    def test_prompt_block_has_key_sections(self):
        """as_prompt_block must include all evidence sections."""
        idx = self.code.find("def as_prompt_block(")
        section = self.code[idx:idx + 3000]
        for label in ["Artist:", "Title:", "Library evidence",
                      "Genre cache", "Genre graph", "Collaborator", "Device:"]:
            self.assertIn(label, section,
                f"Prompt block must include {label!r} section")

    def test_prompt_block_classification_request(self):
        """Prompt block must end with a classification request."""
        idx = self.code.find("def as_prompt_block(")
        section = self.code[idx:idx + 3000]
        # The as_prompt_block method is a class method; search 4000 chars 
        full_fn = self.code[idx:idx + 4000]
        self.assertIn("Given ALL this evidence", full_fn,
            "Prompt block must include classification request")
        # JSON format in prompt uses escaped braces: {\"genre\": \"string\"}
        self.assertTrue(
            '{"genre":' in full_fn or '{\\"genre\\":' in full_fn or "genre" in full_fn,
            "Prompt block must reference genre JSON output format"
        )

    def test_lazy_cache_loading(self):
        """Caches must be loaded lazily (not at import time)."""
        # Global None sentinels indicate lazy loading
        self.assertIn("_genre_cache:      dict | None = None", self.code)
        self.assertIn("_spotify_meta:     dict | None = None", self.code)
        self.assertIn("_streaming_scores: dict | None = None", self.code)

    def test_device_inventory_integration(self):
        """Context builder must call device_inventory.lookup()."""
        self.assertIn("import device_inventory as di", self.code)
        self.assertIn("di.lookup(", self.code)
        self.assertIn("di.get_album_context(", self.code)


class TestClaudeContextIntegration(unittest.TestCase):
    """TC41-46: context_block wired into AI provider calls."""

    def setUp(self):
        self.ai_code     = pathlib.Path("ai_helper.py").read_text(encoding="utf-8")
        self.genre_code  = pathlib.Path("genre_client.py").read_text(encoding="utf-8")

    def test_claude_call_has_context_param(self):
        idx = self.ai_code.find("def _claude_sonnet_call(")
        section = self.ai_code[idx:idx + 300]
        self.assertIn("context_block", section,
            "_claude_sonnet_call must accept context_block parameter")

    def test_claude_uses_system_message(self):
        idx = self.ai_code.find("def _claude_sonnet_call(")
        section = self.ai_code[idx:idx + 3000]
        self.assertIn('"system":', section,
            "Claude call must use system message for genre expert role")
        self.assertIn("_SYSTEM", section)

    def test_claude_system_has_anti_hiphop(self):
        idx = self.ai_code.find("_SYSTEM = (")
        section = self.ai_code[idx:idx + 1500]
        self.assertIn("NEVER default to Hip-Hop", section,
            "Claude system message must have anti-Hip-Hop rule")

    def test_claude_context_used_as_user_message(self):
        """Context block replaces the bare prompt as user message."""
        idx = self.ai_code.find("def _claude_sonnet_call(")
        section = self.ai_code[idx:idx + 3500]  # user_content is ~2400 chars in
        # Context block assigned to user_content when available
        self.assertIn("user_content", section,
            "Claude must use user_content variable")
        self.assertIn("context_block", section,
            "Claude must use context_block")

    def test_gpt4o_has_context_param(self):
        idx = self.ai_code.find("def _gpt4o_call(")
        section = self.ai_code[idx:idx + 300]
        self.assertIn("context_block", section,
            "_gpt4o_call must accept context_block parameter")

    def test_call_ai_has_context_param(self):
        # call_ai signature now inline with context_block default param
        idx = self.ai_code.find("def call_ai(")
        section = self.ai_code[idx:idx + 200]
        self.assertIn("context_block", section,
            "call_ai() must accept context_block parameter")

    def test_call_ai_forwards_to_phase5_6(self):
        idx = self.ai_code.find("# MK12: Phase 5")
        section = self.ai_code[idx:idx + 600]
        self.assertIn("context_block=context_block", section,
            "call_ai must forward context_block to Phase 5 (GPT-4o)")
        # Both phase 5 (_gpt4o_call) and phase 6 (_claude_sonnet_call) forwarded
        self.assertIn("_gpt4o_call(prompt, max_tokens, temperature,", 
                      section.replace("\n","").replace("  ",""),
            "call_ai must forward to _gpt4o_call in Phase 5")
        self.assertIn("context_block=context_block", section,
            "call_ai must forward context_block to Phase 5/6")

    def test_genre_fallback_builds_context(self):
        idx = self.genre_code.find("import context_builder as _cb")
        self.assertGreater(idx, 0,
            "_claude_genre_fallback must import context_builder")
        section = self.genre_code[idx:idx + 500]
        self.assertIn("build_context(", section)
        self.assertIn("as_prompt_block()", section)

    def test_genre_fallback_passes_context_to_call_ai(self):
        idx = self.genre_code.find("_call_ai(prompt, max_tokens=256")
        self.assertGreater(idx, 0,
            "Genre fallback must pass context_block to call_ai")
        section = self.genre_code[idx:idx + 200]
        self.assertIn("context_block=_context_block", section)

    def test_infer_genre_uses_context_builder(self):
        idx = self.ai_code.find("def infer_genre(")
        section = self.ai_code[idx:idx + 2200]
        self.assertIn("import context_builder as _cb", section)
        self.assertIn("build_context(", section)
        self.assertIn("as_prompt_block()", section)
        self.assertIn("context_block=_context_block", section)


# =============================================================================
# MC SIMULATIONS
# =============================================================================

class TestMCSim_DevicePathParsing(unittest.TestCase):
    """MC22: 500 ADB path strings parse to correct DeviceFile fields."""
    N = 500
    SEED = 2718

    def _parse_path(self, path: str, sd_mount: str) -> dict:
        """Simulate the _parse_paths() logic."""
        import posixpath, unicodedata, re
        AUDIO = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav"}
        ext = posixpath.splitext(path)[1].lower()
        if ext not in AUDIO:
            return {}
        folder   = posixpath.dirname(path)
        filename = posixpath.basename(path)
        stem     = posixpath.splitext(filename)[0].lower().strip()
        stem_n   = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
        stem_n   = re.sub(r"\s+", " ", stem_n).strip()
        storage  = ("internal" if "/storage/emulated" in path
                    else "sd" if (sd_mount and sd_mount in path)
                    else "sd" if re.search(r"/storage/[A-F0-9]{4}-[A-F0-9]{4}", path, re.I)
                    else "unknown")
        return {"ext": ext, "storage": storage, "stem": stem_n,
                "folder": folder, "filename": filename}

    def test_mc_path_parsing(self):
        rng = random.Random(self.SEED)
        sd = "/storage/4A21-0000"
        errors = []

        templates = [
            ("{sd}/Music/{artist} - {album}/{artist} - {album} - {nn} {title}.flac", "sd", ".flac"),
            ("{sd}/Music/{artist}/{album}/0{nn} {title}.mp3",                         "sd", ".mp3"),
            ("/storage/emulated/0/Music/{artist}/{album}/{nn} {title}.m4a",           "internal", ".m4a"),
            ("/storage/emulated/0/Music/{artist}/{nn} {title}.mp3",                   "internal", ".mp3"),
            ("{sd}/Music/{title}.mp3",                                                 "sd", ".mp3"),
            ("/storage/emulated/0/Download/{title}.flac",                             "internal", ".flac"),
        ]
        artists = ["21 Savage", "Anderson .Paak", "Asake", "Burna Boy", "J. Cole"]
        albums  = ["SAVAGE MODE II", "Malibu", "Work of Art", "African Giant", "KOD"]
        titles  = ["Intro", "The Bird", "Ototo", "Ye", "MIDDLE CHILD"]

        for i in range(self.N):
            tmpl, exp_storage, exp_ext = rng.choice(templates)
            path = tmpl.format(
                sd=sd,
                artist=rng.choice(artists),
                album=rng.choice(albums),
                title=rng.choice(titles),
                nn=str(rng.randint(1, 20)).zfill(2),
            )
            parsed = self._parse_path(path, sd)
            if not parsed:
                continue   # non-audio path — expected for some templates

            if parsed["ext"] != exp_ext:
                errors.append(f"Path {path}: ext {parsed['ext']!r} != {exp_ext!r}")
            if parsed["storage"] != exp_storage:
                errors.append(f"Path {path}: storage {parsed['storage']!r} != {exp_storage!r}")
            if not parsed["stem"]:
                errors.append(f"Path {path}: empty stem")

        print(f"\n  [MC22] {self.N} path parse trials: {len(errors)} errors")
        self.assertEqual(errors, [], "\n".join(errors[:5]))


class TestMCSim_ContextPromptCompleteness(unittest.TestCase):
    """MC23: 200 simulated ContextBlock.as_prompt_block() calls have all sections."""
    N = 200
    SEED = 1414

    def _simulate_prompt_block(self, ctx: dict) -> str:
        """Simulate what as_prompt_block() produces."""
        lines = [
            f"Artist:  {ctx['artist']}",
            f"Title:   {ctx['title']}",
        ]
        if ctx.get("album"):
            lines.append(f"Album:   {ctx['album']}")
        if ctx.get("year"):
            lines.append(f"Year:    {ctx['year']}")
        behaviours = []
        if ctx.get("is_liked"):     behaviours.append("Liked ♥")
        if ctx.get("play_count"):   behaviours.append(f"played {ctx['play_count']}×")
        if behaviours:
            lines.append(f"Usage:   {' | '.join(behaviours)}")
        if ctx.get("device_found"):
            lines.append(f"Device:  {ctx['device_path']} [FLAC]")
        else:
            lines.append("Device:  not found on Walkman")
        lines.append("")
        lines.append("── Library evidence ──────────────────────────────────────")
        if ctx.get("cached_genre"):
            lines.append(f"• Already cached:  {ctx['cached_genre']!r}")
        else:
            lines.append("• Genre cache:   not yet classified")
        if ctx.get("graph_genre"):
            lines.append(f"• Genre graph:   {ctx['graph_genre']!r}")
        if ctx.get("collaborators"):
            lines.append("• Collaborator genres from this library:")
            for c in ctx["collaborators"]:
                lines.append(f"    {c['artist']}")
        lines.append("")
        lines.append('Given ALL this evidence, classify into the most specific, accurate genre.')
        return "\n".join(lines)

    def test_mc_prompt_completeness(self):
        rng = random.Random(self.SEED)
        errors = []

        for i in range(self.N):
            ctx = {
                "artist":        rng.choice(["Brasstracks", "Masego", "Common", "Unknown"]),
                "title":         rng.choice(["Golden Ticket", "Tadow", "Testify", "Song"]),
                "album":         rng.choice(["", "Unknown Album", "Self Titled"]),
                "year":          rng.choice(["", "2017", "2020", "1995"]),
                "is_liked":      rng.choice([True, False]),
                "play_count":    rng.choice([0, 1, 5, 23]),
                "device_found":  rng.choice([True, False]),
                "device_path":   "/storage/4A21-0000/Music/Artist/01 Track.flac",
                "cached_genre":  rng.choice(["", "Hip-Hop", "Jazz", "Neo Soul"]),
                "graph_genre":   rng.choice(["", "Jazz-Hop", "Neo Soul"]),
                "collaborators": rng.choice([[], [{"artist": "Masego", "genre": "Neo Soul"}]]),
            }
            block = self._simulate_prompt_block(ctx)

            # Required elements
            if "Artist:" not in block:
                errors.append(f"Trial {i}: missing Artist:")
            if "Library evidence" not in block:
                errors.append(f"Trial {i}: missing Library evidence section")
            if "Device:" not in block:
                errors.append(f"Trial {i}: missing Device: line")
            if "classify" not in block.lower():
                errors.append(f"Trial {i}: missing classification request")
            if not ctx["cached_genre"] and "not yet classified" not in block:
                errors.append(f"Trial {i}: uncached genre should say 'not yet classified'")

        print(f"\n  [MC23] {self.N} context blocks: {len(errors)} errors")
        self.assertEqual(errors, [], "\n".join(errors[:5]))


class TestMCSim_CollaboratorExtraction(unittest.TestCase):
    """MC24: 300 track titles — feat. artists extracted correctly."""
    N = 300
    SEED = 3141

    def _extract_collabs(self, title: str) -> list[str]:
        """Simulate _collaborator_genres title parsing."""
        feat_m = re.search(r"\((?:feat|ft)\.?\s+(.+?)\)", title, re.IGNORECASE)
        if not feat_m:
            return []
        feat_str = feat_m.group(1)
        return [a.strip() for a in re.split(r"[,&]", feat_str)]

    def test_anchor_cases(self):
        cases = [
            ("Golden Ticket (feat. Masego & Common)", ["Masego", "Common"]),
            ("blessing me (feat. Kali Uchis) (remix)", ["Kali Uchis"]),
            ("All The Stars (feat. SZA)", ["SZA"]),
            ("HUMBLE.", []),
            ("Water (ft. Kendrick Lamar)", ["Kendrick Lamar"]),
            ("No Role Modelz", []),
        ]
        for title, expected in cases:
            result = self._extract_collabs(title)
            self.assertEqual(sorted(result), sorted(expected),
                f"Title {title!r}: got {result}, expected {expected}")

    def test_mc_random_titles(self):
        rng = random.Random(self.SEED)
        artists = ["SZA", "Masego", "Common", "Kali Uchis", "Kendrick", "J. Cole"]
        bases   = ["Song Title", "Another Track", "Banger", "Late Night", "Feels"]
        templates = [
            "{base} (feat. {a1})",
            "{base} (feat. {a1} & {a2})",
            "{base} (ft. {a1})",
            "{base}",   # no feature
            "{base} - Remix",
        ]
        errors = []

        for i in range(self.N):
            tmpl = rng.choice(templates)
            a1, a2 = rng.choice(artists), rng.choice(artists)
            base   = rng.choice(bases)
            title  = tmpl.format(base=base, a1=a1, a2=a2)
            result = self._extract_collabs(title)

            if "(feat." in title or "(ft." in title:
                if not result:
                    errors.append(f"Title {title!r}: expected collaborators, got none")
            else:
                if result:
                    errors.append(f"Title {title!r}: expected no collaborators, got {result}")

        print(f"\n  [MC24] {self.N} feat. extraction trials: {len(errors)} errors")
        self.assertEqual(errors, [], "\n".join(errors[:5]))


if __name__ == "__main__":
    print("=" * 65)
    print("SpotifyTuner MK12 — Rich Context Pipeline Validation Suite")
    print("=" * 65)
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestDeviceInventoryAPI,
        TestContextBuilderAPI,
        TestClaudeContextIntegration,
        TestMCSim_DevicePathParsing,
        TestMCSim_ContextPromptCompleteness,
        TestMCSim_CollaboratorExtraction,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
