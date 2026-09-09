"""
genre_client.py  —  Multi-API metadata enrichment
===================================================
Provides genre and release year for every track, using five external APIs
in a waterfall.  Spotify is intentionally NOT used for this —
see spotify_client.py for the reasoning.

Provider waterfall
------------------
Phase 1 — fast, parallel (GENRE_WORKERS = 50 threads):
    1. Last.fm    — crowdsourced track-level tags; best for niche/underground
    2. Deezer     — good mainstream coverage; free, no key required

Phase 1.5 — MusicFetch (sequential, misses only, requires MUSICFETCH_API_KEY):
    3. MusicFetch — resolves Spotify track IDs to canonical ISRC records;
                    best release-date coverage for mainstream; also returns
                    streaming links (Apple Music, YouTube, Tidal).
                    Only called when Phase 1 left year or genre unresolved.
                    Free tier available; set MUSICFETCH_API_KEY in .env.

Phase 2 — slow, sequential (rate-limited, only for remaining misses):
    4. MusicBrainz — community-voted; authoritative release dates; 1 req/s hard limit
    5. Discogs    — deepest style taxonomy; 60 req/min with token; 1 req/s conservative

Phase 3 — web search (Google) for missing genres (esp. African subgenres):
    6. Google     — scrape knowledge panel / featured snippet

Phase 4 — AI fallback (Groq/Gemini) for the hardest cases.
"""

import re
import os
import json
import time
import logging
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Canonical form for cache keys: ASCII-lower-no-articles-collapsed."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]", "", text.lower())
    text = re.sub(r"^(the |a |an )", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


def _cache_key(artist: str, title: str) -> str:
    return f"{_norm(artist)}|{_norm(title)}"


# ---------------------------------------------------------------------------
# Genre normalization map
# ---------------------------------------------------------------------------
# Maps raw API genre strings → preferred Walkman genre tag.
# Key: lowercase stripped form.  Value: canonical display tag.
# Goal: second-most-specific subgenre — specific enough to be meaningful,
# broad enough to group related tracks (e.g. "Amapiano" not "Piano Log").
#
# Covers the full genre landscape of this library:
#   Afrobeats, Amapiano, Bongo Flava, Kompa, Funk Carioca, J-Jazz,
#   Neo Soul, Detroit/Flint Rap, Trap, Psychedelic Rock, Classic Soul,
#   R&B, French Trap, Alternative R&B, J-Pop, J-Rock, City Pop, and more.

_GENRE_NORM: dict[str, str] = {
    # ── African Music (broad → specific) ──────────────────────────────────
    # MK8 Fix (PR-GenreGraph): "african music" and bare "african" are too broad
    # to collapse directly to "Afrobeats" — they include Bongo Flava, Afropop,
    # Afro House, Kwaito, etc.  Map them to nothing (let the lookup waterfall
    # find a specific tag) rather than blindly writing "Afrobeats".
    # afropop is a PAN-AFRICAN pop umbrella, not the same as Nigerian Afrobeats.
    # alte is the Nigerian alternative/indie scene, not mainstream Afrobeats.
    # afro r&b blends African rhythms with R&B — closer to Alt R&B than Afrobeats.
    "african music":            "Afropop",         # broad umbrella → use Afropop not Afrobeats
    "african":                  "Afropop",         # same — too vague for Afrobeats
    "afrobeat":                 "Afrobeats",       # singular Fela-era form → keep
    "afrobeats":                "Afrobeats",
    "afropop":                  "Afropop",         # pan-African pop ≠ Nigerian Afrobeats
    "afro pop":                 "Afropop",
    "afro-pop":                 "Afropop",
    "nigerian pop":             "Afrobeats",       # Nigeria-specific → Afrobeats correct
    "nigerian music":           "Afrobeats",
    "west african pop":         "Afrobeats",       # West African specifically → ok
    "afro fusion":              "Afropop",         # fusion = broad, not Afrobeats-specific
    "alte":                     "Alte",            # distinct Nigerian alternative/indie scene
    "afro r&b":                 "Alt R&B",         # African R&B blend → Alt R&B not Afrobeats

    # ── Amapiano (keep specific — distinct enough from Afrobeats) ──────────
    "amapiano":                 "Amapiano",
    "piano":                    "Amapiano",        # Last.fm sometimes returns bare "piano"
    "south african house":      "Amapiano",
    "afro house":               "Afro House",
    "gqom":                     "Gqom",
    "kwaito":                   "Kwaito",
    "south african pop":        "Afropop",         # SA pop ≠ Afrobeats (includes Amapiano etc.)
    "south african music":      "Amapiano",

    # ── East African / Bongo Flava ─────────────────────────────────────────
    # MK8 Fix (LOG_17_3): "east african pop" was incorrectly mapped to
    # "Afrobeats" (a West African / Nigerian genre).  East Africa has its
    # own distinct genres: Bongo Flava (Tanzania), Benga (Kenya), Singeli.
    # Use "Afropop" as the safe pan-African umbrella for undifferentiated
    # East African pop — never "Afrobeats" which implies Nigerian origin.
    "bongo flava":              "Bongo Flava",
    "bongo":                    "Bongo Flava",
    "tanzanian pop":            "Bongo Flava",
    "east african pop":         "Afropop",         # ← was "Afrobeats" — wrong region
    "east african music":       "Afropop",
    "kenyan pop":               "Afropop",         # Kenya-specific → Afropop umbrella
    "ugandan pop":              "Afropop",
    "singeli":                  "Singeli",
    "genge":                    "Genge",           # Kenyan hip-hop subgenre
    "kenyan rap":               "Genge",
    "gengetone":                "Gengetone",       # Kenyan club music

    # ── Haitian / Caribbean ────────────────────────────────────────────────
    "kompa":                    "Kompa",
    "compas":                   "Kompa",
    "konpa":                    "Kompa",
    "haitian kompa":            "Kompa",
    "haitian music":            "Kompa",
    "zouk":                     "Zouk",
    "reggaeton":                "Reggaeton",
    "latin":                    "Latin",
    "latin pop":                "Latin Pop",
    "latin trap":               "Latin Trap",
    "latin hip hop":            "Latin Hip-Hop",
    "dembow":                   "Dembow",

    # ── Brazilian ─────────────────────────────────────────────────────────
    "funk carioca":             "Funk Carioca",
    "baile funk":               "Funk Carioca",
    "funk brasileiro":          "Funk Carioca",
    "phonk":                    "Phonk",
    "brazilian funk":           "Funk Carioca",
    "pagode":                   "Pagode",
    "samba":                    "Samba",
    "bossa nova":               "Bossa Nova",
    "mpb":                      "MPB",

    # ── Japanese ──────────────────────────────────────────────────────────
    "j-jazz":                   "J-Jazz",
    "japanese jazz":            "J-Jazz",
    "japanese":                 "J-Pop",
    "j-pop":                    "J-Pop",
    "jpop":                     "J-Pop",
    "j pop":                    "J-Pop",
    "city pop":                 "City Pop",
    "japanese city pop":        "City Pop",
    "citypop":                  "City Pop",
    "shibuya-kei":              "City Pop",
    "kayokyoku":                "City Pop",
    "new music":                "City Pop",    # Japanese "New Music" era = proto-City Pop
    "j-rock":                   "J-Rock",
    "jrock":                    "J-Rock",
    "j-metal":                  "J-Rock",
    "visual kei":               "Visual Kei",
    "anime":                    "J-Pop",
    "anisong":                  "J-Pop",
    "j-r&b":                    "J-R&B",
    "japanese r&b":             "J-R&B",
    "j r&b":                    "J-R&B",
    "j-rap":                    "J-Rap",
    "japanese hip hop":         "J-Rap",
    "japanese hip-hop":         "J-Rap",
    "enka":                     "Enka",
    "idol pop":                 "J-Pop",

    # ── Korean ────────────────────────────────────────────────────────────
    "k-pop":                    "K-Pop",
    "kpop":                     "K-Pop",
    "k pop":                    "K-Pop",
    "k-r&b":                    "K-R&B",
    "korean r&b":               "K-R&B",
    "korean hip hop":           "K-Hip Hop",
    "k-hip hop":                "K-Hip Hop",
    "k-hiphop":                 "K-Hip Hop",

    # ── Chinese ───────────────────────────────────────────────────────────
    "c-pop":                    "C-Pop",
    "cpop":                     "C-Pop",
    "mandopop":                 "C-Pop",
    "cantopop":                 "C-Pop",
    "chinese pop":              "C-Pop",
    "taiwanese pop":            "C-Pop",

    # ── Soul / R&B — MK10 ISSUE-E: Wikipedia-sourced subgenre expansion ──
    # Pre-1975 Soul sub-genres (will get Classic Soul via era-tagging)
    "classic soul":             "Classic Soul",   # MK10: explicit mapping
    "northern soul":            "Classic Soul",   # UK mod-era soul (60s–70s)
    "northern":                 "Classic Soul",
    "motown":                   "Classic Soul",   # Motown Records sound
    "motown soul":              "Classic Soul",
    "philly soul":              "Classic Soul",   # Philadelphia International
    "philadelphia soul":        "Classic Soul",
    "chicago soul":             "Classic Soul",
    "detroit soul":             "Classic Soul",
    "southern soul":            "Classic Soul",   # Deep South / Muscle Shoals
    "deep soul":                "Classic Soul",
    "atlantic soul":            "Classic Soul",
    "stax":                     "Classic Soul",   # Stax Records sound
    "memphis soul":             "Classic Soul",
    "gospel soul":              "Classic Soul",
    "doo-wop":                  "Classic Soul",
    "doo wop":                  "Classic Soul",
    "blue-eyed soul":           "Classic Soul",   # White artists in soul tradition
    "blue eyed soul":           "Classic Soul",
    "retro soul":               "Classic Soul",   # Retroist artists (Amy Winehouse)
    "retro-soul":               "Classic Soul",
    # 1990s–era Soul
    "neo soul":                 "Neo Soul",
    "neo-soul":                 "Neo Soul",
    "new soul":                 "Neo Soul",
    # General Soul (era-tagging refines these at lookup time)
    "soul":                     "Soul",
    "soul music":               "Soul",
    # R&B
    "quiet storm":              "Classic R&B",    # 70s–80s smooth R&B radio format
    "new jack swing":           "Classic R&B",    # Late 80s / early 90s
    "new jack":                 "Classic R&B",
    "swingbeat":                "Classic R&B",    # UK name for new jack swing
    "swing beat":               "Classic R&B",
    "rhythm and blues":         "R&B",
    "r&b":                      "R&B",
    "rnb":                      "R&B",
    "contemporary r&b":         "R&B",
    "urban contemporary":       "R&B",
    "urban":                    "R&B",
    "trap soul":                "Trap Soul",      # Drake/PartyNextDoor sub-genre
    "trap r&b":                 "Trap Soul",
    "alternative r&b":          "Alt R&B",
    "indie r&b":                "Alt R&B",
    "quiet storm":              "Classic R&B",    # duplicate removed — first def (line 228) is canonical
    "new jack swing":           "Classic R&B",    # duplicate removed — first def (line 229) is canonical
    "funk":                     "Funk",
    "classic funk":             "Classic Funk",   # MK10: pre-1985 Parliament/JB era
    "p-funk":                   "Classic Funk",   # Parliament-Funkadelic
    "pfunk":                    "Classic Funk",
    "parliament":               "Classic Funk",
    "deep funk":                "Classic Funk",   # 60s–70s rare groove funk
    "funk carioca":             "Funk Carioca",   # duplicate map kept for safety
    "boogie":                   "Funk",           # late 70s / early 80s dance funk
    "go-go":                    "Funk",           # DC-area funk variant
    "electrofunk":              "Funk",           # Zapp, Roger, Cameo
    "electro funk":             "Funk",
    "disco":                    "Disco",
    "dance":                    "Dance",
    "dance pop":                "Dance Pop",

    # ── Alternative R&B / Indie R&B ────────────────────────────────────────
    "alternative r&b":          "Alt R&B",
    "alt r&b":                  "Alt R&B",
    "indie r&b":                "Alt R&B",
    "indie soul":               "Alt R&B",
    "art pop":                  "Art Pop",
    "indie pop":                "Indie Pop",
    "alternative pop":          "Indie Pop",
    "dream pop":                "Dream Pop",
    "bedroom pop":              "Bedroom Pop",
    "lo-fi":                    "Lo-Fi",
    "lo fi":                    "Lo-Fi",
    "chillwave":                "Chillwave",
    "synthpop":                 "Synth-Pop",
    "synth-pop":                "Synth-Pop",
    "electronic":               "Electronic",
    "electro pop":              "Electro Pop",

    # ── Hip-Hop / Rap (broad → subgenre) ──────────────────────────────────
    "hip hop":                  "Hip-Hop",
    "hip-hop":                  "Hip-Hop",
    "rap":                      "Hip-Hop",
    "hip hop/rap":              "Hip-Hop",
    "rap/hip hop":              "Hip-Hop",
    "hip-hop/rap":              "Hip-Hop",
    "conscious hip hop":        "Hip-Hop",
    "underground hip hop":      "Hip-Hop",
    "east coast hip hop":       "East Coast Rap",
    "west coast hip hop":       "West Coast Rap",
    "west coast rap":           "West Coast Rap",
    "southern hip hop":         "Southern Hip-Hop",
    "trap":                     "Trap",
    "trap music":               "Trap",
    "trap rap":                 "Trap",
    "melodic trap":             "Melodic Trap",
    "mumble rap":               "Trap",
    "atlanta hip hop":          "Trap",
    "dirty south":              "Southern Hip-Hop",
    "gangsta rap":              "Gangsta Rap",
    "hardcore hip hop":         "Hardcore Hip-Hop",
    "boom bap":                 "Boom Bap",
    "midwest hip hop":          "Midwest Hip-Hop",
    "detroit hip hop":          "Detroit Rap",
    "detroit rap":              "Detroit Rap",
    "flint rap":                "Detroit Rap",     # Flint is Detroit orbit
    "michigan rap":             "Detroit Rap",
    "drill":                    "Drill",
    "uk drill":                 "UK Drill",
    "chicago drill":            "Drill",
    "pop rap":                  "Pop Rap",
    "crunk":                    "Crunk",
    "chopped and screwed":      "Chopped & Screwed",

    # ── R&B-adjacent sub-eras ─────────────────────────────────────────────
    "2000s r&b":                "R&B",
    "early 2000s r&b":         "R&B",
    "90s r&b":                  "R&B",

    # ── Rock / Alternative ────────────────────────────────────────────────
    "rock":                     "Rock",
    "indie rock":               "Indie Rock",
    "alternative":              "Alternative Rock",
    "alternative rock":         "Alternative Rock",
    "psychedelic rock":         "Psychedelic Rock",
    "psychedelic pop":          "Psychedelic Rock",
    "neo-psychedelia":          "Psychedelic Rock",
    "soft rock":                "Soft Rock",
    "pop rock":                 "Pop Rock",

    # ── Pop (broad) ────────────────────────────────────────────────────────
    "pop":                      "Pop",
    "pop music":                "Pop",

    # ── French / French-Caribbean ─────────────────────────────────────────
    "french rap":               "French Rap",
    "french hip hop":           "French Rap",
    "afro trap":                "Afro Trap",
    "french r&b":               "French R&B",
    "variété française":        "French Pop",
    "french pop":               "French Pop",

    # ── Jazz ──────────────────────────────────────────────────────────────
    "jazz":                     "Jazz",
    "jazz rap":                 "Jazz Rap",
    "nu jazz":                  "Nu Jazz",
    "acid jazz":                "Acid Jazz",
    "jazz fusion":              "Jazz Fusion",
    "contemporary jazz":        "Jazz",
    "smooth jazz":              "Smooth Jazz",

    # ── Gospel / Religious ────────────────────────────────────────────────
    "gospel":                   "Gospel",
    "christian":                "Gospel",

    # ── Electronic / Dance ────────────────────────────────────────────────
    "house":                    "House",
    "deep house":               "Deep House",
    "afrotech":                 "Afro House",
    "techno":                   "Techno",
    "drum and bass":            "Drum and Bass",
    "dnb":                      "Drum and Bass",
    "dubstep":                  "Dubstep",
    "edm":                      "Electronic",
    "ambient":                  "Ambient",
    "downtempo":                "Downtempo",

    # ── Dominican / Latin Caribbean ──────────────────────────────────────
    "bachata":                  "Bachata",
    "merengue":                 "Merengue",
    "salsa":                    "Salsa",
    "cumbia":                   "Cumbia",
    "vallenato":                "Vallenato",
    "bolero":                   "Bolero",
    "tropical":                 "Tropical",

    # ── Emo Rap / SoundCloud Rap ───────────────────────────────────────────
    "emo rap":                  "Emo Rap",
    "soundcloud rap":           "Emo Rap",
    "pluggnb":                  "Emo Rap",
    "rage":                     "Trap",
    "hyperpop":                 "Hyperpop",
    "cloud rap":                "Cloud Rap",

    # ── Soul Rap / Sample-based Hip-Hop ───────────────────────────────────
    "soul rap":                 "Hip-Hop",
    "conscious rap":            "Hip-Hop",
    "alternative hip hop":      "Hip-Hop",
    "abstract hip hop":         "Hip-Hop",
    "progressive rap":          "Hip-Hop",
    "jazz hop":                 "Jazz Rap",
    "lo-fi hip hop":            "Lo-Fi Hip-Hop",
    "lofi hip hop":             "Lo-Fi Hip-Hop",

    # ── Chicago / New York specific ───────────────────────────────────────
    "chicago rap":              "Drill",
    "new york rap":             "East Coast Rap",
    "nyc rap":                  "East Coast Rap",
    "bronx hip hop":            "East Coast Rap",
    "brooklyn drill":           "Drill",
    "new york drill":           "Drill",
    "chicago hip hop":          "Drill",

    # ── MK11 ISSUE-D: missing genre categories from live run misclassifications ──
    # HBCU / Marching Band
    "marching band":            "Marching Band",
    "hbcu":                     "Marching Band",
    "drumline":                 "Marching Band",
    "college band":             "Marching Band",
    "band show":                "Marching Band",
    # Jersey Club
    "jersey club":              "Jersey Club",
    "jclub":                    "Jersey Club",
    "club music":               "Jersey Club",    # vague — prefer Jersey Club
    # Chopped & Screwed
    "chopped and screwed":      "Chopped & Screwed",
    "chopped & screwed":        "Chopped & Screwed",
    "chopnotslop":              "Chopped & Screwed",
    "chop not slop":            "Chopped & Screwed",
    "screwed up":               "Chopped & Screwed",
    # Lo-Fi
    "lo-fi hip-hop":            "Lo-Fi Hip-Hop",
    "lo fi hip hop":            "Lo-Fi Hip-Hop",
    "lofi hip hop":             "Lo-Fi Hip-Hop",
    "lofi":                     "Lo-Fi Hip-Hop",
    "lo-fi":                    "Lo-Fi Hip-Hop",
    "chill hop":                "Lo-Fi Hip-Hop",
    "chillhop":                 "Lo-Fi Hip-Hop",
    "study beats":              "Lo-Fi Hip-Hop",
    # World / Arabic
    "world music":              "World Music",
    "arabic music":             "Arabic Music",
    "arabic":                   "Arabic Music",
    "arab":                     "Arabic Music",
    "rai":                      "Rai",              # Algerian/North African
    "gnawa":                    "Gnawa",            # Moroccan
    "chaabi":                   "Chaabi",           # Moroccan/North African pop
    "afrobeats diaspora":       "Afropop",
    "congolese":                "Soukous",
    "soukous":                  "Soukous",
    "ndombolo":                 "Soukous",
    "kizomba":                  "Kizomba",
    "semba":                    "Kizomba",
    "zouk":                     "Zouk",
    "genge":                    "Genge",            # Kenyan urban pop
    # Anime
    "anime":                    "Anime Soundtrack",
    "anime ost":                "Anime Soundtrack",
    "anime soundtrack":         "Anime Soundtrack",
    # Phonk / Internet microgenres
    "phonk":                    "Phonk",
    "cloud rap":                "Cloud Rap",
    "vaporwave":                "Vaporwave",
    "future bass":              "Future Bass",
    "slowed reverb":            "Lo-Fi Hip-Hop",    # common TikTok variant
    "slowed and reverb":        "Lo-Fi Hip-Hop",
    # Quiet Storm / Classic Soul — MK10 extended ──────────────────────
    "adult contemporary":       "R&B",
    "soft soul":                "Soul",
    "smooth soul":              "Soul",
    "yacht rock":               "Classic R&B",    # late 70s / early 80s soft rock–soul
    "sunshine pop":             "Classic Soul",   # 60s sunny soul-pop
    "sophisti-pop":             "Classic R&B",    # 80s sophisticated pop-soul
    "sophisti pop":             "Classic R&B",
    "classic r&b":              "R&B",
    "old school r&b":           "R&B",
    "80s r&b":                  "R&B",
    "70s soul":                 "Soul",
    "60s soul":                 "Soul",
    "motown":                   "Soul",

    # ── Retro J-Pop / City Pop variants ───────────────────────────────────
    "shibuya-kei":              "Shibuya-Kei",
    "shibuya kei":              "Shibuya-Kei",
    "japanese city pop":        "City Pop",
    "japanese pop":             "J-Pop",
    "japanese r&b":             "J-R&B",
    "j-r&b":                    "J-R&B",
    "japanese hip hop":         "J-Rap",
    "j-rap":                    "J-Rap",
    "enka":                     "Enka",
    "kayokyoku":                "City Pop",       # retro J-pop era
    "idol pop":                 "J-Pop",
    "anison":                   "J-Pop",          # anime songs

    # ── Compound tags APIs sometimes return ───────────────────────────────
    "r&b/soul":                 "R&B",
    "r&b & soul":               "R&B",
    "rap & hip-hop":            "Hip-Hop",
    "hip hop & rap":            "Hip-Hop",
    "rap/hip-hop":              "Rap/Hip Hop",
    "dance & electronic":       "Electronic",
    "pop & top 40":             "Pop",
    "world & country":          "World Music",
    "world music":              "World Music",
    "afrobeats & dancehall":    "Afrobeats",
    "afrobeats/afropop":        "Afrobeats",

    # ── Dancehall / Reggae ────────────────────────────────────────────────
    "dancehall":                "Dancehall",
    "reggae":                   "Reggae",
    "reggae fusion":            "Reggae",
    "afroswing":                "Afroswing",      # UK genre

    # ── UK / Grime ────────────────────────────────────────────────────────
    "grime":                    "Grime",
    "uk rap":                   "UK Rap",
    "uk hip hop":               "UK Rap",
    "uk garage":                "UK Garage",
    "bashment":                 "Dancehall",

    # ── Classical / Orchestral crossover ─────────────────────────────────
    "classical":                "Classical",
    "classical crossover":      "Classical",
    "contemporary classical":   "Classical",
    "neoclassical":             "Classical",
    "baroque":                  "Classical",
    "orchestral":               "Classical",

    # ── Catch-alls that should stay broad ──────────────────────────────────
    "unknown":                  "",               # scrub "Unknown" → empty (triggers re-lookup)
    "unknown genre":            "",
    "?":                        "",
    "":                         "",
}


# MK17/MK18 FIX-F14: AI output taxonomy + year validation
# ROOT CAUSE: AI genres written to cache with only str().strip() — no sanity check.
# APPROACH: controlled taxonomy set + year range 1900-2030. Warn on unknown genres
# (not reject — taxonomy is not exhaustive) but always reject bad years.
_VALID_GENRE_TAXONOMY: set = {
    "Hip-Hop","Trap","Drill","UK Drill","Boom Bap","Cloud Rap","West Coast Rap",
    "Detroit Rap","Emo Rap","Conscious Hip-Hop","Abstract Hip-Hop","Jazz Rap",
    "Lo-Fi Hip-Hop","Phonk","Jersey Club","Chopped & Screwed","Grime",
    "R&B","Classic R&B","Alt R&B","Soul","Classic Soul","Neo Soul",
    "Contemporary Soul","Funk","Classic Funk","Gospel","Traditional Gospel",
    "Afrobeats","Afropop","Afroswing","Amapiano","Gqom","Afro House",
    "Bongo Flava","Genge","Kizomba","Soukous","Highlife","Kompa","Zouk",
    "Funk Carioca","Baile Funk","Jazz","Classic Jazz","Contemporary Jazz",
    "Jazz-Hop","Bossa Nova","MPB","Samba","Rock","Alternative Rock",
    "Indie Rock","Post-Punk","Emo","Metal","Punk","Electronic","House",
    "Techno","Ambient","Vaporwave","Hyperpop","Synthwave","Drum and Bass",
    "Dubstep","Arabic Music","Reggae","Dancehall","City Pop","Anime Soundtrack",
    "Marching Band","Classical","Pop","World Music",
    "Reggaeton","Indie Folk","Grunge",  # MK20 NG4
    # MK26 M1 (A3): New taxonomy entries (LOG_6_5_26)
    "K-Pop",                    # Korean pop — distinct genre, not an alias
    "UK Garage",                # British 2-step garage — distinct genre
    "UK Rap","Rai","French Pop","Afrohouse",  # MK22: from LOG_5_5_26
}
_YEAR_MIN, _YEAR_MAX = 1900, 2030


def _broad_genres() -> set:
    """
    MK22 B3.1 FIX: Returns the set of broad/parent genre names.
    ROOT CAUSE: _broad_genres() was called in walkman_sync.py genre-graph inference
    path but never defined anywhere, causing "name '_broad_genres' is not defined"
    NameError that crashed Stage 1 after GenreGraph built successfully.

    Broad genres are the high-level parents that genre-graph inference CAN override
    (because the graph may know better). Specific subgenres (Amapiano, Genge, etc.)
    are NEVER overridden by graph inference.
    """
    return {
        # Hip-Hop family (broad)
        "hip-hop", "hip hop", "rap", "hiphop",
        # R&B/Soul family (broad)
        "r&b", "rnb", "soul", "gospel",
        # Electronic (broad)
        "electronic", "dance", "house", "edm",
        # Rock (broad)
        "rock", "alternative",
        # Jazz (broad)
        "jazz", "blues",
        # Pop (broad)
        "pop",
        # African (broad — specific subgenres like Amapiano are NOT broad)
        "afrobeats", "world music", "world",
        # Latin (broad)
        "latin", "reggae",
        # Funk (broad)
        "funk",
        # Classical
        "classical",
    }

# MK20 NG4: Genre alias map — maps AI-returned variants to canonical taxonomy names.
# These aliases are based on observed AI output from production runs.
# Log evidence: 'Lo-Fi' appeared 30+ times per run alone.
_GENRE_ALIASES_VALIDATE: dict = {
    # Lo-Fi variants (30+ occurrences per run)
    "lo-fi":                "Lo-Fi Hip-Hop",
    "lo fi":                "Lo-Fi Hip-Hop",
    "lo-fi hip hop":        "Lo-Fi Hip-Hop",
    "lofi hip hop":         "Lo-Fi Hip-Hop",
    "lofi":                 "Lo-Fi Hip-Hop",
    # R&B variants
    "alternative r&b":      "Alt R&B",
    "alt r and b":          "Alt R&B",
    "electronic r&b":       "Alt R&B",
    "neo-soul":             "Neo Soul",
    "neo_soul":             "Neo Soul",
    "afrosoul":             "Soul",
    # African variants
    "afrobeat":             "Afrobeats",   # singular
    "afro beats":           "Afrobeats",
    "afro-beats":           "Afrobeats",
    "afrohouse":            "Afro House",
    "afro house music":     "Afro House",
    # Hip-Hop variants
    "hip hop":              "Hip-Hop",
    "hiphop":               "Hip-Hop",
    "east coast hip-hop":   "Hip-Hop",
    "west coast hip-hop":   "West Coast Rap",
    # Other observed misses from 2026-05-04 run
    "reggaeton":            "Reggaeton",
    "indie folk":           "Pop",
    "grunge":               "Alternative Rock",
    "dreamcore":            "Vaporwave",
    "bounce":               "Jersey Club",
    "drum & bass":          "Drum and Bass",
    "dnb":                  "Drum and Bass",
    "taiko drumming":       "World Music",
    "compton g-funk influenced house": "Funk",
    "east coast hip hop":   "Hip-Hop",
    # MK26 M1 (A3): new aliases from LOG_6_5_26 (7 aliases + 2 added to taxonomy above)
    "french house":                  "House",
    "indie electronic":              "Electronic",
    "classical crossover":           "Classical",
    "gangsta rap":                   "Hip-Hop",
    "r&b/soul":                      "R&B",
    "uk bass":                       "Electronic",
    "uk electronic/experimental":    "Electronic",
    "uk electronic":                 "Electronic",
    "indie r&b":                     "Alt R&B",     # common AI output variant
    # MK22 B1.2: new misses from LOG_5_5_26
    "uk rap":               "Hip-Hop",      # UK Rap → Hip-Hop family
    "rai":                  "World Music",  # Algerian Rai → World
    "french pop":           "Pop",          # French Pop → Pop
    "afrohouse":            "Afro House",   # already in map but ensure coverage
}


# MK22 B1.1 + FO-A: Genre contradiction guard + run-end telemetry
# FO-A counters give run-end metrics to validate guard calibration.
# CTO: "validates guard is neither too lax nor over-aggressive."
import threading as _gc_b1_lock
_GENRE_CONTRADICTION_LOCK = _gc_b1_lock.Lock()
_GENRE_CONTRADICTION_STATS: dict = {
    "rejected_count":      0,     # total contradictions rejected this session
    "phase1_floor_count":  0,     # total tracks that got Phase-1 as floor
    "rejected_families":   {},    # {phase1_family: count} for calibration
}


def get_contradiction_stats() -> dict:
    """MK22 FO-A: Return contradiction guard statistics for run-end telemetry."""
    import copy
    with _GENRE_CONTRADICTION_LOCK:
        stats = copy.deepcopy(_GENRE_CONTRADICTION_STATS)
    # Compute top rejected families
    top = sorted(stats["rejected_families"].items(), key=lambda x: x[1], reverse=True)
    stats["top_rejected_families"] = top[:5]
    return stats


def log_contradiction_stats() -> None:
    """MK22 FO-A: Emit contradiction guard stats at run end (INFO level)."""
    stats = get_contradiction_stats()
    if stats["rejected_count"] == 0 and stats["phase1_floor_count"] == 0:
        return
    log.info(
        f"[GenreContradiction/Stats] genre_contradiction_rejected_count="
        f"{stats['rejected_count']} "
        f"phase1_floor_applied_count={stats['phase1_floor_count']} "
        f"top_rejected_families={stats['top_rejected_families']}"
    )


# MK22 B1.1: Genre family groupings for contradiction guard
_GENRE_FAMILY_MAP: dict = {
    "drill": {"Drill", "UK Drill", "Brooklyn Drill", "Chicago Drill"},
    "jersey-club": {"Jersey Club"},
    "funk-carioca": {"Funk Carioca"},
    "uk-drill": {"UK Drill"},
    "grime": {"Grime"},
    "amapiano": {"Amapiano"},
    "afrobeats": {"Afrobeats", "Afropop", "Afroswing"},
    "lo-fi": {"Lo-Fi Hip-Hop"},
    "soul": {"Soul", "Neo Soul", "Classic Soul"},
    "jazz": {"Jazz", "Classic Jazz", "Contemporary Jazz"},
}


def _check_genre_contradiction(phase1_genre: str, ai_genre: str) -> bool:
    """
    MK22 B1.1 + FO-A: Returns True if AI genre contradicts Phase-1 signal.
    Increments telemetry counters for calibration.
    CTO: "if Phase-1 tags include high-confidence drill/uk-rap/jersey-club indicators,
    contradictory AI outputs (e.g., Afroswing/Lo-Fi) are rejected."
    """
    if not phase1_genre or not ai_genre:
        return False
    try:
        import config as _cfg_b11
        if not getattr(_cfg_b11, "GENRE_CONTRADICTION_GUARD", True):
            return False
        deny_map = getattr(_cfg_b11, "GENRE_CONTRADICTION_FAMILIES", {})
    except Exception:
        return False
    p1_lower = phase1_genre.lower().replace(" ", "-").replace("_", "-")
    ai_lower  = ai_genre.lower().replace(" ", "-").replace("_", "-")
    for family, denied_set in deny_map.items():
        if family in p1_lower:
            if ai_lower in denied_set or any(d in ai_lower for d in denied_set):
                # FO-A: increment telemetry counter
                with _GENRE_CONTRADICTION_LOCK:
                    _GENRE_CONTRADICTION_STATS["rejected_count"] += 1
                    _GENRE_CONTRADICTION_STATS["rejected_families"][family] = (
                        _GENRE_CONTRADICTION_STATS["rejected_families"].get(family, 0) + 1
                    )
                return True
    return False


def _validate_ai_genre(genre: str, year: str = "", phase1_genre: str = "") -> tuple:
    """MK18 FIX-F14 + MK20 NG4 + MK22 B1.1: validate AI genre; check aliases before taxonomy.

    MK20 NG4: Added _GENRE_ALIASES_VALIDATE map so common AI variants (e.g.
    'Lo-Fi', 'Alternative R&B', 'Afrobeat') map to canonical names WITHOUT
    generating a WARNING. Previously these 16 variants each produced a
    '[AI/Validate] Genre not in taxonomy; accepting' line — 30+ per run
    for 'Lo-Fi' alone. Now they silently alias to the correct canonical name.

    MK22 B1.1: Added phase1_genre parameter for contradiction guard.
    If Phase-1 had high-confidence result and AI contradicts it, reject AI output.
    """
    import re as _rev
    clean_genre = (genre or "").strip()
    if clean_genre:
        # Check alias map first (case-insensitive)
        _key = clean_genre.lower()
        if _key in _GENRE_ALIASES_VALIDATE:
            _canonical = _GENRE_ALIASES_VALIDATE[_key]
            log.debug(f"[AI/Validate] Genre alias: {clean_genre!r} → {_canonical!r}")
            clean_genre = _canonical
        else:
            canon = next((g for g in _VALID_GENRE_TAXONOMY
                          if g.lower() == clean_genre.lower()), None)
            if canon:
                clean_genre = canon
            else:
                # ─────────────────────────────────────────────────────────
                # MK39.1 C2 — STRUCTURAL FIX (Council 2026-05-10 §4 P0+):
                # "Plug the bottom of the funnel first."
                #
                # The historical behavior here was log.warning(...accepting)
                # which propagated AI's out-of-taxonomy guesses downstream.
                # That is exactly the contamination Phase I had to band-aid
                # with hard-blocks on "Abstract Hip-Hop". The structural
                # cure: under STRICT_AI_GENRE_VALIDATION (default True),
                # reject the guess and return blank — let the operator
                # path (.needs_review.json) handle it via Picard / MP3Tag
                # / quarantine_review --accept.
                # ─────────────────────────────────────────────────────────
                _strict = True   # default ON per Council mandate
                try:
                    import config as _cfg_strict
                    _strict = bool(getattr(_cfg_strict,
                                            "STRICT_AI_GENRE_VALIDATION", True))
                except ImportError:
                    pass

                if _strict:
                    # Telemetry — record the intercepted default-write attempt
                    try:
                        from terminal_state import (
                            DefaultWriteAttempt, record_default_write_attempt,
                        )
                        record_default_write_attempt(DefaultWriteAttempt(
                            field="genre",
                            source="ai_inference_out_of_taxonomy",
                            value=clean_genre,
                            rejected=True,
                        ))
                    except ImportError:
                        log.warning(
                            f"[AI/Validate/STRICT] Genre {clean_genre!r} not in "
                            f"taxonomy and not in alias map → REJECTED (returning "
                            f"blank; track will route to .needs_review.json)"
                        )
                    clean_genre = ""   # terminal-blank — operator path takes over
                else:
                    # Legacy permissive mode — operator opted out of strict
                    log.warning(
                        f"[AI/Validate] Genre {clean_genre!r} not in taxonomy; "
                        f"accepting (STRICT_AI_GENRE_VALIDATION=False)"
                    )

        # MK22 B1.1 + FO-A: Contradiction guard — reject AI if contradicts Phase-1
        if phase1_genre and _check_genre_contradiction(phase1_genre, clean_genre):
            log.debug(
                f"[AI/Validate] GENRE_CONTRADICTION_REJECTED — AI proposed {clean_genre!r} "
                f"but Phase-1 shows {phase1_genre!r}; keeping Phase-1 result"
            )
            clean_genre = phase1_genre   # Fall back to Phase-1 authoritative result
        # FO-A: count Phase-1 floor applications (when fail-open uses Phase-1)
        if phase1_genre and clean_genre == phase1_genre and not ai_genre == phase1_genre:
            with _GENRE_CONTRADICTION_LOCK:
                _GENRE_CONTRADICTION_STATS["phase1_floor_count"] += 1
    clean_year = ""
    if year:
        m = _rev.search(r"\b(\d{4})\b", str(year))
        if m:
            y = int(m.group(1))
            if _YEAR_MIN <= y <= _YEAR_MAX:
                clean_year = str(y)
            else:
                log.warning(f"[AI/Validate] Year {y} out of range [{_YEAR_MIN}-{_YEAR_MAX}]; discarding")
    return clean_genre, clean_year


# MK33 O2-T1 (A2): Per-provider token bucket for rate-aware reeval scheduling.
# CTO: "jitter merely smears provider spikes — it does not flatten them."
# When 4500 entries fail simultaneously, jitter spreads each entry's retry by ±20%
# but they ALL still hit the same provider. This bucket enforces:
#   - max N requests per provider in any rolling 60s window
# If bucket is empty, the reeval is deferred until the bucket has capacity.
# Lightweight: timestamp deque per provider, no async/threading needed.
import collections as _coll_o2
import time as _t_o2

_PROVIDER_RATE_BUDGETS = {
    # provider_key → (requests_per_minute, window_seconds)
    "lastfm":      (10, 60),    # Last.fm free tier: ~5 req/sec, we use 10/min for reeval
    "deezer":      (15, 60),    # Deezer: 50 req/5sec, generous; conservative 15/min
    "musicbrainz": ( 5, 60),    # MusicBrainz: 1 req/sec strict; conservative 5/min
    "discogs":     ( 5, 60),    # Discogs: 60 req/min auth, 25 unauth; conservative
    "gemini":      (15, 60),    # Gemini API: depends on tier
    "groq":        (30, 60),    # Groq: 30 req/min free tier
}
_PROVIDER_BUCKETS: dict = {k: _coll_o2.deque(maxlen=v[0]+1)
                          for k, v in _PROVIDER_RATE_BUDGETS.items()}
_PROVIDER_BUCKETS_LOCK = __import__("threading").Lock()


def _provider_budget_consume(provider: str) -> bool:
    """
    Try to consume one token from the provider's rate budget.
    Returns True if consumed (proceed); False if budget exhausted (defer).

    Bucket is a deque of recent request timestamps. Old timestamps (older than
    window_seconds) are evicted. If len < max_per_minute, the request is allowed
    and a new timestamp is appended.
    """
    p = provider.lower().strip()
    config = _PROVIDER_RATE_BUDGETS.get(p)
    if not config:
        return True  # unknown provider: don't gate
    max_per_window, window_s = config
    now = _t_o2.time()
    cutoff = now - window_s
    with _PROVIDER_BUCKETS_LOCK:
        bucket = _PROVIDER_BUCKETS.setdefault(
            p, _coll_o2.deque(maxlen=max_per_window+1)
        )
        # Evict old timestamps
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_per_window:
            return False  # bucket full; defer
        bucket.append(now)
        return True


def get_provider_budget_status() -> dict:
    """Return per-provider budget utilisation (used / max) for diagnostics."""
    now = _t_o2.time()
    out = {}
    with _PROVIDER_BUCKETS_LOCK:
        for p, (max_per_window, window_s) in _PROVIDER_RATE_BUDGETS.items():
            bucket = _PROVIDER_BUCKETS.get(p, _coll_o2.deque())
            cutoff = now - window_s
            recent = sum(1 for t in bucket if t >= cutoff)
            out[p] = {"used": recent, "max": max_per_window, "window_s": window_s}
    return out


def _resolve_genre_conflict(
    phase1_genre:  str,
    ai_genre:      str,
    phase1_source: str = "phase1",
    ai_source:     str = "groq",
    artist:        str = "",
    title:         str = "",
) -> str:
    """
    MK29 G1-T2 (A2): Explicit truth resolver for multi-source genre conflicts.
    CTO: "Phase1 wins by silent override — not generalized, no observability."

    Resolution rules (in order):
    1. If either source is empty → return the non-empty one
    2. If both agree (case-insensitive) → return it (no conflict)
    3. If external_metadata vs llm_inference conflict → external_metadata wins
       AND log GENRE_CONFLICT_RESOLVED for observability
    4. If both are same type → higher-confidence wins (phase1 > groq/gemini)

    Returns the resolved canonical genre string.
    """
    import logging as _log_g1t2
    _log = _log_g1t2.getLogger(__name__)

    if not phase1_genre:
        return ai_genre
    if not ai_genre:
        return phase1_genre
    if phase1_genre.lower().strip() == ai_genre.lower().strip():
        return phase1_genre  # agreement — no conflict

    # Source type classification
    _EXTERNAL_SOURCES = {"lastfm","last.fm","deezer","musicbrainz","discogs","phase1"}
    _p1_is_external = any(s in phase1_source.lower() for s in _EXTERNAL_SOURCES)
    _ai_is_external = any(s in ai_source.lower() for s in _EXTERNAL_SOURCES)

    if _p1_is_external and not _ai_is_external:
        # External metadata beats LLM inference — always
        _log.info(
            f"  [GenreResolver] GENRE_CONFLICT_RESOLVED "
            f"{artist!r} — {title!r}: "
            f"external_metadata({phase1_source}={phase1_genre!r}) beats "
            f"llm_inference({ai_source}={ai_genre!r}) → using {phase1_genre!r}"
        )
        return phase1_genre

    if _ai_is_external and not _p1_is_external:
        # Reverse (unusual but handle it)
        _log.info(
            f"  [GenreResolver] GENRE_CONFLICT_RESOLVED "
            f"{artist!r} — external_metadata({ai_source}) beats "
            f"heuristic({phase1_source}) → {ai_genre!r}"
        )
        return ai_genre

    # Both same type — default Phase1 wins (Phase1 pipeline is higher quality overall)
    _log.debug(
        f"  [GenreResolver] Same-type conflict "
        f"{phase1_genre!r} vs {ai_genre!r} → phase1 wins by priority"
    )
    return phase1_genre


# MK32 N3-T1 (A1): Genre family table for conflict severity scoring.
# CTO: "Neo Soul vs Alt R&B and Classical vs Drill look identical operationally —
# tiny disagreements and catastrophic disagreements should not be the same event."
# Lightweight family-based distance: same family = "taxonomic" (low severity);
# different family = "categorical" (high severity, queues for manual review).
# No ML deps — static table covers the genre space the user actually has.
GENRE_FAMILIES = {
    "rnb_soul":      {"r&b", "rnb", "neo soul", "alt r&b", "alternative r&b", "soul",
                       "contemporary soul", "classic soul", "rhythm and blues"},
    "hip_hop":       {"hip-hop", "hip hop", "rap", "rap/hip hop", "trap", "drill",
                       "boom bap", "conscious hip-hop", "gangsta rap", "southern hip-hop",
                       "abstract hip-hop", "cloud rap", "uk drill"},
    "electronic":    {"electronic", "house", "techno", "edm", "dance", "trance",
                       "ambient", "drum and bass", "dubstep", "synthwave", "vaporwave",
                       "uk garage"},
    "afro":          {"afrobeats", "afro house", "amapiano", "gqom", "afro pop",
                       "afro fusion", "kwaito"},
    "rock":          {"rock", "alternative rock", "indie rock", "punk", "punk rock",
                       "psychedelic rock", "classic rock", "post-rock", "metal"},
    "pop":           {"pop", "k-pop", "j-pop", "c-pop", "city pop", "synth-pop",
                       "indie pop", "dance pop"},
    "jazz":          {"jazz", "jazz fusion", "smooth jazz", "bebop", "free jazz",
                       "nu jazz", "acid jazz"},
    "classical":     {"classical", "baroque", "romantic", "modern classical",
                       "orchestral", "chamber music", "opera"},
    "folk_country":  {"folk", "country", "americana", "bluegrass", "indie folk",
                       "alt-country"},
    "latin":         {"latin", "reggaeton", "salsa", "bachata", "cumbia", "latin pop"},
    "reggae":        {"reggae", "dancehall", "ska", "dub"},
}


# ════════════════════════════════════════════════════════════════════════════
# MK33 O5-T1 (A1): Evidence-native projection layer — FOUNDATION for MK34+.
# CTO: "biggest remaining architectural contradiction — you carefully preserve
# ambiguity, contested evidence, ontology fuzziness, policy nuance, authority
# weighting, semantic conflict severity...and then still terminate with
# final_genre = choose_one(). Everything upstream evolved. The final output
# model did not."
#
# This is NOT a full migration. It's the seed:
#   - Audit STILL writes a single chosen genre to the file (backward compat)
#   - But ALSO writes a ResolvedView to the cache entry preserving:
#       all evidence considered, which won, why, under which policy version
#   - MK34+ can read the projection when output formats support multi-evidence
# ════════════════════════════════════════════════════════════════════════════

class ResolvedView:
    """
    Evidence-native projection of a resolved genre decision.
    Preserves all upstream evidence even though the file write collapses to one value.
    """
    __slots__ = ("chosen", "all_evidence", "decision_path",
                 "policy_version", "severity", "ts")

    def __init__(self, chosen, all_evidence, decision_path,
                 policy_version="", severity="trivial", ts=0):
        self.chosen         = chosen
        self.all_evidence   = list(all_evidence)
        self.decision_path  = decision_path
        self.policy_version = policy_version
        self.severity       = severity
        self.ts             = ts or int(__import__("time").time())

    def to_dict(self) -> dict:
        return {
            "chosen":         self.chosen,
            "all_evidence":   self.all_evidence,
            "decision_path":  self.decision_path,
            "policy_version": self.policy_version,
            "severity":       self.severity,
            "ts":             self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ResolvedView":
        return cls(
            chosen=d.get("chosen", ""),
            all_evidence=d.get("all_evidence", []),
            decision_path=d.get("decision_path", "unknown"),
            policy_version=d.get("policy_version", ""),
            severity=d.get("severity", "trivial"),
            ts=int(d.get("ts", 0)),
        )

    def __repr__(self) -> str:
        return (
            f"ResolvedView(chosen={self.chosen!r}, "
            f"path={self.decision_path!r}, severity={self.severity!r}, "
            f"evidence={len(self.all_evidence)} sources)"
        )

    def to_mk34_normalized_track(self, track_key: str, identity: dict | None = None):
        """
        MK34 P1-T3 bridge: hydrate this ResolvedView into a NormalizedTrack.
        Allows the MK33 evidence projection to feed the MK34 reconciliation engine
        without data migration. Both layers coexist during the MK33 → MK34 transition.
        """
        try:
            from mk34_reconciliation import NormalizedTrack
            return NormalizedTrack.from_resolved_view(track_key, self.to_dict(), identity)
        except ImportError:
            # mk34_reconciliation not yet installed; return None as graceful degradation
            return None


def build_resolved_view(chosen_genre, phase1_evidence=None, ai_evidence=None,
                        decision_path="unknown", severity="trivial"):
    """
    Construct a ResolvedView from the evidence sources used in a decision.
    `chosen_genre` = single value written to the file.
    Evidence dicts: {source, value, authority, confidence, evaluability}.
    """
    try:
        import config as _cfg_o5
        _ver = _cfg_o5.POLICY_VERSION
    except Exception:
        _ver = "unknown"

    evidence = []
    for ev in (phase1_evidence, ai_evidence):
        if ev and ev.get("value"):
            evidence.append({
                "source":       ev.get("source", "unknown"),
                "value":        ev.get("value"),
                "authority":    ev.get("authority", "unknown"),
                "confidence":   ev.get("confidence"),
                "evaluability": ev.get("evaluability", "unknown"),
            })

    return ResolvedView(
        chosen=chosen_genre,
        all_evidence=evidence,
        decision_path=decision_path,
        policy_version=_ver,
        severity=severity,
    )


def _genre_family(genre: str) -> str:
    """Return the family name for a genre, or 'unknown_family' if not categorized."""
    g = genre.lower().strip()
    for family, members in GENRE_FAMILIES.items():
        if g in members:
            return family
    return "unknown_family"


# MK33 O3-T2: Track novel genres encountered in the current session.
# CTO: "Amapiano, 3-Step, Private School Piano — emerging genres should not be
# auto-classified as catastrophic conflicts." Operator can review at end of run.
_NOVEL_GENRES_SEEN: set = set()
_NOVEL_GENRES_LOCK = __import__("threading").Lock()


def get_novel_genres_seen() -> list:
    """Return sorted list of novel genres encountered this session (for ontology review)."""
    with _NOVEL_GENRES_LOCK:
        return sorted(_NOVEL_GENRES_SEEN)


def reset_novel_genres_seen() -> None:
    """Clear the session's novel genre tracker."""
    with _NOVEL_GENRES_LOCK:
        _NOVEL_GENRES_SEEN.clear()


def classify_conflict_severity(genre_a: str, genre_b: str) -> str:
    """
    MK32 N3-T1 + MK33 O3-T1: Classify genre conflict severity.

    Returns:
      "trivial"     — same canonical genre (case/whitespace only)
      "taxonomic"   — same family (e.g. Neo Soul vs Alt R&B). Low severity.
      "novel"       — at least one genre not in known taxonomy. CTO: "system must
                      not bias against emerging genres by treating them as catastrophic."
                      Logs ONTOLOGY_DRIFT_DETECTED for operator review.
      "categorical" — different known families (e.g. Classical vs Drill). High severity.
    """
    if not genre_a or not genre_b:
        return "trivial"
    a, b = genre_a.lower().strip(), genre_b.lower().strip()
    if a == b:
        return "trivial"
    fam_a = _genre_family(a)
    fam_b = _genre_family(b)

    # MK33 O3-T1: novel tier — at least one genre is uncategorized
    if fam_a == "unknown_family" or fam_b == "unknown_family":
        # Track which novel genres we've seen for taxonomy review
        with _NOVEL_GENRES_LOCK:
            if fam_a == "unknown_family":
                _NOVEL_GENRES_SEEN.add(a)
            if fam_b == "unknown_family":
                _NOVEL_GENRES_SEEN.add(b)
        import logging as _l_o3
        _l_o3.getLogger(__name__).info(
            f"  [GenreFamily/O3] ONTOLOGY_DRIFT_DETECTED "
            f"({a!r} vs {b!r}) — at least one not in GENRE_FAMILIES, "
            f"classified 'novel' (not 'categorical'). Add to taxonomy if recurring."
        )
        return "novel"

    if fam_a == fam_b:
        return "taxonomic"
    return "categorical"


def sample_validate_external_metadata(
    max_sample: int = 50,
    sample_rate: float = 0.01,
) -> dict:
    """
    MK30 F3-T1 (A1): 1% sampling validation of external_metadata cache entries.
    CTO: "skipping external_metadata creates permanent blind trust zones — you can
    never detect upstream provider corruption."

    Strategy:
    - Sample 1% of external_metadata entries (capped at max_sample=50 per run)
    - For LastFm-sourced entries, cross-check against Deezer and vice versa
    - Log EXTERNAL_METADATA_MISMATCH if providers disagree
    - Downgrade mismatched entries to confidence=0.70, source_type="heuristic"
    - Non-blocking: runs as background check, does not delay pipeline

    Returns {"sampled": N, "mismatches": N, "downgraded": N}
    """
    import random, logging as _log_f3
    _log = _log_f3.getLogger(__name__)

    cache       = _get_genre_cache()
    # Find external_metadata entries
    candidates = [
        (k, v) for k, v in cache.items()
        if isinstance(v, dict)
        and v.get("_source_type") == "external_metadata"
        and v.get("source") not in (None, "")
    ]
    if not candidates:
        return {"sampled": 0, "mismatches": 0, "downgraded": 0}

    n_sample   = min(max_sample, max(1, int(len(candidates) * sample_rate)))
    sample     = random.sample(candidates, n_sample)
    mismatches = 0
    downgraded = 0

    for key, entry in sample:
        # Cross-check: if source=lastfm, try deezer; if source=deezer, try lastfm
        _src   = str(entry.get("source", "")).lower()
        _genre = entry.get("genre", "")
        if not _genre:
            continue

        # Parse key back to artist|title (key format: "artist|title")
        parts = key.split("|")
        if len(parts) < 2:
            continue
        _artist, _title = parts[0], parts[1] if len(parts) > 1 else ""

        # Attempt secondary lookup (non-blocking — single call)
        _secondary_genre = ""
        try:
            if "lastfm" in _src:
                _secondary_genre, _ = _deezer_lookup(_artist, _title)
            elif "deezer" in _src:
                _secondary_genre, _ = _lastfm_lookup(_artist, _title)
        except Exception:
            continue  # secondary unavailable — skip, don't flag

        if not _secondary_genre:
            continue  # secondary miss — can't validate, don't flag

        # Normalise both for comparison
        _prim_norm = _normalize_genre(_genre).lower().strip()
        _sec_norm  = _normalize_genre(_secondary_genre).lower().strip()

        if _prim_norm and _sec_norm and _prim_norm != _sec_norm:
            mismatches += 1
            # MK31 M3-T1 (A1): CTO: "disagreement ≠ heuristic quality — it's contested evidence."
            # Both providers may be authoritative but using different taxonomic frameworks.
            # "Neo Soul" vs "Alt R&B" for The Weeknd — both defensible, neither wrong.
            # source_type="contested_external" preserves the authority context while flagging conflict.
            _log.warning(
                f"  [GenreCache/F3] EXTERNAL_METADATA_MISMATCH "
                f"{_artist!r} — {_title!r}: "
                f"{_src}={_genre!r} vs secondary={_secondary_genre!r}. "
                f"Marking as contested_external (not degrading to heuristic — both may be valid)."
            )
            # N3-T1: Classify severity (taxonomic vs categorical)
            _severity = classify_conflict_severity(_genre, _secondary_genre)
            _log.warning(
                f"  [GenreCache/N3] CONFLICT_SEVERITY={_severity!r} "
                f"({_genre!r} vs {_secondary_genre!r})"
            )
            with _GENRE_CACHE_LOCK:
                if key in cache:
                    cache[key]["_source_type"]       = "contested_external"
                    cache[key]["validation_state"]   = "conflicted"
                    cache[key]["conflict_severity"]  = _severity   # N3-T1: severity tag
                    cache[key]["contested_genres"]   = [_genre, _secondary_genre]
                    cache[key]["evaluability"]        = "verifiable"
                    cache[key]["authority"]           = "contested"
                    downgraded += 1
            if downgraded > 0:
                _flush_genre_cache_locked(cache)

    _log.debug(
        f"  [GenreCache/F3] External sampling: {n_sample}/{len(candidates)} checked, "
        f"{mismatches} mismatches, {downgraded} downgraded."
    )
    return {"sampled": n_sample, "mismatches": mismatches, "downgraded": downgraded}


def _normalize_genre(raw: str, artist: str = "", title: str = "",
                      year: str = "") -> str:
    """
    Map a raw API genre string to the canonical Walkman genre tag.

    MK10 ISSUE-D: also checks GENRE_ARTIST_OVERRIDES and GENRE_TITLE_OVERRIDES
    from config.py so user-defined categories take highest priority.
    MK10 ISSUE-C: applies era-aware subgenre promotion after norm lookup.

    Returns the normalized string, or the original title-cased if no mapping found.
    """
    if not raw:
        return ""

    # ── MK10 ISSUE-D: config title-pattern overrides (highest priority) ──
    if title:
        try:
            import config as _c
            title_overrides = getattr(_c, "GENRE_TITLE_OVERRIDES", {})
            tl = title.lower()
            for pattern, override_genre in title_overrides.items():
                if pattern and pattern in tl:
                    return override_genre
        except Exception:
            pass

    key = raw.strip().lower()
    if key in _GENRE_NORM:
        normalized = _GENRE_NORM[key]
    else:
        # Partial match: e.g. "Afrobeats & Dancehall" → "Afrobeats"
        normalized = raw.title()
        for k, v in _GENRE_NORM.items():
            if k in key:
                normalized = v
                break

    # ── MK10 ISSUE-C: era-aware subgenre promotion ──────────────────────
    if year:
        normalized = apply_era_genre(normalized, year)

    # ── MK39 Phase I-B (Council 2026-05-10 §3-B): block Abstract Hip-Hop
    # as a silent default for artists not on the curated allow-list.
    # The dominant-bleed pattern in the log was: API/AI returns nothing
    # confident → cache mapping returns "Abstract Hip-Hop" because that
    # genre dominates the override dict by volume → wrong genre written.
    # Now blocked at the normalization layer regardless of which provider
    # produced it.
    try:
        import mk39_phase_I_containment as _p1
        _filtered = _p1.block_abstract_hip_hop_default(normalized, artist)
        if _filtered is None:
            return ""    # Council preference: blank over confident error
        normalized = _filtered
    except Exception:
        pass

    return normalized


# ---------------------------------------------------------------------------
# MK10 ISSUE-C — Era-aware genre tagging
# ---------------------------------------------------------------------------
# CTO direction: distinguish "classic Stevie Wonder soul" from "modern Brent
# Faiyaz soul".  We implement year-threshold rules that promote broad genre
# strings ("Soul", "R&B", "Funk", "Jazz") into era-specific subgenres.
#
# The split year boundaries are chosen conservatively:
#   Soul  : pre-1975  → Classic Soul    (Motown, Stax, Atlantic golden era)
#           1975-1989 → Soul            (post-disco transition, keep as Soul)
#           1990-2005 → Neo Soul        (D'Angelo, Erykah Badu, Maxwell era)
#           post-2005 → Contemporary Soul / Neo Soul (user can override via config)
#
#   R&B   : pre-1990  → Classic R&B
#           1990-2009 → R&B             (90s/2000s R&B golden age — already specific)
#           post-2010 → Contemporary R&B
#
#   Funk  : pre-1985  → Classic Funk    (Parliament, James Brown, Sly Stone era)
#           post-1985 → Funk
#
#   Jazz  : pre-1970  → Classic Jazz / Bebop / Hard Bop (by subgenre detection)
#           post-1980 → Contemporary Jazz / Jazz Fusion
#
# Boundaries and canonical names are fully configurable via config.py:
#   ERA_GENRE_RULES  (dict, see below — config overrides the defaults here)
#   ERA_GENRE_ENABLED (bool, default True)

_DEFAULT_ERA_RULES: list[dict] = [
    # =========================================================================
    # MK10: Full era-aware genre rules for the complete library genre landscape
    # =========================================================================
    # Design philosophy:
    #   - Rules fire ONLY on broad/unqualified genre strings (e.g. "Soul", not
    #     "Neo Soul" or "Classic Soul" — those are already specific).
    #   - Year boundaries are conservative and historically motivated.
    #   - All rules are overridable via config.ERA_GENRE_RULES.
    #   - Genres not listed here pass through unchanged — intentional.

    # ── Soul ─────────────────────────────────────────────────────────────────
    # Motown / Stax / Atlantic golden era → Classic Soul
    # Disco-funk / late 70s transition → Soul (no subgenre needed)
    # D'Angelo / Erykah Badu / Maxwell era → Neo Soul
    # Frank Ocean / SZA / Brent Faiyaz era → Contemporary Soul
    {"genre": "Soul", "year_max": 1974, "result": "Classic Soul"},
    {"genre": "Soul", "year_min": 1975, "year_max": 1989, "result": "Soul"},
    {"genre": "Soul", "year_min": 1990, "year_max": 2009, "result": "Neo Soul"},
    {"genre": "Soul", "year_min": 2010, "result": "Contemporary Soul"},

    # ── R&B ──────────────────────────────────────────────────────────────────
    # Pre-1990: Quiet Storm, New Jack Swing origins → Classic R&B
    # 1990-2009: TLC / Destiny's Child / Usher golden era → R&B (already peak)
    # 2010+: Weeknd / Drake / SZA streaming era → Contemporary R&B
    {"genre": "R&B",  "year_max": 1989, "result": "Classic R&B"},
    {"genre": "R&B",  "year_min": 1990, "year_max": 2009, "result": "R&B"},
    {"genre": "R&B",  "year_min": 2010, "result": "Contemporary R&B"},

    # ── Funk ─────────────────────────────────────────────────────────────────
    # Parliament / James Brown / Sly Stone era → Classic Funk
    # Post-Prince / post-disco funk → Funk
    {"genre": "Funk", "year_max": 1984, "result": "Classic Funk"},
    {"genre": "Funk", "year_min": 1985, "result": "Funk"},

    # ── Hip-Hop ──────────────────────────────────────────────────────────────
    # No era rules for "Hip-Hop" itself — it's already a broad umbrella and
    # the pipeline should already have resolved it to a subgenre (Boom Bap,
    # Trap, Drill, etc.) via the _BROAD_TO_SPECIFIC artist lookup.
    # We only add rules for BARE "Rap" which Last.fm sometimes returns:
    #   pre-1986: Old School Hip-Hop (Sugarhill / Grandmaster Flash era)
    #   1986-1999: Golden Age (Rakim / EPMD / Wu-Tang / Nas era)
    #   2000-2012: Post-Golden (Kanye / Lil Wayne / Drake come-up era)
    #   2013+:     passthrough (should already be Trap/Drill/etc. by this point)
    {"genre": "Rap", "year_max": 1985, "result": "Old School Hip-Hop"},
    {"genre": "Rap", "year_min": 1986, "year_max": 1999, "result": "Boom Bap"},
    {"genre": "Rap", "year_min": 2000, "year_max": 2012, "result": "Hip-Hop"},
    {"genre": "Rap", "year_min": 2013, "result": "Hip-Hop"},

    # ── Afrobeats ────────────────────────────────────────────────────────────
    # Fela Kuti / Tony Allen Afrobeat era (note: "Afrobeat" singular vs
    # "Afrobeats" plural — both are in _GENRE_NORM but the era split is useful
    # for "Afrobeats" as a catch-all label returned by lazy APIs).
    # pre-2000: Roots Afrobeats / Highlife era → mapped to Highlife
    # 2000-2011: Pioneer Afrobeats (2face, P-Square, D'banj era)
    # 2012+: Contemporary Afrobeats (Burna Boy, Wizkid, Davido streaming era)
    {"genre": "Afrobeats", "year_max": 1999, "result": "Highlife"},
    {"genre": "Afrobeats", "year_min": 2000, "year_max": 2011, "result": "Afrobeats"},
    {"genre": "Afrobeats", "year_min": 2012, "result": "Afrobeats"},

    # Afropop (pan-African, looser) — split at streaming era
    {"genre": "Afropop",  "year_max": 1999, "result": "Highlife"},
    {"genre": "Afropop",  "year_min": 2000, "result": "Afropop"},

    # ── Reggae ───────────────────────────────────────────────────────────────
    # Roots Reggae: Bob Marley / Burning Spear / Culture era (1968-1982)
    # Dancehall: Yellowman / Shabba / Beenie Man era (1980+, overlaps)
    # We use 1983 as the practical split where dancehall took over the charts.
    {"genre": "Reggae", "year_max": 1982, "result": "Roots Reggae"},
    {"genre": "Reggae", "year_min": 1983, "year_max": 1999, "result": "Reggae"},
    {"genre": "Reggae", "year_min": 2000, "result": "Reggae"},

    # ── Electronic ───────────────────────────────────────────────────────────
    # Kraftwerk / Giorgio Moroder era → Classic Electronic
    # House / Techno / Detroit era → Electronic (already specific enough)
    # EDM / post-streaming → keep as Electronic unless subgenre is known
    {"genre": "Electronic", "year_max": 1984, "result": "Classic Electronic"},
    {"genre": "Electronic", "year_min": 1985, "result": "Electronic"},

    # ── Disco ────────────────────────────────────────────────────────────────
    # Disco is already era-specific (1974-1981 peak) but sometimes applied to
    # Nu-Disco (2005+) revival artists — split to avoid conflation.
    {"genre": "Disco", "year_max": 1984, "result": "Disco"},
    {"genre": "Disco", "year_min": 1985, "year_max": 2004, "result": "Disco"},
    {"genre": "Disco", "year_min": 2005, "result": "Nu-Disco"},

    # ── Jazz ─────────────────────────────────────────────────────────────────
    # Bebop / Hard Bop / Cool Jazz golden era → Classic Jazz
    # Post-Bop / Fusion transition era → Jazz
    # Post-80s Contemporary Jazz / Smooth Jazz era
    {"genre": "Jazz", "year_max": 1959, "result": "Classic Jazz"},
    {"genre": "Jazz", "year_min": 1960, "year_max": 1979, "result": "Jazz"},
    {"genre": "Jazz", "year_min": 1980, "result": "Contemporary Jazz"},

    # ── Gospel ───────────────────────────────────────────────────────────────
    # Traditional / Quartet Gospel → Classic Gospel
    # Contemporary / Urban Gospel (Kirk Franklin era) → Contemporary Gospel
    {"genre": "Gospel", "year_max": 1979, "result": "Traditional Gospel"},
    {"genre": "Gospel", "year_min": 1980, "year_max": 1999, "result": "Gospel"},
    {"genre": "Gospel", "year_min": 2000, "result": "Contemporary Gospel"},

    # ── Pop ──────────────────────────────────────────────────────────────────
    # Bare "Pop" is too broad for era-splitting to be meaningful for this
    # library — Hip-Hop / Afrobeats / Soul tracks tagged "Pop" by lazy APIs
    # should have been corrected upstream. No rules — pass through as-is.

    # ── Rock ─────────────────────────────────────────────────────────────────
    # Bare "Rock" similarly too broad. Specific subgenres (Classic Rock,
    # Indie Rock, Alternative) are already in _GENRE_NORM.
    # One useful split: bare "Rock" pre-1980 → Classic Rock
    {"genre": "Rock", "year_max": 1979, "result": "Classic Rock"},
    {"genre": "Rock", "year_min": 1980, "result": "Rock"},
]


def apply_era_genre(genre: str, year: str | int) -> str:
    """
    Apply era-based subgenre promotion.

    Parameters
    ----------
    genre : str  — already-normalized genre (e.g. "Soul", "R&B", "Funk")
    year  : str | int — 4-digit release year (or empty string to skip)

    Returns
    -------
    str — the era-refined genre, or the original genre if no rule matches.

    This is called as a post-normalization step so it only fires on
    BROAD genres ("Soul", "R&B") — specific genres ("Neo Soul", "Trap Soul")
    are left untouched.  The function is idempotent: running it twice on
    "Classic Soul" does nothing because "Classic Soul" won't match the
    {"genre": "Soul"} rules.
    """
    if not genre or not year:
        return genre

    # Check if era-tagging is enabled
    try:
        import config as _c
        if not getattr(_c, "ERA_GENRE_ENABLED", True):
            return genre
    except Exception:
        pass

    try:
        y = int(str(year).strip()[:4])
    except (ValueError, TypeError):
        return genre

    if not (1900 <= y <= 2030):
        return genre   # implausible year — skip

    # Load rules from config if overridden, else use defaults
    rules = _DEFAULT_ERA_RULES
    try:
        import config as _c2
        cfg_rules = getattr(_c2, "ERA_GENRE_RULES", None)
        if cfg_rules and isinstance(cfg_rules, list):
            rules = cfg_rules
    except Exception:
        pass

    for rule in rules:
        if rule.get("genre", "").lower() != genre.lower():
            continue
        lo = rule.get("year_min", 0)
        hi = rule.get("year_max", 9999)
        if lo <= y <= hi:
            refined = rule["result"]
            if refined != genre:
                log.debug(
                    f"  [EraGenre] {genre!r} ({y}) → {refined!r}"
                )
            return refined

    return genre   # no rule matched → return as-is


# ---------------------------------------------------------------------------
# Manual genre overrides
# ---------------------------------------------------------------------------
# For artists where all APIs consistently return wrong genres. Checked before
# the API waterfall — overrides ONLY apply when the cache has nothing better.
# Keys: normalised artist name (lowercase, ASCII). Values: canonical genre tag.
# Add artist-specific overrides here when you know the correct genre.
_ARTIST_GENRE_OVERRIDES: dict[str, str] = {
    # =========================================================================
    # DESIGN PHILOSOPHY
    # =========================================================================
    # This dict is a FALLBACK of last resort — it fires only when the full
    # API waterfall (Last.fm → Deezer → MusicBrainz → Discogs → Google →
    # Gemini/Groq) either returned nothing or returned a confidently wrong
    # genre like "Alternative Rock" for MF DOOM.
    #
    # It does NOT prevent API lookups from running — those always run first.
    # The override only applies when:
    #   (a) all providers missed entirely, OR
    #   (b) the returned genre is broader/less accurate than the override
    #       (checked by _is_more_specific() in audit.py).
    #
    # Artists that span genres intentionally get their PRIMARY/MOST COMMON
    # genre here as the safe fallback. Individual tracks will still get more
    # specific genre tags from the APIs when the APIs succeed.
    #
    # Keys must be the output of _norm() — ASCII, lowercase, no articles,
    # collapsed whitespace, no punctuation except spaces.
    # Example: "Tyler, the Creator" → "tyler creator"
    #          "MF DOOM" → "mf doom"
    #          "$NOT" → "not"
    # =========================================================================

    # ── MF DOOM universe: all aliases are Daniel Dumile ───────────────────
    # Source: RateYourMusic — Vaudeville Villain = #4 Abstract Hip-Hop album
    # of all time; MF DOOM artist page genre = Abstract Hip-Hop / Experimental.
    # Last.fm erroneously returns "alternative" for Viktor Vaughn which the
    # _GENRE_NORM maps to "Alternative Rock" — this override corrects that.
    "mf doom":              "Abstract Hip-Hop",
    "viktor vaughn":        "Abstract Hip-Hop",   # Vaudeville Villain alter ego
    "king geedorah":        "Abstract Hip-Hop",   # three-headed monster alter ego
    "metal fingers":        "Abstract Hip-Hop",   # instrumental/production alias
    "zev love x":          "Abstract Hip-Hop",    # KMD-era name
    "dangerdoom":           "Abstract Hip-Hop",   # DOOM + Danger Mouse (Mouse & Mask)
    "madvillain":           "Abstract Hip-Hop",   # DOOM + Madlib (Madvillainy)
    "nehruviandoom":        "Abstract Hip-Hop",   # DOOM + Bishop Nehru
    "czarface":             "Boom Bap",           # Inspectah Deck + 7L & Esoteric; in library

    # ── Abstract / Underground Hip-Hop adjacent (heavy in Spotify history) ─
    # These artists appear in top-500 plays and sit in the same underground
    # scene. APIs return inconsistent results for all of them.
    "madlib":               "Abstract Hip-Hop",   # Madvillainy producer; Quasimoto alias
    "quasimoto":            "Abstract Hip-Hop",   # Madlib alter ego
    "mf grimm":             "Abstract Hip-Hop",   # frequent DOOM collaborator
    "aesop rock":           "Abstract Hip-Hop",   # RYM Abstract Hip-Hop top 5 artist
    "busdriver":            "Abstract Hip-Hop",   # RYM; Anticon collective
    "armand hammer":        "Abstract Hip-Hop",   # billy woods + ELUCID
    "billy woods":          "Abstract Hip-Hop",   # Armand Hammer member
    "elucid":               "Abstract Hip-Hop",   # Armand Hammer member
    "navy blue":            "Abstract Hip-Hop",   # RYM; underground NY abstract rap
    "mavi":                 "Abstract Hip-Hop",   # Charlotte underground lyricist
    "mach hommy":           "Abstract Hip-Hop",   # Griselda-adjacent; esoteric/abstract
    "ka":                   "Abstract Hip-Hop",   # NYC; shadow of the sun; underground
    "mike":                 "Abstract Hip-Hop",   # MIKE (rapper); experimental NY rap
    "maxo":                 "Abstract Hip-Hop",   # Maxo; RYM-rated abstract hip-hop
    "milo":                 "Abstract Hip-Hop",   # now raps as Scallops Hotel
    "quelle chris":         "Abstract Hip-Hop",   # Detroit abstract hip-hop
    "noname":               "Abstract Hip-Hop",   # Chicago; note-worthy lyricist
    "saba":                 "Conscious Hip-Hop",  # Chicago; CARE FOR ME; introspective
    "mick jenkins":         "Conscious Hip-Hop",  # Chicago; conscious/abstract
    "injury reserve":       "Experimental Hip-Hop",  # Phoenix experimental rap
    "jpegmafia":            "Experimental Hip-Hop",  # already in map; confirm
    "death grips":          "Experimental Hip-Hop",  # Sacramento noise-rap
    "run the jewels":       "Boom Bap",           # El-P + Killer Mike; aggressive boom bap
    "killer mike":          "Conscious Hip-Hop",  # RTJ member; solo = Southern Rap
    "el p":                 "Experimental Hip-Hop",  # Company Flow; RTJ
    "company flow":         "Abstract Hip-Hop",   # Funcrusher Plus; underground classic
    "deltron 3030":         "Abstract Hip-Hop",   # Del + Dan the Automator + Kid Koala
    "del the funky homosapien": "Abstract Hip-Hop",  # Deltron 3030; Hieroglyphics
    "j dilla":              "Abstract Hip-Hop",   # Donuts; influential Detroit producer
    "knxwledge":            "Lo-Fi",              # beats-focused; lo-fi hip-hop
    "mndsgn":               "Neo Soul",           # Mndsgn; Ringpiece; soulful electronic
    "teebs":                "Lo-Fi",              # experimental beat tape; Brainfeeder
    "flying lotus":         "Jazz Rap",           # Brainfeeder; jazz-influenced electronic
    "thundercat":           "Neo Soul",           # Brainfeeder; funk/soul bass
    "the gaslamp killer":   "Abstract Hip-Hop",   # LA beat scene; Brainfeeder-adjacent
    "knucks":               "UK Rap",             # London rapper; Alpha Place
    "central cee":          "UK Drill",           # London; 23 album; melodic UK drill
    "dave":                 "UK Rap",             # Streatham; Psychodrama; UK conscious rap
    "skepta":               "Grime",              # North London; Konnichiwa; grime godfather
    "stormzy":              "Grime",              # South London; Gang Signs & Prayer
    "jme":                  "Grime",              # BBK; Boy Better Know; UK grime
    "giggs":                "UK Rap",             # South London; Landlord
    "headie one":           "UK Drill",           # Tottenham; UK drill originator
    "digga d":              "UK Drill",           # OFB; UK drill
    "abra cadabra":         "UK Drill",           # London
    "kwengface":            "UK Drill",           # OFB; London

    # ── Griselda / Buffalo Boom Bap ────────────────────────────────────────
    # Westside Gunn, Benny the Butcher, Conway appear in DJ Vic Club Chops
    # folder and are top-500 Spotify plays. Hard-hitting sample-based boom bap.
    # Source: Pitchfork, AllMusic, RYM unanimous.
    "westside gunn":        "Boom Bap",
    "benny the butcher":    "Boom Bap",
    "conway the machine":   "Boom Bap",
    "freddie gibbs":        "Boom Bap",           # Piñata with Madlib = canonical Boom Bap
    "boldy james":          "Boom Bap",           # Detroit; The Price of Tea in China
    "roc marciano":         "Boom Bap",           # New York; Marcberg; lo-fi boom bap
    "action bronson":       "Boom Bap",           # Queens; Saab Stories
    "smoke dza":            "Boom Bap",           # Harlem; Kushed God
    "domo genesis":         "Boom Bap",           # Odd Future; Rolling Papers
    "currensy":             "Boom Bap",           # New Orleans; Pilot Talk
    "larry june":           "Boom Bap",           # Bay Area; organic boom bap
    "the alchemist":        "Boom Bap",           # producer; Alc files
    "nas":                  "East Coast Rap",     # Queens; Illmatic
    "gang starr":           "East Coast Rap",     # Guru + DJ Premier
    "a tribe called quest": "East Coast Rap",     # Queens; Native Tongues
    "digable planets":      "Jazz Rap",           # jazz-hop collective
    "slum village":         "Jazz Rap",           # Detroit; J Dilla's group
    "wu tang clan":         "East Coast Rap",     # Staten Island; 36 Chambers
    "ol dirty bastard":     "East Coast Rap",     # Wu-Tang member
    "ghostface killah":     "East Coast Rap",     # Wu-Tang; Iron Man
    "raekwon":              "East Coast Rap",
    "gza":                  "East Coast Rap",
    "nas ebk":              "East Coast Rap",
    "joey bada":            "East Coast Rap",     # Pro Era; normalised strips $$
    "ab soul":              "Conscious Hip-Hop",  # TDE; Control System; introspective
    "black thought":        "East Coast Rap",     # The Roots MC
    "the roots":            "Neo Soul",           # Philly; live instruments + rap
    "lootpack":             "Abstract Hip-Hop",   # Madlib's group (Soundpieces)
    "jaylib":               "Abstract Hip-Hop",   # Madlib + J Dilla collab

    # ── Tyler, The Creator / Odd Future orbit ─────────────────────────────
    # #1 most played artist in this library (2,974 plays). APIs return
    # "Alternative Hip Hop", "Rap/Hip Hop", "Alternative" — all too broad.
    # His genre has shifted across albums (Goblin → Wolf → Cherry Bomb →
    # Flower Boy → Igor → Call Me If You Get Lost). The safe override is
    # his primary critical classification. Individual API hits will
    # correctly tag Igor as R&B / Neo Soul, Goblin as Rap, etc.
    "tyler creator":        "Hip-Hop",            # broad fallback; API hits are specific
    "tyler the creator":    "Hip-Hop",            # alt normalisation
    "earl sweatshirt":      "Abstract Hip-Hop",   # already in map; top-538 plays

    # ── MK39 Phase I (Council 2026-05-10 forensic-log additions) ──────────
    # Council §1 named A$AP Rocky as receiving "Psychedelic Rock" for hip-hop
    # tracks. _norm() strips "$" so the canonical lookup key is "aap rocky".
    # Adding all spelling variants prevents future wrong defaults regardless
    # of cache state or input formatting.
    "aap rocky":            "Hip-Hop",            # _norm("A$AP Rocky") = "aap rocky"
    "asap rocky":           "Hip-Hop",            # variant spelling
    "a$ap rocky":           "Hip-Hop",            # raw string fallback
    "rakim mayers":         "Hip-Hop",            # legal name fallback
    "frank ocean":          "Alt R&B",            # Channel Orange / Blonde
    "weeknd":               "Alt R&B",            # The Weeknd — _norm() strips "the "
                                                  # After Hours/Beauty Behind The Madness
                                                  # = dark synth-pop Alt R&B, not Afrobeats
    # ── MK26 additions — confirmed wrong from LOG_6_5_26 audit run ──────────
    # SA artists getting "Afrobeats" or "Abstract Hip-Hop" when Groq was sole provider:
    "luxury sa":            "Afro House",          # SA Afro House producer (Mali 2.0)
    "babalwa m":            "Afro House",           # SA vocalist, Acquiesce album
    "issa sisdoh":          "Afro House",           # SA Afro House; Vuma Dlozi Lami
    "dj kayd boizen":       "Afro House",           # SA 3-step; Ngizwe Nami
    "peekay mzee":          "Afro House",           # SA; Point Of Order
    "volka":                "Afro House",           # SA; Umsindo
    "beekay monalayzzar":   "Afro House",           # SA 3-step Afro House; Amalobolo
    # City Pop artists getting "Abstract Hip-Hop" fallback:
    "shizuka kudo":         "City Pop",             # 80s Japanese idol/City Pop
    "kei ishiguro":         "City Pop",             # Yokohama Ragtime
    "yuki saitou":          "J-Pop",                # 90s Japanese J-Pop
                                                  # BBTM/AH=R&B, Starboy/Dawn FM=Synth-Pop;
                                                  # R&B fallback prevents Afrobeats from AI.
    "syd":                  "Alt R&B",            # The Internet lead; solo work
    "the internet":         "Alt R&B",            # already in map; confirm
    "matt martians":        "Alt R&B",            # The Internet member
    "steve lacy":           "Alt R&B",            # #2 most played after Tyler; Apollo / Gemini Rights
    "odd future":           "Hip-Hop",            # collective tag
    "kevin abstract":       "Hip-Hop",            # BROCKHAMPTON founder
    "brockhampton":         "Hip-Hop",            # boy band/rap collective
    "vince staples":        "West Coast Rap",     # Long Beach; Big Fish Theory
    "mike":                 "Abstract Hip-Hop",   # MIKE rapper (separate from Michael Jackson)
    "duckwrth":             "Alt R&B",            # LA; The SoundTrack
    "smino":                "Neo Soul",           # St. Louis; blkswn; jazz-soul rap
    "brent faiyaz":         "Alt R&B",            # already in map; #5 most played
    "sonder":               "Alt R&B",            # already in map
    "nxworries":            "Neo Soul",           # Anderson .Paak + Knxwledge
    "anderson paak":        "Neo Soul",           # Oxnard; Malibu; soul-rap fusion
    "free nationals":       "Neo Soul",           # Anderson .Paak's live band
    "kali uchis":           "Alt R&B",            # already in map
    "kamauu":               "Neo Soul",           # NYC; Mango; soulful
    "nico segal":           "Neo Soul",           # already in map (Donnie Trumpet)
    "donnie trumpet":       "Neo Soul",           # already in map
    "terrace martin":       "Neo Soul",           # LA; Velvet Portraits
    "masego":               "Alt R&B",            # TrapHouseJazz; trap + jazz + R&B
    "goldlink":             "Alt R&B",            # DMV; At What Cost; future bounce
    "amine":                "Hip-Hop",            # Portland; Good for You; varied style
    "aminé":                "Hip-Hop",            # same artist, accented

    # ── Kanye / Chicago orbit ─────────────────────────────────────────────
    # #4 most played (1,007 plays). Genre varies enormously by era.
    # Late Reg = Soulful Hip-Hop, Yeezus = Experimental, Donda = Gospel Rap.
    # "Hip-Hop" is the correct broad fallback; APIs handle per-track precision.
    "kanye west":           "Hip-Hop",
    "kid cudi":             "Alternative Hip-Hop",  # Man on the Moon; psychedelic rap
    "kids see ghosts":      "Alternative Hip-Hop",  # Kanye + Cudi collab
    "chance the rapper":    "Conscious Hip-Hop",    # Coloring Book; gospel rap
    "common":               "Conscious Hip-Hop",    # Chicago; Like Water for Chocolate
    "pusha t":              "Trap",                 # Daytona; My Name Is My Name
    "big sean":             "Hip-Hop",              # Detroit; Finally Famous
    "a ap rocky":           "Hip-Hop",              # ASAP Mob; Long. Live. ASAP
    "a ap mob":             "Hip-Hop",
    "a ap ferg":            "Hip-Hop",
    "nigo":                 "Hip-Hop",              # I Know NIGO; streetwear/music crossover
    "pharrell williams":    "Hip-Hop",              # producer/rapper; N.E.R.D
    "n e r d":              "Hip-Hop",              # Pharrell's rock-rap band
    "spillage village":     "Conscious Hip-Hop",    # Dreamville/EarthGang collective
    "earthgang":            "Conscious Hip-Hop",    # Atlanta; Mirrorland
    "dreamville":           "Hip-Hop",              # label compilation
    "cordae":               "Conscious Hip-Hop",    # The Lost Boy; introspective
    "omen":                 "Conscious Hip-Hop",    # Dreamville; Elephant Eyes
    "lute":                 "Conscious Hip-Hop",    # Dreamville; Gold Mouf
    "ari lennox":           "Neo Soul",             # Dreamville R&B
    "jid":                  "Southern Hip-Hop",     # Atlanta; DiCaprio 2
    "j i d":                "Southern Hip-Hop",     # alt normalisation
    "bas":                  "Conscious Hip-Hop",    # Dreamville; Too High to Riot
    "isaiah rashad":        "Southern Hip-Hop",     # Chattanooga; Cilvia Demo
    "schoolboy q":          "West Coast Rap",       # TDE; Blank Face LP
    "kendrick lamar":       "West Coast Rap",       # Compton; TPAB; #9 top plays
    "j cole":               "Conscious Hip-Hop",    # Fayetteville; KOD; 2014 FHD
    "mac miller":           "Hip-Hop",              # Pittsburgh; Swimming; #2 most played
    "childish gambino":     "Hip-Hop",              # Donald Glover; varied (Awaken My Love = R&B)
    "d smoke":              "Conscious Hip-Hop",    # Inglewood; Black Habits
    "d banj":               "Afrobeats",            # Nigerian; Oliver Twist

    # ── Playboi Carti / SoundCloud/Cloud Rap orbit ─────────────────────────
    # Carti = #2 most played (1,183 plays). Whole Lotta Red is Punk-influenced
    # but his core genre classification across platforms = Cloud Rap / Trap.
    "playboi carti":        "Cloud Rap",
    "ken carson":           "Cloud Rap",            # Opium label; Carti protégé
    "cochise":              "Cloud Rap",            # Cloud Rap / Trap / Dancehall blend
    "lil yachty":           "Cloud Rap",            # "Sailing Team"; Let's Start Here (2023 = Psych Rock)
    "don toliver":          "Melodic Trap",         # Houston; Heaven or Hell; melodic Auto-Tune trap
    "baby keem":            "Hip-Hop",              # Las Vegas; The Melodic Blue
    "yeat":                 "Cloud Rap",            # 2 Alivë; Lyfestyle
    "skaiwater":            "Emo Rap",              # alternative emo-adjacent rap
    "lucki":                "Cloud Rap",            # Chicago; alternative/cloud rap
    "trippie redd":         "Emo Rap",              # Canton; melodic trap/emo
    "lil uzi vert":         "Emo Rap",              # Philadelphia; Eternal Atake
    "ski mask the slump god": "Emo Rap",            # Florida; self-titled
    "rico nasty":           "Emo Rap",              # Maryland; Anger Management (with Kenny Beats)
    "comethazine":          "Trap",                 # St. Louis; Pandemic
    "midwxst":              "Emo Rap",              # Cincinnati; emo-adjacent rap
    "snot":                 "Cloud Rap",            # Cochise "Tell Em" collab ($not)
    "not":                  "Cloud Rap",            # $NOT normalised (strips $)
    "rich amiri":           "Cloud Rap",            # San Diego; alternative/cloud

    # ── Detroit Rap (heavy in log — Rio Da Yung OG, Babyface Ray etc) ──────
    # This library has significant Detroit rap presence from streaming data.
    "rio da yung og":       "Detroit Rap",
    "babyface ray":         "Detroit Rap",
    "babytron":             "Detroit Rap",          # Detroit; Megatron album
    "sheff g":              "Drill",                # Brooklyn drill
    "ice spice":            "Drill",                # Bronx; drill-pop crossover
    "sha ek":               "Drill",                # Bronx
    "sha gz":               "Drill",                # Bronx
    "kyle richh":           "Drill",                # Bronx
    "kay flock":            "Drill",                # Bronx
    "king von":             "Drill",                # Chicago; O'Block
    "lil durk":             "Drill",                # Chicago; OTF
    "polo g":               "Drill",                # Chicago; The GOAT
    "bfb da packman":       "Detroit Rap",          # Flint; Flint rap scene
    "sada baby":            "Detroit Rap",          # Detroit; Skuba Sada
    "veeze":                "Detroit Rap",          # Detroit; Ganger
    "tee grizzley":         "Detroit Rap",          # Detroit; First Day Out
    "peezy":                "Detroit Rap",          # Detroit; Activate
    "dthang":               "Detroit Rap",          # Detroit
    "icewear vezzo":        "Detroit Rap",          # Detroit
    "baby smoove":          "Detroit Rap",          # Detroit

    # ── Trap / Atlanta (confirmed API misses) ──────────────────────────────
    "travis scott":         "Trap",                 # already in map; confirm
    "future":               "Trap",                 # Atlanta; DS2; Purple Reign
    "young thug":           "Trap",                 # Atlanta; Barter 6
    "21 savage":            "Trap",                 # Atlanta; ISSA album
    "gunna":                "Trap",                 # Atlanta; DS4EVER
    "lil baby":             "Trap",                 # Atlanta; My Turn
    "metro boomin":         "Trap",                 # Atlanta producer; Heroes & Villains
    "jackboys":             "Trap",                 # Travis Scott's Cactus Jack crew
    "pi erre bourne":       "Cloud Rap",            # South Carolina; The Piesociety (production)
    "chief keef":           "Drill",                # Chicago; Finally Rich
    "teezo touchdown":      "Alternative Hip-Hop",  # unique/genre-defying; Pop + Rap
    "morray":               "Southern Hip-Hop",     # Fayetteville; Street Sermons
    "rod wave":             "Southern Hip-Hop",     # St. Petersburg FL; Pray 4 Love
    "moneybagg yo":         "Trap",                 # Memphis; A Gangsta's Pain

    # ── Mac Miller / Pittsburgh / introspective ────────────────────────────
    "mac miller":           "Hip-Hop",              # already above; Swimming / Circles
    "wiz khalifa":          "Hip-Hop",              # Pittsburgh; Rolling Papers

    # ── J. Cole era ───────────────────────────────────────────────────────
    # Different J. Cole albums differ: Come Up = Boom Bap, Off-Season = Trap.
    # "Conscious Hip-Hop" covers his studio-album identity best.
    "j cole":               "Conscious Hip-Hop",   # already above; confirm
    "j  cole":              "Conscious Hip-Hop",   # normalisation variant

    # ── OutKast / Southern Hip-Hop ────────────────────────────────────────
    "outkast":              "Southern Hip-Hop",
    "big boi":              "Southern Hip-Hop",
    "andre 3000":           "Southern Hip-Hop",
    "goodie mob":           "Southern Hip-Hop",
    "organized noize":      "Southern Hip-Hop",    # OutKast production crew

    # ── Bachata (remaining artists from log, not in existing map) ─────────
    "monchy alexandra":     "Bachata",             # normalised (& stripped)
    "hector acosta":        "Bachata",             # El Torito
    "toby love":            "Bachata",
    "xtreme":               "Bachata",             # Robert & Rocky
    "frank reyes":          "Bachata",
    "anthony santos":       "Bachata",
    "zacarias ferreira":    "Bachata",
    "joe veras":            "Bachata",
    "yoskar sarante":       "Bachata",
    "el chaval de la bachata": "Bachata",
    "raulin rodriguez":     "Bachata",
    "teodoro reyes":        "Bachata",
    "alex bueno":           "Bachata",
    "luis segura":          "Bachata",             # bachata traditionalist
    "juan luis guerra":     "Merengue",            # Dominican merengue/bachata crossover

    # ── Afrobeats (heavy in streaming data — confirm / extend) ────────────
    "burna boy":            "Afrobeats",           # already in map; #6 most played
    "wizkid":               "Afrobeats",           # already in map
    "davido":               "Afrobeats",           # already in map
    "asake":                "Afrobeats",           # already in map
    "ckay":                 "Afrobeats",           # already in map; Love Nwantiti
    "rema":                 "Afrobeats",           # already in map; Calm Down
    "omah lay":             "Afrobeats",           # already in map
    "fireboy dml":          "Afrobeats",           # already in map
    "tiwa savage":          "Afrobeats",           # already in map
    "ayra starr":           "Afrobeats",           # already in map
    "victony":              "Afrobeats",           # already in map
    "tems":                 "Afrobeats",           # already in map
    "mr eazi":              "Afrobeats",           # already in map
    "ruger":                "Afrobeats",           # already in map
    "oxlade":               "Afrobeats",           # Lagos; Oxlade from Africa
    "kizz daniel":          "Afrobeats",           # Lagos; Buga
    "pheelz":               "Afrobeats",           # Lagos; Finesse
    "olamide":              "Afrobeats",           # Yoruba rap + Afrobeats
    "adekunle gold":        "Afrobeats",           # Lagos; Afro Pop Vol. 1
    "flavour":              "Afrobeats",           # Enugu; Igbo-Afrobeats
    "tekno":                "Afrobeats",           # Lagos; Pana
    "patoranking":          "Afrobeats",           # Lagos; dancehall-Afrobeats
    "joeboy":               "Afrobeats",           # Lagos; Beginning EP
    "bayanni":              "Afrobeats",           # MAVIN roster; Lagos
    "lojay":                "Afrobeats",           # Lagos; LV N ATTN
    "wande coal":           "Afrobeats",           # Lagos; Mushin 2 Mo'Hits
    "bnxn":                 "Afrobeats",           # formerly Benson Daniel; Lagos
    "zinoleesky":           "Afrobeats",           # Marlian Music; Lagos
    "shallipopi":           "Afrobeats",           # Benin; ELON EP
    "rexxie":               "Afrobeats",           # Nigerian producer
    "sarz":                 "Afrobeats",           # Nigerian producer; Is Sarz On The Beat
    "cruel santino":        "Afrobeats",           # Odunsi protégé; Lagos alt
    "odunsi the engine":    "Afrobeats",           # Lagos alt/indie Afrobeats
    "amaarae":              "Afrobeats",           # Ghanaian; The Angel You Don't Know
    "fuse odg":             "Afrobeats",           # Ghanaian-British; TINA
    "kelvyn boy":           "Afrobeats",           # Ghanaian
    "r2bees":               "Afrobeats",           # Ghanaian hiplife/afropop
    "sarkodie":             "Afrobeats",           # Ghanaian; hiplife/hip-hop
    "shatta wale":          "Afrobeats",           # Ghanaian dancehall
    "samini":               "Afrobeats",           # Ghanaian reggae-afropop
    "koffee":               "Reggae",              # Jamaican; Toast
    "sean paul":            "Dancehall",           # Jamaican dancehall; Temperature
    "vybz kartel":          "Dancehall",           # Jamaican dancehall
    "konshens":             "Dancehall",           # Jamaican
    "gyptian":              "Dancehall",           # Jamaican; Hold You
    "musical youth":        "Reggae",              # Birmingham reggae pop; Pass the Dutchie
    "bob marley the wailers": "Reggae",            # normalisation drops &
    "1da banton":           "Afroswing",           # UK; For Granted
    "afro b":               "Afroswing",           # UK; Joanna
    "young t bugsey":       "Afroswing",           # UK; Don't Rush
    "yxng bane":            "Afroswing",           # UK
    "p square":             "Afrobeats",           # Nigerian duo; Temptation
    "d banj":               "Afrobeats",           # Nigerian; already above
    "2baba":                "Afrobeats",           # Nigerian; 2face Idibia
    "9ice":                 "Afrobeats",           # Nigerian; Street Credibility
    "fally ipupa":          "Afrobeats",           # Congolese; Jupiter
    "werrason":             "Afrobeats",           # Congolese; Wenge Musica
    "koffi olomide":        "Afrobeats",           # Congolese; Soukous king
    "awilo longomba":       "Afrobeats",           # Congolese; Karolina
    "magic system":         "Afrobeats",           # Ivorian; Premier Gaou
    "boj":                  "Afrobeats",           # Nigerian; DTHANG affiliate
    "naeto c":              "Afrobeats",           # Nigerian; You Know My Name

    # ── Bongo Flava (Tanzania — already in map, add missing) ──────────────
    "alikiba":              "Bongo Flava",          # Tanzanian; in top-178 plays
    "harmonize":            "Bongo Flava",          # already in map
    "rayvanny":             "Bongo Flava",          # already in map
    "matonya":              "Bongo Flava",          # already in map
    "diamond platnumz":     "Bongo Flava",          # already in map; #1 Tanzanian
    "mbosso":               "Bongo Flava",          # already in map
    "zuchu":                "Bongo Flava",          # already in map
    "nandy":                "Bongo Flava",          # already in map
    "marioo":               "Bongo Flava",          # already in map
    "jux":                  "Bongo Flava",          # already in map
    "ali kiba":             "Bongo Flava",          # same as alikiba
    "lolilo":               "Bongo Flava",          # already in map
    "ben pol":              "Bongo Flava",          # Tanzanian; in saved artists
    "abdukiba":             "Bongo Flava",          # Tanzanian; Kings Music
    "bien":                 "Afrobeats",            # Sauti Sol member (Kenyan)
    "sauti sol":            "Afrobeats",            # already in map
    "nameless":             "Afrobeats",            # Kenyan; In Love
    "wahu":                 "Afrobeats",            # Kenyan; Liar
    "nonini mgenge2ru":     "Genge",                # Nonini; Kenyan Genge
    "e sir":                "Genge",                # Kenyan; pioneering genge rapper

    # ── Amapiano / South African ──────────────────────────────────────────
    "focalistic":           "Amapiano",             # already in map
    "kabza de small":       "Amapiano",             # already in map
    "dj maphorisa":         "Amapiano",             # already in map
    "uncle waffles":        "Amapiano",             # Cape Town; DJ/dancer
    "kelvin momo":          "Amapiano",             # South African; Ndia
    "mas musiq":            "Amapiano",             # already in map
    "vigro deep":           "Amapiano",             # already in map
    "mellow sleazy":        "Amapiano",             # South African duo
    "semi tee":             "Amapiano",             # Labantwana Ama Uber
    "daliwonga":            "Amapiano",             # South African
    "felo le tee":          "Amapiano",             # South African
    "tyler icu":            "Amapiano",             # South African
    "mr jazziq":            "Amapiano",             # South African
    "de mthuda":            "Amapiano",             # South African
    "sam deep":             "Amapiano",             # South African
    "dj stokie":            "Amapiano",             # South African
    "prince kaybee":        "Afro House",           # South African; Piano Hub
    "sun el musician":      "Afro House",           # South African; To the World & Beyond
    "busta 929":            "Amapiano",             # South African
    "dj ramatoulaye":       "Amapiano",             # Botswana-based
    "soa mattrix":          "Amapiano",
    "murumba pitch":        "Amapiano",
    "shakes les":           "Amapiano",
    "nandipha808":          "Amapiano",
    "pabi cooper":          "Amapiano",
    "aymos":                "Amapiano",
    "reece madlisa":        "Amapiano",
    "njelic":               "Amapiano",
    "dbn gogo":             "Amapiano",
    "dj melzi":             "Amapiano",
    "myztro":               "Amapiano",
    "kamo mphela":          "Amapiano",
    "yumbs":                "Amapiano",
    "dj soulchild ac":      "Amapiano",
    "kaygeeda king":        "Amapiano",
    "caltonic sa":          "Amapiano",
    "dj bake":              "Amapiano",
    "sizwe nineteen":       "Amapiano",
    "dj kawest":            "Amapiano",
    "sho madjozi":          "Gqom",                # South African; John Cena
    "dj tira":              "Gqom",                 # already in map
    "babes wodumo":         "Gqom",                 # already in map

    # ── Haitian / French Caribbean / Kompa ────────────────────────────────
    "saucyn8te":            "Kompa",                # already in map
    "arly lariviere":       "Kompa",                # already in map
    "michaelle laudan":     "Kompa",                # already in map
    "tayron kwidans":       "Kompa",                # Haitian Kompa; in top plays
    "joé dwèt filé":        "Zouk",                 # Guadeloupean zouk
    "joe dwet file":        "Zouk",                 # normalised
    "franglish":            "French R&B",           # French R&B; in top plays
    "dadju":                "French R&B",           # French R&B; Poison ou antidote
    "tayc":                 "French R&B",           # French R&B/Afropop
    "gims":                 "French Rap",           # GIMS (Maître Gims); French Rap
    "keblack":              "French R&B",           # French R&B
    "ronisia":              "French R&B",           # French-Guinean; La Confiance
    "tour 2 garde":         "French Rap",           # French rap duo
    "el azhar":             "French Rap",           # French rap
    "anderson 1er":         "Kompa",                # Haitian-French crossover
    "alicia aylies":        "French R&B",           # French R&B
    "naza":                 "Afro Trap",            # French-Congolese; Pépite
    "dystinct":             "Afro Trap",            # Moroccan-Dutch; French trap
    "afara tsena":          "French R&B",           # Malagasy-French
    "aya nakamura":         "French R&B",           # French-Malian; Djadja; in top-82
    "vegedream":            "French R&B",           # French-Ivorian; Ramenez la coupe

    # ── Brazilian / Bossa Nova / MPB ──────────────────────────────────────
    "marcos valle":         "MPB",                  # Brazilian MPB; in top-222 plays
    "joao gilberto":        "Bossa Nova",           # father of bossa nova
    "antônio carlos jobim":  "Bossa Nova",           # canonical; Girl from Ipanema
    "antonio carlos jobim": "Bossa Nova",           # normalised
    "walter wanderley":     "Bossa Nova",           # Brazilian organist
    "luiz bonfa":           "Bossa Nova",           # Black Orpheus
    "stan getz":            "Bossa Nova",           # Getz/Gilberto collaboration
    "elis regina":          "MPB",                  # Brazilian; Como Nossos Pais
    "astrud gilberto":      "Bossa Nova",           # Girl from Ipanema singer
    "toquinho":             "MPB",                  # Brazilian guitarist; Aquarela
    "vinícius de moraes":   "Bossa Nova",           # lyricist; Girl from Ipanema co-writer
    "vinicius de moraes":   "Bossa Nova",           # normalised
    "caetano veloso":       "MPB",
    "gilberto gil":         "MPB",
    "nara leao":            "Bossa Nova",
    "gal costa":            "MPB",
    "maria bethania":       "MPB",
    "quarteto em cy":       "Bossa Nova",
    "joao donato":          "Bossa Nova",           # Brazilian pianist
    "jorge ben jor":        "MPB",                  # Mas Que Nada
    "tim maia":             "MPB",                  # Brazilian soul-pop
    "sergio mendes":        "Bossa Nova",           # Mas Que Nada
    "luiz bonfa":           "Bossa Nova",
    "rosa passos":          "Bossa Nova",
    "vinícius de moraes":   "Bossa Nova",
    "chico buarque":        "MPB",
    "ceu":                  "MPB",
    "miucha":               "Bossa Nova",
    "arturo sandoval":      "Jazz",
    "arthur verocai":       "MPB",
    "ivan lins":            "MPB",

    # ── Japanese City Pop (existing + extensions) ─────────────────────────
    "mariya takeuchi":      "City Pop",             # already in map
    "tatsuro yamashita":    "City Pop",             # already in map
    "miki matsubara":       "City Pop",             # already in map
    "anri":                 "City Pop",             # already in map
    "sugar babe":           "City Pop",             # already in map
    "masayoshi takanaka":   "City Pop",             # already in map; in top-53 plays
    "taeko onuki":          "City Pop",             # already in map
    "hiroshi sato":         "City Pop",             # already in map
    "yasuha":               "City Pop",             # already in map
    "toshiki kadomatsu":    "City Pop",             # already in map
    "omega tribe":          "City Pop",             # already in map
    "tube":                 "City Pop",             # already in map
    "shogun":               "City Pop",             # already in map
    "yukihiro takahashi":   "City Pop",             # already in map
    "azymuth":              "Brazilian Jazz",       # already in map
    "ginger root":          "City Pop",             # American City Pop revival; in saved artists
    "casiopea":             "City Pop",             # Japanese jazz fusion; in saved artists

    # ── J-Pop, J-Rock ────────────────────────────────────────────────────
    "babymetal":            "J-Pop",                # already in map
    "one ok rock":          "J-Rock",               # already in map
    "yoasobi":              "J-Pop",                # already in map; in saved artists
    "the pillows":          "J-Rock",               # FLCL soundtrack band; in saved artists
    "ado":                  "J-Pop",                # already in map
    "king gnu":             "J-Pop",                # already in map
    "kenshi yonezu":        "J-Pop",                # already in map
    "mrs green apple":      "J-Pop",                # already in map
    "platina jazz":         "J-Jazz",               # anime jazz covers; in top plays

    # ── K-Pop / K-Rap ────────────────────────────────────────────────────
    "bts":                  "K-Pop",                # already in map
    "blackpink":            "K-Pop",                # already in map
    "stray kids":           "K-Pop",                # already in map
    "twice":                "K-Pop",                # already in map
    "dean":                 "K-R&B",                # already in map
    "epik high":            "K-Hip Hop",            # already in map
    "zico":                 "K-Hip Hop",            # already in map
    "bigbang":              "K-Pop",                # already in map
    "iu":                   "K-Pop",                # already in map
    "g dragon":             "K-Pop",                # already in map (g-dragon normalised)

    # ── Classic Soul / Funk / R&B legends ────────────────────────────────
    "michael jackson":      "R&B",                  # pop/R&B; in top-191 plays
    "the jackson 5":        "Soul",                 # Motown classic soul
    "the jacksons":         "Soul",                 # Post-Motown
    "marvin gaye":          "Soul",                 # What's Going On; classic soul
    "stevie wonder":        "Soul",                 # Songs in the Key of Life
    "earth wind fire":      "Soul",                 # normalisation (strips &) Boogie Wonderland
    "the isley brothers":   "Soul",                 # Shout; It's Your Thing
    "parliament":           "Funk",                 # Mothership Connection; P-Funk
    "donny hathaway":       "Soul",                 # A Song for You
    "erykah badu":          "Neo Soul",             # Baduizm; Dallas neo-soul
    "d angelo":             "Neo Soul",             # Voodoo; Richmond neo-soul
    "ms lauryn hill":       "Neo Soul",             # The Miseducation; Fugees
    "bill withers":         "Soul",                 # Lean on Me; Lovely Day
    "herbie hancock":       "Jazz",                 # Head Hunters; jazz fusion
    "prince":               "Funk",                 # Purple Rain; Minneapolis sound
    "james brown":          "Funk",                 # Godfather of Soul
    "curtis mayfield":      "Soul",                 # Move On Up; Chicago soul
    "the gap band":         "Funk",                 # You Dropped a Bomb on Me
    "harold melvin the blue notes": "Soul",
    "the spinners":         "Soul",                 # Could It Be I'm Falling in Love
    "teddy pendergrass":    "Soul",
    "patrice rushen":       "Funk",                 # Forget Me Nots
    "chrisette michele":    "Neo Soul",             # I Am
    "alicia keys":          "R&B",                  # already in map (via partial match)
    "ashanti":              "R&B",                  # already in map
    "ne yo":                "R&B",                  # In My Own Words
    "b2k":                  "R&B",                  # Uh Huh; in top plays
    "usher":                "R&B",
    "bryson tiller":        "Alt R&B",              # Trapsoul; R&B/trap crossover
    "summer walker":        "Alt R&B",              # Over It
    "kehlani":              "Alt R&B",              # SweetSexySavage
    "cleo sol":             "Neo Soul",             # UK neo-soul
    "jorja smith":          "Alt R&B",              # Walsall; Lost & Found
    "blood orange":         "Alt R&B",              # Dev Hynes; Freetown Sound
    "omar apollo":          "Alt R&B",              # Hobart, IN; Ivory
    "alex isley":           "Alt R&B",              # Brooklyn; First Glance
    "robert glasper":       "Jazz",                 # Black Radio; jazz-hip-hop fusion
    "kamasi washington":    "Jazz",                 # The Epic; LA jazz
    "j dilla":              "Abstract Hip-Hop",     # already above
    "hiatus kaiyote":       "Neo Soul",             # Melbourne; Choose Your Weapon
    "domi jd beck":         "Jazz Fusion",          # LA jazz duo
    "yebba":                "Neo Soul",             # NY soul singer

    # ── Alternative / Indie Rock (in library) ────────────────────────────
    "radiohead":            "Alternative Rock",     # Thom Yorke; OK Computer
    "nirvana":              "Alternative Rock",     # Seattle grunge
    "tame impala":          "Psychedelic Rock",     # Kevin Parker; Currents
    "mac demarco":          "Indie Rock",           # Vancouver; Salad Days
    "king krule":           "Indie Rock",           # SE London; The Ooz
    "gorillaz":             "Alternative Rock",     # Damon Albarn; virtual band
    "unknown mortal orchestra": "Psychedelic Rock", # Portland; Multi-Love
    "connan mockasin":      "Psychedelic Rock",     # NZ; Caramel
    "tv girl":              "Dream Pop",            # San Diego; French Exit
    "homeshake":            "Bedroom Pop",          # ex-Mac DeMarco guitarist
    "mild high club":       "Psychedelic Rock",     # Alexander Brettin; Skiptracing
    "toro y moi":           "Chillwave",            # Chaz Bear; Causers of This
    "crumb":                "Dream Pop",            # Brooklyn; Jinx
    "beach house":          "Dream Pop",            # Baltimore; Teen Dream
    "pinkpantheress":       "UK Garage",            # already in map
    "cosmo pyke":           "Indie Pop",            # South London; Just Cosmo
    "choker":               "Bedroom Pop",          # Detroit; Honeybloom
    "men i trust":          "Dream Pop",            # Montreal; Untourable Album
    "joji":                 "Indie Pop",            # Japanese-Australian; Nectar
    "yellow days":          "Indie Rock",           # Glasgow; A World In Tact
    "king crimson":         "Progressive Rock",     # Canterbury; In the Court
    "slipknot":             "Metal",                # Des Moines; Iowa
    "system of a down":     "Metal",                # LA; Toxicity
    "black sabbath":        "Metal",                # Birmingham; Black Sabbath
    "nirvana":              "Alternative Rock",     # already above
    "death grips":          "Experimental Hip-Hop", # already above

    # ── Show Dem Camp / Nigerian rap ─────────────────────────────────────
    "show dem camp":        "Hip-Hop",              # Lagos; Theater of the Gaseous Almond

    # ── Misc confirmed misses from the log ────────────────────────────────
    "czarface":             "Boom Bap",             # already above (Inspector Deck + 7L)
    "kmd":                  "East Coast Rap",       # MF DOOM's original group; Zev Love X
    "danger mouse":         "Abstract Hip-Hop",     # DANGERDOOM producer; Gnarls Barkley
    "nehruviandoom":        "Abstract Hip-Hop",     # already above
    "madvillain":           "Abstract Hip-Hop",     # already above
    "mf grimm":             "Abstract Hip-Hop",     # already above
    "onra":                 "Abstract Hip-Hop",     # French producer; Chinoiseries
    "kaytranada":           "Electronic",           # Haitian-Canadian; 99.9%
    "kaytramine":           "Electronic",           # KAYTRANADA + Aminé collab
    "dj scheme":            "Hip-Hop",              # Florida; Scheme with Friends
    "kenny beats":          "Hip-Hop",              # Connecticut producer
    "black pumas":          "Soul",                 # Austin; Colors
    "sampha":               "Alt R&B",              # South London; Process
    "james blake":          "Alt R&B",              # London; The Colour in Anything

    # ── MK8 additions — LOG_17 AI-fallback mislabelings (Groq at capacity) ─
    # Groq llama-3.1-8b-instant produced wrong genres for these artists
    # when Gemini's free-tier quota was exhausted early in the session.
    # Each entry here is the correct genre that will now override the
    # erroneous Groq result on future runs.

    # R&B / Soul — mislabeled as Afrobeats
    "phabo":                "R&B",                 # Phabo; Los Angeles R&B singer
    "rell casso":           "R&B",                 # Philly R&B; New Aretha
    "lady donli":           "Alternative R&B",     # Lagos/Toronto; innovative Afro-R&B
    "wurld":                "Afropop",             # WurlD; Lagos Afropop + R&B fusions

    # Punjabi Pop — mislabeled as Afrobeats
    "ap dhillon":           "Punjabi Pop",          # Indo-Canadian; Brownprint

    # Afrobeats — mislabeled as Hip-Hop / K-Pop / other
    "boutross":             "Afrobeats",            # Kenyan; Angela; Nairobi Afrobeats
    "kilsosouth":           "Afrobeats",            # UK Afrobeats/Nigerian; THONG
    "nizzy":                "Afrobeats",            # UK Afrobeats; Billi Bands

    # Bongo Flava — mislabeled as Afrobeats
    "dully sykes":          "Bongo Flava",          # Tanzania; Salome / Utamu
    "ommy dimpoz":          "Bongo Flava",          # Tanzania; Nai Nai
    "aslay":                "Bongo Flava",          # Tanzania; Nibebe

    # Gqom — mislabeled as Afrobeats
    "maziya":               "Gqom",                # South Africa; eSamenteni

    # Jersey Club — mislabeled as Afrobeats
    "groovy":               "Jersey Club",          # GROOVY; jersey luv

    # Congolese / Soukous — mislabeled as Amapiano
    "congo worldwide":      "Soukous",              # Congolese; MBOKA PIANO

    # French Trap — mislabeled as Zouk
    "bramsito":             "French Trap",          # Paris; Faut pas négliger

    # Kompa — mislabeled as Afrobeats
    "lucas seb":            "Kompa",               # Haitian-French; Mortal kompa

    # Electronic/Novelty — mislabeled as Electronic (too broad) or wrong
    "siren gliss":          "Classical",            # Air on the G String / Thong Song mash

    # Afrobeats sub-genres — mislabeled as Amapiano (different country)
    "ugovhb":               "Afrobeats",            # Nigerian; WTF 2 slowed

    # Gospel / Afropop — mislabeled via Groq hallucination in LOG_17
    "nerih":                "Gospel/Afropop",        # Nigerian gospel-influenced Afropop
}

# ---------------------------------------------------------------------------
# Unicode script detection — auto-genre for non-Latin artists/titles
# ---------------------------------------------------------------------------
# When an artist name or track title contains characters from a non-Latin
# script, the API waterfall usually fails (Last.fm and Deezer return nothing
# for queries in Kanji, Hangul, Arabic, etc.) or returns a useless broad tag
# like "World Music".  Instead of requiring every such artist to be listed
# manually in _ARTIST_GENRE_OVERRIDES, we detect the dominant Unicode script
# and assign a sensible default genre.
#
# The genre assigned is the most meaningful *browsable* tag for that script on
# the ZX500 — e.g. "City Pop" for Japanese (not "J-Pop", which is too broad
# and loses the retro City Pop context; but see override table for specific
# known artists).  Users can always add manual overrides for artists they want
# to differentiate further.
#
# Detection order: check the artist name first; if it's purely ASCII (e.g. a
# Western artist who has a Japanese album title) check the track title too.
# If the title contains Japanese characters, the track itself is Japanese-
# language content and should be tagged accordingly.

def _dominant_script(text: str) -> str | None:
    """
    Return a script identifier for the dominant non-Latin block found in text,
    or None if the text is purely Latin/ASCII.

    Identifiers returned:
        "japanese"  — CJK + Hiragana/Katakana (or Katakana-only = loanword text)
        "chinese"   — CJK without any Kana (Simplified/Traditional)
        "korean"    — Hangul
        "arabic"    — Arabic script (covers Arabic, Persian, Urdu)
        "cyrillic"  — Russian/Eastern European Cyrillic
        "thai"      — Thai script
        "hindi"     — Devanagari (Hindi, Marathi, Sanskrit)
        "hebrew"    — Hebrew script
    """
    has_kana    = False   # Hiragana or Katakana
    has_cjk     = False
    has_hangul  = False
    has_arabic  = False
    has_cyrillic= False
    has_thai    = False
    has_deva    = False
    has_hebrew  = False

    for ch in text:
        cp = ord(ch)
        # Hiragana: U+3040–U+309F  Katakana: U+30A0–U+30FF  Katakana Ext: U+31F0–U+31FF
        if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:
            has_kana = True
        # CJK Unified: U+4E00–U+9FFF  Ext A: U+3400–U+4DBF  Compat: U+F900–U+FAFF
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF:
            has_cjk = True
        # Hangul syllables: U+AC00–U+D7A3  Jamo: U+1100–U+11FF
        elif 0xAC00 <= cp <= 0xD7A3 or 0x1100 <= cp <= 0x11FF:
            has_hangul = True
        # Arabic: U+0600–U+06FF  Arabic Presentation: U+FB50–U+FDFF  U+FE70–U+FEFF
        elif 0x0600 <= cp <= 0x06FF or 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF:
            has_arabic = True
        # Cyrillic: U+0400–U+04FF
        elif 0x0400 <= cp <= 0x04FF:
            has_cyrillic = True
        # Thai: U+0E00–U+0E7F
        elif 0x0E00 <= cp <= 0x0E7F:
            has_thai = True
        # Devanagari: U+0900–U+097F
        elif 0x0900 <= cp <= 0x097F:
            has_deva = True
        # Hebrew: U+0590–U+05FF
        elif 0x0590 <= cp <= 0x05FF:
            has_hebrew = True

    # Japanese: any Kana present, with or without CJK
    if has_kana:
        return "japanese"
    # Korean: Hangul
    if has_hangul:
        return "korean"
    # Chinese: CJK without Kana (Japanese was already caught above)
    if has_cjk:
        return "chinese"
    if has_arabic:
        return "arabic"
    if has_cyrillic:
        return "cyrillic"
    if has_thai:
        return "thai"
    if has_deva:
        return "hindi"
    if has_hebrew:
        return "hebrew"
    return None


# Default genre per script when no API data and no manual override.
# These are the tags that will actually appear in the ZX500 genre browser,
# so they should be both accurate and useful as a browsing category.
_SCRIPT_DEFAULT_GENRE: dict[str, str] = {
    "japanese":  "City Pop",       # the dominant genre on this device; covers J-Pop, City Pop, J-R&B
    "korean":    "K-Pop",
    "chinese":   "C-Pop",
    "arabic":    "Arabic Pop",
    "cyrillic":  "Eastern European",
    "thai":      "Thai Pop",
    "hindi":     "Bollywood",
    "hebrew":    "Israeli Pop",
}


# MK39 Phase I-B2 (Council 2026-05-10) — operator-supplied genre overrides
# from a JSON file. Loaded once at first lookup, cached, refreshed only on
# process restart. Format documented in genre_overrides.json itself.
_OPERATOR_OVERRIDES_CACHE: dict[str, str] | None = None
_OPERATOR_OVERRIDES_LOCK = __import__("threading").Lock()


def _load_operator_overrides() -> dict[str, str]:
    """Load and cache operator-provided artist→genre overrides from
    genre_overrides.json. Council §B2 ABSOLUTE PRIORITY data — checked
    before the in-code dict, cache, and all external lookups.

    Search order: working directory first, then alongside this module.
    Errors during load are logged once and the result is an empty dict
    (the in-code overrides remain effective)."""
    global _OPERATOR_OVERRIDES_CACHE
    if _OPERATOR_OVERRIDES_CACHE is not None:
        return _OPERATOR_OVERRIDES_CACHE
    with _OPERATOR_OVERRIDES_LOCK:
        if _OPERATOR_OVERRIDES_CACHE is not None:
            return _OPERATOR_OVERRIDES_CACHE
        import json as _json, os as _os
        candidates = [
            "genre_overrides.json",
            _os.path.join(_os.path.dirname(__file__), "genre_overrides.json"),
        ]
        loaded: dict[str, str] = {}
        for path in candidates:
            try:
                if _os.path.isfile(path):
                    with open(path, encoding="utf-8") as _f:
                        data = _json.load(_f)
                    overrides = data.get("overrides", {}) if isinstance(data, dict) else {}
                    if isinstance(overrides, dict):
                        # Normalize keys via _norm
                        loaded = {_norm(k): str(v) for k, v in overrides.items()
                                    if isinstance(k, str) and isinstance(v, str)}
                        log.info(f"[Genre/Overrides] loaded {len(loaded)} operator "
                                  f"overrides from {path} (Council Phase I-B2)")
                        break
            except Exception as e:
                log.warning(f"[Genre/Overrides] failed to load {path}: "
                              f"{type(e).__name__}: {e}")
        _OPERATOR_OVERRIDES_CACHE = loaded
        return loaded


def _reset_operator_overrides_cache() -> None:
    """Reset the cache. Used by tests; not called in production."""
    global _OPERATOR_OVERRIDES_CACHE
    with _OPERATOR_OVERRIDES_LOCK:
        _OPERATOR_OVERRIDES_CACHE = None


def _artist_override(artist: str, title: str = "") -> str:
    """
    Returns a manual genre override for a known artist, or a script-detected
    default genre, or "" if neither applies.

    Priority order:
      0. operator-provided genre_overrides.json (MK39 Phase I-B2 — ABSOLUTE PRIORITY)
      1. Exact match in _ARTIST_GENRE_OVERRIDES (normalised artist name)
      2. Partial match in _ARTIST_GENRE_OVERRIDES
      3. Unicode script detection on artist name
      4. Unicode script detection on track title (catches Western artists
         with Japanese/CJK track titles — e.g. a collaboration or anime tie-in)
    """
    an = _norm(artist)

    # 0. operator-provided overrides (Council 2026-05-10 §B2)
    # ABSOLUTE PRIORITY: takes precedence over the in-code dict, the cache,
    # and every external lookup. Loaded once at first call (cached).
    op = _load_operator_overrides()
    if an in op:
        return op[an]

    # 1. Exact match
    if an in _ARTIST_GENRE_OVERRIDES:
        return _ARTIST_GENRE_OVERRIDES[an]

    # 2. Partial match (e.g. "diamond platnumz lolilo" matches "diamond platnumz")
    #    Guard: only match keys that are >= 5 chars — short keys like "ka" or
    #    "mike" would otherwise false-positive on "beeKAy", "volKA", "kaoru
    #    akimoto", "s kiyotAKA", "rmc MIKE", "killer MIKE", etc.
    for key, genre in _ARTIST_GENRE_OVERRIDES.items():
        if len(key) >= 5 and (key in an or an in key):
            return genre

    # 3. Script detection on artist name
    script = _dominant_script(artist)
    if script and script in _SCRIPT_DEFAULT_GENRE:
        return _SCRIPT_DEFAULT_GENRE[script]

    # 4. Script detection on title (catches CJK/Arabic titles by Latin-named artists)
    if title:
        script = _dominant_script(title)
        if script and script in _SCRIPT_DEFAULT_GENRE:
            return _SCRIPT_DEFAULT_GENRE[script]

    return ""

# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

# ==========================================================================
# MK15 FIX-A: Genre cache memory-first singleton with dirty-write flush
# ==========================================================================
# CTO direction: "Stop reloading .genre_cache.json for every track.
# Move into a memory-mapped / singleton. Accessing 4,700-entry JSON via
# standard I/O 25 times a second is killing performance."
#
# Implementation: module-level dict loaded once at first call.
# Writes batched: every GENRE_CACHE_DIRTY_FLUSH_INTERVAL new entries.
# SIGTERM / pipeline shutdown triggers an explicit flush.
# Thread-safe: all mutations go through _genre_cache_lock.
#
# Benefit: O(N) disk read per track → O(1) dict lookup.
# With 25 workers × 377 tracks, this eliminates ~9,425 disk reads per run.
# ==========================================================================

import threading as _gc_thread
import atexit as _gc_atexit

_GENRE_CACHE_SINGLETON: dict | None = None
_GENRE_CACHE_DIRTY: int = 0          # count of unsaved new entries
# MK15-HF3: RLock (reentrant) prevents deadlock when the main thread calls
# flush_genre_cache() while a worker holds the lock via _genre_cache_set().
# threading.Lock() deadlocks when the SAME thread tries to acquire it twice
# (e.g., flush_genre_cache → _flush_genre_cache_locked, both under the lock).
_GENRE_CACHE_LOCK  = _gc_thread.RLock()
GENRE_CACHE_DIRTY_FLUSH_INTERVAL = 50  # write-back every 50 new entries


def _get_genre_cache() -> dict:
    """Return the in-memory cache, loading from disk on first call."""
    global _GENRE_CACHE_SINGLETON
    with _GENRE_CACHE_LOCK:
        if _GENRE_CACHE_SINGLETON is None:
            if config.GENRE_CACHE_FILE.exists():
                try:
                    _GENRE_CACHE_SINGLETON = json.loads(
                        config.GENRE_CACHE_FILE.read_text(encoding="utf-8")
                    )
                    log.debug(
                        f"[GenreCache] Loaded {len(_GENRE_CACHE_SINGLETON)} entries "
                        f"from disk (singleton init)"
                    )
                except Exception as e:
                    log.warning(f"[GenreCache] Failed to load cache: {e}")
                    _GENRE_CACHE_SINGLETON = {}
            else:
                _GENRE_CACHE_SINGLETON = {}
        return _GENRE_CACHE_SINGLETON


def _genre_cache_set(key: str, value: dict) -> None:
    """Set a cache entry and trigger a dirty-write flush if threshold reached."""
    global _GENRE_CACHE_DIRTY, _GENRE_CACHE_SINGLETON
    with _GENRE_CACHE_LOCK:
        # MK15 HOTFIX: access _GENRE_CACHE_SINGLETON directly here — do NOT call
        # _get_genre_cache() which would re-acquire _GENRE_CACHE_LOCK on the same
        # thread, causing a deadlock (threading.Lock is not reentrant).
        # Root cause of post-Phase-1.5+2 freeze confirmed in live run.
        if _GENRE_CACHE_SINGLETON is None:
            _GENRE_CACHE_SINGLETON = {}
        cache = _GENRE_CACHE_SINGLETON
        is_new = key not in cache
        cache[key] = value
        if is_new:
            _GENRE_CACHE_DIRTY += 1
            if _GENRE_CACHE_DIRTY >= GENRE_CACHE_DIRTY_FLUSH_INTERVAL:
                _flush_genre_cache_locked(cache)
                _GENRE_CACHE_DIRTY = 0


def _flush_genre_cache_locked(cache: dict | None = None) -> None:
    """Write the in-memory cache to disk. Call only while holding the lock."""
    c = cache or _GENRE_CACHE_SINGLETON
    if c is None:
        return
    try:
        config.GENRE_CACHE_FILE.write_text(
            json.dumps(c, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.debug(f"[GenreCache] Flushed {len(c)} entries to disk")
    except Exception as e:
        log.warning(f"[GenreCache] Flush failed: {e}")


def flush_genre_cache() -> None:
    """Public flush — call at pipeline end or on SIGTERM."""
    global _GENRE_CACHE_DIRTY
    with _GENRE_CACHE_LOCK:
        _flush_genre_cache_locked()
        _GENRE_CACHE_DIRTY = 0


# Register atexit flush so cache is never lost on clean exit
_gc_atexit.register(flush_genre_cache)


def purge_genre_cache(artist: str | None = None) -> int:
    """
    Purge genre-cache entries so they get re-inferred on next lookup.

    Issue 2 root cause: pre-MK39 poisoned inferences (e.g. "Afrobeats" for
    The Weeknd) persisted because `_artist_override`'s TypeError was silently
    swallowed by a bare except at its call site, so overrides that would have
    corrected the cache were dropped. That call-site bug is fixed (routes
    through `mk39_phase_I_containment.safe_artist_override`), and the MK38.0
    semantic-write freeze correctly blocks new bad writes — but neither
    un-poisons what's already on disk. This purge does that.

    artist=None purges the whole cache (nuclear option). A specific artist
    name purges only that artist's entries (`"<norm artist>|"` key prefix),
    leaving unrelated correct entries untouched.

    Returns the number of entries removed.
    """
    with _GENRE_CACHE_LOCK:
        cache = _get_genre_cache()
        if artist is None:
            n = len(cache)
            cache.clear()
        else:
            prefix = _norm(artist) + "|"
            targets = [k for k in cache if k.split(":", 1)[-1].startswith(prefix)]
            for k in targets:
                del cache[k]
            n = len(targets)
        _flush_genre_cache_locked(cache)
        global _GENRE_CACHE_DIRTY
        _GENRE_CACHE_DIRTY = 0
    return n


def invalidate_genre_cache_singleton() -> None:
    """Force the next _get_genre_cache() call to reload from disk."""
    global _GENRE_CACHE_SINGLETON, _GENRE_CACHE_DIRTY
    with _GENRE_CACHE_LOCK:
        _GENRE_CACHE_SINGLETON = None
        _GENRE_CACHE_DIRTY = 0


# ── Legacy compatibility shims ─────────────────────────────────────────────
# These are kept so existing call sites in the file don't break.
# New code should use _get_genre_cache() / _genre_cache_set() directly.

def _load_genre_cache() -> dict:
    """DEPRECATED: Use _get_genre_cache() instead. Returns the singleton."""
    return _get_genre_cache()


def _save_genre_cache(cache: dict):
    """DEPRECATED: Use _genre_cache_set() + flush_genre_cache() instead."""
    global _GENRE_CACHE_SINGLETON
    with _GENRE_CACHE_LOCK:
        _GENRE_CACHE_SINGLETON = cache
        _flush_genre_cache_locked(cache)


# ---------------------------------------------------------------------------
# Phase 1: fast providers (no mandatory sleep, run in parallel)
# ---------------------------------------------------------------------------

# Genres too broad to be useful — if a provider returns one of these,
# we still accept it as a fallback but try the next provider first.
_BROAD_GENRES = {
    "pop", "rock", "alternative", "electronic", "dance", "african music",
    "african", "world music", "world", "music", "other", "miscellaneous",
    "r&b", "rnb", "rhythm and blues", "urban contemporary", "urban",
    "hip hop", "hip-hop", "rap", "hip-hop/rap", "rap/hip hop",
    "latin", "gospel", "christian", "country", "folk", "classical",
    "jazz", "blues", "soul",
    # FIX 6 (2026-04-17): these broad strings appeared as stale cache
    # values for MF DOOM, Viktor Vaughn, DANGERDOOM etc. — add them so
    # _is_broad() catches them and _should_refetch() can supersede via
    # _artist_override.
    "alternative rock", "hardcore hip-hop", "hardcore hip hop",
    "hardcore rap", "underground hip-hop", "underground hip hop",
    "underground rap",
}

def _is_broad(genre: str) -> bool:
    return genre.strip().lower() in _BROAD_GENRES


# 2026-09-02 — regional/niche genres that a single thin Last.fm crowd tag must
# NOT be allowed to decide on its own. ROOT CAUSE: _lastfm() returns the first
# non-broad toptag with zero vote-count check; track.getInfo toptags carry no
# usable count, so one person tagging an Adele track "afrobeats" made the audit
# rewrite all of Adele "19" to Afrobeats/Amapiano. These genres are heavily
# represented in this library, so a spurious tag looks plausible.
# A genre in this set is only accepted from _fast_lookup when a SECOND source
# corroborates it (Deezer album genre, or an artist override).
_CORROBORATION_REQUIRED_GENRES = {
    "afrobeats", "afrobeat", "amapiano", "amapiano soul", "afro house",
    "afro tech", "afro-tech", "afro pop", "afropop", "afro-fusion", "afro fusion",
    "gqom", "kwaito", "sgija", "3-step", "3 step", "bacardi", "shangaan electro",
    "alte",  # Nigerian-alt genre — kept bleeding onto Western rock/alt artists
}


def _genre_needs_corroboration(genre: str) -> bool:
    return (genre or "").strip().lower() in _CORROBORATION_REQUIRED_GENRES


def _should_refetch(track: dict, cache_entry: dict | None) -> bool:
    """
    Decide whether a cached genre entry should be invalidated and retried.

    Returns True when:
      - The cache entry is missing entirely, OR
      - The stored genre is empty (Phase-4 FIX 17 behaviour), OR
      - The stored genre is a broad/useless bucket AND an _artist_override
        exists for this artist — meaning the override would have produced
        a better result had the entry been treated as a miss.

    The third case fixes the stale-cache bug where previous API runs stored
    e.g. MF DOOM → "Alternative Rock" or Cochise → "Rap/Hip Hop" before the
    override table was expanded.  Because enrich_metadata() only retried
    empty entries, the override never got a chance to supersede those
    wrong-but-present values.
    """
    if not cache_entry:
        return True
    cached_genre = (cache_entry.get("genre") or "").strip()
    if not cached_genre:
        return True
    # Stored genre is present but broad — refetch only if the override
    # table has something specific to offer for this artist (or for the
    # title's script).  Otherwise leave the entry alone; re-querying APIs
    # that already returned a broad value is wasted effort.
    if _is_broad(cached_genre):
        override = _artist_override(track.get("artist", ""), track.get("title", ""))
        if override and not _is_broad(override):
            return True
    return False

_LASTFM_SKIP = {
    "seen live", "favourites", "favorite", "my favorite",
    "cool", "awesome", "love", "beautiful", "amazing",
}


def _lastfm(artist: str, title: str) -> dict:
    """Last.fm track.getInfo — best genre coverage, free, fast."""
    if not config.LASTFM_API_KEY:
        return {}
    for attempt in range(3):
        try:
            r = requests.get(
                "https://ws.audioscrobbler.com/2.0/",
                params={
                    "method":      "track.getInfo",
                    "api_key":     config.LASTFM_API_KEY,
                    "artist":      artist,
                    "track":       title,
                    "autocorrect": 1,
                    "format":      "json",
                },
                timeout=5,
            )
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                return {}
            track = r.json().get("track", {})
            if not track:
                return {}

            result = {}
            for tag in track.get("toptags", {}).get("tag", []):
                name = tag.get("name", "").strip()
                if name.lower() not in _LASTFM_SKIP and len(name) > 1:
                    normed = _normalize_genre(name)
                    if normed:  # skip if normalization maps to empty (Unknown etc.)
                        result["genre"] = normed
                        if not _is_broad(normed):
                            break
                    # if broad, keep scanning for a more specific tag

            published = track.get("wiki", {}).get("published", "")
            m = re.search(r"\b(19|20)\d{2}\b", published)
            if m:
                result["year"] = m.group(0)

            return result
        except Exception:
            return {}
    return {}


def _deezer(artist: str, title: str) -> dict:
    """Deezer — good year coverage, free, no key required."""
    for attempt in range(3):
        try:
            r = requests.get(
                "https://api.deezer.com/search/track",
                params={"q": f'track:"{title}" artist:"{artist}"', "limit": 5},
                timeout=5,
            )
            time.sleep(0.1)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                return {}
            data = r.json().get("data", [])
            if not data:
                return {}

            # Artist confidence check: only use a result whose artist name
            # overlaps substantially with the queried artist.  Deezer fuzzy-
            # matches on title and returns whatever artist it finds — "Magumba"
            # (Khadeair, SA) otherwise matched a J-Pop track, "Rio" (Detroit)
            # matched a Tanzanian artist, etc.
            #
            # Rules:
            #   1. Short artist names (≤ 4 chars, e.g. "Rio", "Jme") require
            #      an exact match — too ambiguous for partial matching.
            #   2. Longer names: accept if either name contains the other, or
            #      they share ≥ 50% of tokens ("DJ Tira" → "Tira" passes).
            #   3. If no candidate passes, return {} — discard all results.
            artist_norm = _norm(artist)
            track = None
            _exact_only = len(artist_norm.replace(" ", "")) <= 4
            for candidate in data:
                cand_artist = _norm(candidate.get("artist", {}).get("name", ""))
                if _exact_only:
                    # Short names: must match exactly
                    if artist_norm == cand_artist:
                        track = candidate
                        break
                else:
                    artist_tokens = set(artist_norm.split())
                    cand_tokens   = set(cand_artist.split())
                    overlap = artist_tokens & cand_tokens
                    if (artist_norm in cand_artist
                            or cand_artist in artist_norm
                            or (overlap and len(overlap) / max(len(artist_tokens), 1) >= 0.5)):
                        track = candidate
                        break
            if track is None:
                return {}  # No confident artist match — discard all results

            release = track.get("release_date", "")
            if release:
                result["year"] = release[:4]

            album_id = track.get("album", {}).get("id")
            if album_id:
                ar = requests.get(
                    f"https://api.deezer.com/album/{album_id}", timeout=5
                )
                time.sleep(0.1)
                if ar.status_code == 200:
                    genres = ar.json().get("genres", {}).get("data", [])
                    if genres:
                        # Walk the genre list: prefer first non-broad specific genre
                        for _g in genres:
                            _gn = _normalize_genre(_g["name"])
                            if _gn and not _is_broad(_gn):
                                result["genre"] = _gn
                                break
                        if "genre" not in result and genres:
                            result["genre"] = _normalize_genre(genres[0]["name"])

            return result
        except Exception:
            return {}
    return {}


def _fast_lookup(artist: str, title: str) -> dict:
    """
    Phase 1: try Last.fm then Deezer (and manual override first).
    Returns a dict containing only keys where a real value was found.
    A missing key means 'provider had no data' — not an empty string.
    This distinction is critical for the Phase 2 filter.
    """
    result: dict = {}

    # Check manual artist override (and Unicode script detection) before hitting APIs
    override = _artist_override(artist, title)
    if override:
        result["genre"] = override
        # Still fetch year from APIs even if genre is overridden
        lfm = _lastfm(artist, title)
        if lfm.get("year"):
            result["year"] = lfm["year"]
        if not result.get("year"):
            dz = _deezer(artist, title)
            if dz.get("year"):
                result["year"] = dz["year"]
        return result

    lfm = _lastfm(artist, title)
    _lfm_genre = lfm.get("genre") or ""
    # 2026-09-02 — regional-genre corroboration gate. If Last.fm handed back a
    # niche/regional genre (Afrobeats/Amapiano/Alte/...), do NOT trust it on its
    # own — a single crowd tag is enough to trigger _lastfm(). Defer the genre
    # decision to Deezer below; only keep the Last.fm regional genre if Deezer
    # agrees (same genre, or Deezer's own genre is also regional).
    _lfm_needs_corrob = bool(_lfm_genre) and _genre_needs_corroboration(_lfm_genre)
    if _lfm_genre and not _lfm_needs_corrob:
        result["genre"] = _lfm_genre
    if lfm.get("year"):
        result["year"] = lfm["year"]

    if "genre" in result and "year" in result:
        return result

    dz = _deezer(artist, title)
    dz_genre = dz.get("genre") or ""
    if _lfm_needs_corrob and "genre" not in result:
        # keep the Last.fm regional genre only with corroboration
        if dz_genre and (
            dz_genre.strip().lower() == _lfm_genre.strip().lower()
            or _genre_needs_corroboration(dz_genre)
        ):
            result["genre"] = _lfm_genre
        elif dz_genre:
            result["genre"] = dz_genre           # Deezer disagrees — trust Deezer
        else:
            log.debug(
                f"  [Genre/Corrob] dropped uncorroborated Last.fm genre "
                f"{_lfm_genre!r} for {artist!r} — {title!r} (no Deezer support)"
            )
            # genre stays unset — falls through to Phase 1.5/2, override, or blank
    if dz_genre and "genre" not in result:
        result["genre"] = dz_genre
    if dz.get("year") and "year" not in result:
        result["year"] = dz["year"]

    return result   # only contains keys with real values


# ---------------------------------------------------------------------------
# Phase 2: slow providers (rate-limited, sequential, misses only)
# ---------------------------------------------------------------------------

def _musicbrainz(artist: str, title: str) -> dict:
    """
    MusicBrainz — authoritative release dates, community genre tags. 1 req/s.

    MK10 ISSUE-B fix: previous code read `first-release-date` from
    recordings[0] (the highest-scoring search result), which is often a
    re-master, deluxe edition, or streaming release — not the original.
    Example: "Superstition" by Stevie Wonder returned 2018 (Motown 50th
    Anniversary remaster) instead of 1972 (Talking Book original).

    Fix: walk ALL returned recordings (up to limit=5), collect every
    `first-release-date` value, and return the EARLIEST one.  This gives
    the original release year per the CTO's directive.

    Also: aggregate tags from all recordings (weighted by vote count) so
    we get a richer genre signal than from a single recording.

    MK37/P0-4: enforce offline capability boundary. If OFFLINE_MODE=True,
    raises OfflineProviderViolation. MK37/S5: provider health recording
    around the call to drive cooldown logic.
    """
    # MK37/P0-4: offline capability check
    try:
        from mk37_wiring import (
            offline_provider_capability_check, OfflineProviderViolation,
            is_provider_available, record_provider_success,
            record_provider_failure,
        )
        offline_provider_capability_check("musicbrainz")
        if not is_provider_available("musicbrainz"):
            return {}
    except ImportError:
        record_provider_success = lambda _n: None
        record_provider_failure = lambda _n, _ec=None: None
    except Exception as _e:
        # OfflineProviderViolation propagates up — must NOT be swallowed
        if "OfflineProviderViolation" in type(_e).__name__:
            raise
        return {}
    try:
        r = requests.get(
            "https://musicbrainz.org/ws/2/recording/",
            params={
                "query": f'recording:"{title}" AND artist:"{artist}"',
                "fmt":   "json",
                "limit": 5,   # MK10: fetch more results to find earliest date
            },
            headers={"User-Agent": "WalkmanSyncAgent/10.0 (contact@example.com)"},
            timeout=8,
        )
        time.sleep(1.1)   # MusicBrainz enforces 1 req/s — must sleep after every call
        if r.status_code != 200:
            return {}

        recordings = r.json().get("recordings", [])
        if not recordings:
            return {}

        result = {}

        # MK10 ISSUE-B: collect ALL first-release-dates and pick the earliest.
        # A recording with a date like "2018" for a 1972 track is a remaster;
        # the original always has the smallest year across all recordings that
        # match the same song.  We require a valid 4-digit year matching the
        # 19xx or 20xx pattern to filter out nonsense values.
        _year_re = re.compile(r"^(19|20)\d{2}")
        candidate_years: list[str] = []
        for rec in recordings:
            frd = rec.get("first-release-date", "")
            if frd and _year_re.match(frd):
                candidate_years.append(frd[:4])

        if candidate_years:
            result["year"] = min(candidate_years)   # earliest = original release

        # Aggregate genre tags across all recordings, weighted by vote count.
        tag_votes: dict[str, int] = {}
        for rec in recordings:
            for tag in rec.get("tags", []):
                name  = tag.get("name", "").strip()
                count = int(tag.get("count", 1))
                if name:
                    tag_votes[name] = tag_votes.get(name, 0) + count
            # Also check release-level genres
            for rel in rec.get("releases", []):
                for g in rel.get("genres", []):
                    name  = g.get("name", "").strip()
                    count = int(g.get("count", 1))
                    if name:
                        tag_votes[name] = tag_votes.get(name, 0) + count

        if tag_votes:
            best_tag = max(tag_votes, key=lambda k: tag_votes[k])
            result["genre"] = best_tag.title()

        return result
    except Exception:
        return {}


def _discogs(artist: str, title: str) -> dict:
    """Discogs — deepest style taxonomy. 1 req/s conservative."""
    # MK37/P0-4 + S5: offline capability + provider health check
    try:
        from mk37_wiring import (
            offline_provider_capability_check, is_provider_available,
        )
        offline_provider_capability_check("discogs")
        if not is_provider_available("discogs"):
            return {}
    except ImportError:
        pass
    if not config.DISCOGS_TOKEN:
        return {}
    try:
        r = requests.get(
            "https://api.discogs.com/database/search",
            params={"q": f"{artist} {title}", "type": "release", "per_page": 3},
            headers={
                "Authorization": f"Discogs token={config.DISCOGS_TOKEN}",
                "User-Agent":    "WalkmanSyncAgent/9.0",
            },
            timeout=8,
        )
        time.sleep(1.0)
        if r.status_code != 200:
            return {}

        results = r.json().get("results", [])
        if not results:
            return {}

        result = {}
        hit    = results[0]

        year = str(hit.get("year", ""))
        if re.match(r"(19|20)\d{2}", year):
            result["year"] = year

        styles = hit.get("style", [])
        genres = hit.get("genre", [])
        if styles:
            result["genre"] = styles[0]
        elif genres:
            result["genre"] = genres[0]

        return result
    except Exception:
        return {}


# ──────────────────────────────────────────────────────────────────────────
# Rev 14.9.6 A1 — Session-level artist→genre cache
# ──────────────────────────────────────────────────────────────────────────
# LOG_16 evidence: of 611 Phase 3 Google scrapes in one run, 186 (30%) were
# duplicates of an artist we'd already scraped — "LO-FI BEATS" hit 22
# times, "Lofi Hip-Hop Beats" 14 times, "Samthing Soweto" 5 times.  Each
# scrape costs ~5 seconds (2s sleep + ~3s request).  186 * 5s = 15 minutes
# wasted per run, plus the IP-ban risk the noise creates.
#
# The fix: a session-level dict keyed by normalized artist name only.
# When we successfully scrape genre G for artist A, every subsequent
# Phase 3 call for the same artist returns G immediately — no HTTP
# request, no sleep.  Negative results (empty {}) are also cached so we
# don't repeatedly retry artists Google has nothing on.
#
# Why session-level not disk-persistent: artist→genre relationships are
# stable but the Google scrape is the LAST FALLBACK before the AI tier;
# if a scrape returns junk for one track, persisting it would poison
# every future track by that artist across runs.  Session-only means
# next run re-scrapes once and either confirms or replaces the value.
#
# Thread-safe via a Lock because Phase 3 fires from the same 25-worker
# pool as Phase 1.
import threading as _ag_threading
_ARTIST_GENRE_CACHE: dict[str, dict] = {}
_ARTIST_GENRE_LOCK = _ag_threading.Lock()


def _artist_cache_key(artist: str) -> str:
    """Normalised artist name as session-cache key.  Lowercase, collapsed
    whitespace.  Empty string for unknown."""
    return _norm(artist).lower()


def _artist_cache_get(artist: str):
    """Return cached {'genre': ...} dict for artist (or {} for cached negative
    or None for cache miss).

    MK38.0 SEMANTIC WRITE BARRIER: under freeze, the existing in-memory
    artist cache may already be poisoned (per the "everything became
    Afrobeats" incident — recursive contamination from a prior run).
    Returning poisoned entries here would defeat the write-side block at
    _artist_cache_put because downstream code uses the returned genre to
    populate other paths. Under freeze we return None for any entry whose
    source isn't canonical, forcing the caller to either fetch fresh from
    a canonical source or proceed without an artist-genre hint.
    """
    key = _artist_cache_key(artist)
    if not key:
        return None
    with _ARTIST_GENRE_LOCK:
        cached = _ARTIST_GENRE_CACHE.get(key)
    try:
        from mk38_0_semantic_write_barrier import (
            is_frozen as _frozen, cache_read_allowed,
        )
        if _frozen() and cached:
            src = cached.get("_source") or cached.get("source") or "artist_cache_inferred"
            if not cache_read_allowed(src):
                return None
    except ImportError:
        pass
    return cached


def _artist_cache_put(artist: str, result: dict) -> None:
    # MK38.0 SEMANTIC WRITE BARRIER: artist→genre cache is the cross-track
    # propagation vector. One sparse-artist inference becomes a cache entry
    # that then feeds every subsequent track by the same artist — the exact
    # mechanism behind the "everything became Afrobeats" incident. Under
    # freeze, refuse to populate the cache from inference sources. Reads
    # of the cache are separately gated via cache_read_allowed().
    try:
        from mk38_0_semantic_write_barrier import is_frozen as _frozen
        if _frozen():
            # Refuse to cache. Note: this means subsequent calls re-fetch
            # from upstream, which is intentional — if upstream returns a
            # canonical answer, the canonical write path bypasses the freeze
            # cleanly. We're stopping the *propagation* of inference, not
            # the *fetching*.
            return
    except ImportError:
        pass
    key = _artist_cache_key(artist)
    if not key:
        return
    with _ARTIST_GENRE_LOCK:
        # Don't overwrite a positive (genre present) with a negative ({}).
        existing = _ARTIST_GENRE_CACHE.get(key)
        if existing and existing.get("genre"):
            if not result.get("genre"):
                return
        _ARTIST_GENRE_CACHE[key] = result


def _google_genre(artist: str, title: str) -> dict:
    """
    Phase 3 fallback: scrape Google search for genre.
    Parses the knowledge panel / JSON-LD / featured snippet.
    Returns {"genre": str} or {} on miss/error. Throttled to 1 req/2s.

    Rev 14.9.6 A1 — consults the session-level artist cache before any HTTP
    request.  Subsequent calls for the same artist resolve in microseconds.
    """
    # ── Cache check ─────────────────────────────────────────────────────
    cached = _artist_cache_get(artist)
    if cached is not None:
        log.debug(f"  [Google/cache] artist hit: {artist!r} -> {cached}")
        return dict(cached)  # defensive copy

    def _ret(result: dict) -> dict:
        """Cache and return — single exit point so we never miss a path."""
        _artist_cache_put(artist, result)
        return result

    try:
        import json as _jg
        import re as _rg

        query   = f"{artist} {title} genre music"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(
            "https://www.google.com/search",
            params={"q": query, "hl": "en", "gl": "us", "num": "3"},
            headers=headers,
            timeout=8,
        )
        time.sleep(2.0)  # throttle — 1 req/2s to avoid 429
        if r.status_code != 200:
            return {}
        html = r.text
        result: dict = {}

        # 1. JSON-LD schema.org — most reliable
        jld_pattern = r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
        for jld in _rg.findall(jld_pattern, html, _rg.S):
            try:
                data  = _jg.loads(jld)
                items = data.get("@graph", [data]) if isinstance(data, dict) else [data]
                if not isinstance(items, list):
                    items = [items]
                for item in items:
                    g = item.get("genre") or ""
                    if isinstance(g, list):
                        g = g[0] if g else ""
                    if isinstance(g, str) and g:
                        normed = _normalize_genre(g)
                        if normed and not _is_broad(normed):
                            return _ret({"genre": normed})
            except Exception:
                continue

        # 2. Knowledge panel "Genre:" row (various Google HTML patterns)
        for pat in [
            r'data-attrid=["\'][^"\']*genre[^"\']*["\'][^>]*>[^<]*<[^>]+>([^<]{3,40})<',
            r'"genre"\s*:\s*"([^"]{3,40})"',
            r'Genre</span>[^<]*<span[^>]*>([^<]{3,40})</span>',
        ]:
            m = _rg.search(pat, html, _rg.I)
            if m:
                normed = _normalize_genre(m.group(1).strip())
                if normed and not _is_broad(normed):
                    return _ret({"genre": normed})
                elif normed and not result.get("genre"):
                    result["genre"] = normed  # broad — keep as weak candidate

        # 3. Wikipedia infobox via featured snippet
        wm = _rg.search(r'Genre[s]?[:\s]+([A-Z][a-zA-Z ,/&\-]{3,50}?)(?:<|\n|\|)', html)
        if wm:
            raw = wm.group(1).strip().split(",")[0].strip()
            normed = _normalize_genre(raw)
            if normed and not result.get("genre"):
                result["genre"] = normed

        return _ret(result)

    except Exception as exc:
        log.debug(f"  [Google] genre scrape failed: {exc}")
        return {}  # network error — do NOT cache; retry next call may succeed

# ── Gemini genre advisor ──────────────────────────────────────────────────────
# Used in two places:
#   1. _gemini_genre_fallback()  — Phase 4: when ALL other providers failed
#   2. _gemini_normalize_unusual_genre() — when a raw genre string looks non-standard
# 2026-08-22: gemini-2.0-flash decommissioned (404 → "use gemini-3.6-flash").
_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
_GEMINI_GENRE_LOCK     = __import__("threading").Lock()
_GEMINI_GENRE_CACHE: dict = {}  # (artist, title) → {"genre": str} or {}

# REV 14.6 — Gemini 429 circuit breaker.
#
# The 2026-04-19 log showed 546/546 Phase 1.5+2 tracks falling through to
# Gemini, with >95% returning HTTP 429 "Too Many Requests" — Gemini's free
# tier is ~60 requests/minute and bursty parallel genre workers blew past
# that limit within the first minute and stayed there for ~26 minutes of
# wasted API calls.
#
# After _GEMINI_429_THRESHOLD consecutive 429s we disable Gemini for the
# rest of the session (log once at WARNING; subsequent calls early-return).
# A single 200 OK resets the counter — if the quota window rolls over we
# automatically pick Gemini back up.
_GEMINI_429_STREAK       = 0      # consecutive 429s; reset on any 200

def reset_session_state() -> None:
    """
    MK26 H2 (A4): Reset Gemini circuit breaker state at session startup.
    Evidence (LOG_6_5_26): Session opened with streak=7 (pre-tripped from prior run).
    Circuit breaker state should not carry across process restarts — each new
    process gets a fresh 429 budget. Carry-over only makes sense if the quota
    period hasn't expired; a 4h+ gap between runs means Gemini quota has reset.
    Called by main.py at startup before any pipeline stages.
    """
    global _GEMINI_429_STREAK, _GEMINI_DISABLED, _GEMINI_TRIPPED_AT
    global _GEMINI_HALF_OPEN, _GEMINI_PROBE_DELAY_S, _GEMINI_PROBE_FAILURES
    import time as _t_h2
    # Only reset if enough time has passed for quota to have likely refreshed
    # Gemini free tier resets daily; if tripped_at was >4h ago → fresh session
    _elapsed = _t_h2.time() - _GEMINI_TRIPPED_AT if _GEMINI_TRIPPED_AT else 9999
    if _GEMINI_DISABLED and _elapsed > 4 * 3600:
        log.info(
            f"[Gemini/Session] Resetting circuit breaker state — "
            f"was tripped {_elapsed/3600:.1f}h ago (>4h = quota likely refreshed). "
            f"Starting fresh session."
        )
        _GEMINI_429_STREAK    = 0
        _GEMINI_DISABLED      = False
        _GEMINI_TRIPPED_AT    = 0.0
        _GEMINI_HALF_OPEN     = False
        _GEMINI_PROBE_DELAY_S = 300.0  # reset to base probe delay
        try:
            _GEMINI_PROBE_FAILURES = 0
        except NameError:
            pass
    elif _GEMINI_DISABLED:
        log.info(
            f"[Gemini/Session] Circuit still tripped ({_elapsed/3600:.1f}h < 4h) "
            f"— keeping pre-trip state (quota likely still exhausted)."
        )
    else:
        log.debug("[Gemini/Session] Circuit clean at startup — no reset needed.")
_GEMINI_DISABLED         = False
# MK17/MK18 FIX-F6: half-open probe state
# APPROACH: simple flag + monotonic clock. No token bucket (overkill for this pattern).
# After GEMINI_HALF_OPEN_DELAY_S, one probe allowed; success→CLOSED, fail→OPEN.
_GEMINI_HALF_OPEN        = False
_GEMINI_TRIPPED_AT       = 0.0
GEMINI_HALF_OPEN_DELAY_S = 300.0
# MK20 NG2: exponential probe backoff (prevents 84-probe storms)
_GEMINI_PROBE_FAILURES   = 0
_GEMINI_PROBE_DELAY_S    = 300.0   # starts at 300s; multiplied on each probe fail
# MK20 G2: Probe failure tracking for exponential backoff.
# ROOT CAUSE: probe failure resets HALF_OPEN=False but didn't update TRIPPED_AT,
# so the next probe window opened again in 5min → immediate 429 → streak++.
# The streak reached 107+ across a 2-hour run.
# FIX: each probe failure: reset HALF_OPEN + update TRIPPED_AT + multiply delay.
_GEMINI_PROBE_FAILURES   = 0      # consecutive probe failures this session
_GEMINI_PROBE_DELAY_S    = 300.0  # current probe interval (grows on failure)

# MK20 G3+G4: Groq adaptive throttle + enrichment fail-open
import threading as _gc_threading
_GROQ_429_WINDOW: list       = []   # G3: timestamps of recent Groq 429s
_GROQ_ACTIVE_WORKERS: list   = [0]  # G3: mutable worker count [current]
_GROQ_CALLS_THIS_RUN: list   = [0]  # G3: per-run Groq call counter
_GROQ_LOCK                   = _gc_threading.Lock()
_ENRICH_ALL_FAIL_STREAK: list = [0] # G4: consecutive all-providers-failed count
_FAIL_OPEN_WARNED:       list = [False]  # G4-MK15: gate log to once per session


def _groq_record_429_and_get_workers() -> int:
    """
    MK20 G3: Record a Groq 429. Returns recommended worker count.
    ROOT CAUSE: 25 workers hitting Groq's 30-req/min limit causes
    perpetual 429 cycling. Fix: detect burst → halve workers.
    """
    import time as _grt
    now = _grt.monotonic()
    window_s = 30.0
    with _GROQ_LOCK:
        _GROQ_429_WINDOW.append(now)
        # Prune old entries
        while _GROQ_429_WINDOW and now - _GROQ_429_WINDOW[0] > window_s:
            _GROQ_429_WINDOW.pop(0)
        try:
            _thresh = int(getattr(config, "GROQ_THROTTLE_DETECTION_WINDOW", 5))
            _min_w  = int(getattr(config, "GROQ_THROTTLE_MIN_WORKERS", 3))
        except Exception:
            _thresh, _min_w = 5, 3
        if len(_GROQ_429_WINDOW) >= _thresh and _GROQ_ACTIVE_WORKERS[0] > _min_w:
            new_w = max(_min_w, _GROQ_ACTIVE_WORKERS[0] // 2)
            log.warning(
                f"  [AI/Budget] GroqWorkerReduced GROQ_THROTTLE_APPLIED — "
                f"{len(_GROQ_429_WINDOW)} 429s in {window_s:.0f}s; "
                f"workers {_GROQ_ACTIVE_WORKERS[0]}→{new_w} "
                f"(MK21 G3 adaptive controller, floor={_min_w})"
            )
            _GROQ_ACTIVE_WORKERS[0] = new_w
        return _GROQ_ACTIVE_WORKERS[0]
# MK14 FIX-C: Reduced from 20 → 5.
# Log showed 80 Gemini 429s → 800s (13.3 min) wasted before Groq took over.
# At 20 consecutive × 10s penalty = 200s dead time per trigger.
# At 5 consecutive × 10s penalty = 50s — trips 4× faster, saves ~150s per session.
# Exponential backoff (see below) makes early retries fast and late ones slower.
_GEMINI_429_THRESHOLD    = getattr(config, "GEMINI_429_CIRCUIT_THRESHOLD", 5)
_GEMINI_CIRCUIT_ENABLED  = getattr(config, "GEMINI_CIRCUIT_BREAKER_ENABLED", True)


def _gemini_circuit_should_skip() -> bool:
    """True if the Gemini breaker is OPEN and we should not call Gemini.
    MK18 FIX-F6 / MK20 G2: half-open probe with exponential backoff.

    MK20 G2 FIX — ROOT CAUSE: _GEMINI_HALF_OPEN was never reset to False after
    a probe failure — only after a probe success. So the probe window kept
    opening every cooldown interval even though Gemini was still quota-exhausted.
    FIX: probe failure resets HALF_OPEN=False AND updates TRIPPED_AT to now(),
    multiplying the delay by GEMINI_PROBE_BACKOFF_MULTIPLIER (default 3×).
    After GEMINI_PROBE_MAX_FAILURES probe failures, session-ban Gemini entirely.
    """
    if not _GEMINI_CIRCUIT_ENABLED:
        return False
    global _GEMINI_HALF_OPEN, _GEMINI_DISABLED, _GEMINI_TRIPPED_AT, \
           _GEMINI_PROBE_FAILURES, _GEMINI_PROBE_DELAY_S
    with _GEMINI_GENRE_LOCK:
        if not _GEMINI_DISABLED:
            return False
        # Session-banned: too many probe failures → stop probing
        _max_probe_fails = int(getattr(config, "GEMINI_PROBE_MAX_FAILURES", 3))
        if _GEMINI_PROBE_FAILURES >= _max_probe_fails:
            return True   # session-banned: no more probes
        elapsed = __import__("time").monotonic() - _GEMINI_TRIPPED_AT
        if elapsed >= _GEMINI_PROBE_DELAY_S and not _GEMINI_HALF_OPEN:
            _GEMINI_HALF_OPEN = True
            log.info(f"[Gemini/CB] HALF_OPEN: {elapsed:.0f}s elapsed — sending probe")
            return False   # allow probe
        return True


def _gemini_circuit_record(status_code: int) -> None:
    """Feed the breaker one HTTP status.  Trips at threshold consecutive 429s."""
    global _GEMINI_429_STREAK, _GEMINI_DISABLED
    global _GEMINI_HALF_OPEN, _GEMINI_TRIPPED_AT, _GEMINI_PROBE_FAILURES, _GEMINI_PROBE_DELAY_S
    if not _GEMINI_CIRCUIT_ENABLED:
        return
    with _GEMINI_GENRE_LOCK:
        if status_code == 429:
            _GEMINI_429_STREAK += 1
            if (_GEMINI_429_STREAK >= _GEMINI_429_THRESHOLD
                    and not _GEMINI_DISABLED):
                _GEMINI_DISABLED   = True
                _GEMINI_TRIPPED_AT = __import__("time").monotonic()
                log.warning(
                    f"  [Gemini/CircuitBreaker] Disabling Gemini for this "
                    f"session: {_GEMINI_429_STREAK} consecutive 429s "
                    f"(free-tier quota exhausted).  Will retry next run."
                )
            # MK20 G2: If this 429 was a probe attempt (HALF_OPEN was set),
            # reset HALF_OPEN and apply exponential backoff to next probe window.
            if _GEMINI_HALF_OPEN:
                _GEMINI_HALF_OPEN   = False
                _GEMINI_PROBE_FAILURES += 1
                _backoff_mult = float(getattr(config, "GEMINI_PROBE_BACKOFF_MULTIPLIER", 3.0))
                _max_delay    = 3600.0   # cap at 1 hour
                _GEMINI_PROBE_DELAY_S = min(
                    _GEMINI_PROBE_DELAY_S * _backoff_mult, _max_delay
                )
                _GEMINI_TRIPPED_AT = __import__("time").monotonic()   # restart cooldown
                _max_fails = int(getattr(config, "GEMINI_PROBE_MAX_FAILURES", 3))
                log.warning(
                    f"  [Gemini/CB] PROBE_BACKOFF_EXTENDED GeminiProbeBackoff — "
                    f"probe #{_GEMINI_PROBE_FAILURES}/{_max_fails} failed; "
                    f"next window in {_GEMINI_PROBE_DELAY_S/60:.0f}min "
                    f"(MK21 G2: exponential backoff {_GEMINI_PROBE_DELAY_S:.0f}s)"
                )
        elif 200 <= status_code < 300:
            # any success clears the streak and re-enables
            if _GEMINI_429_STREAK or _GEMINI_DISABLED:
                log.debug(f"  [Gemini/CircuitBreaker] 200 received after "
                          f"{_GEMINI_429_STREAK} failures — reset")
            _GEMINI_429_STREAK = 0
            _GEMINI_DISABLED = False


def _claude_genre_fallback(artist: str, title: str) -> dict:
    """
    Phase 4: Ask AI to infer genre from artist + title when all APIs failed.
    Uses Gemini's broad training knowledge of music genres, with Groq
    (Llama-3) as a fallback when Gemini's free-tier quota is exhausted.

    Rev 14.9.6 A2 — When the Gemini circuit breaker has tripped (typical
    in LOG_16 after ~20 consecutive 429s), this used to return {} and
    every Phase 4 call was wasted.  Now we fall through to ai_helper's
    call_ai waterfall, which round-robins Groq's Llama-3.3-70b →
    llama-4-scout → llama-3.1-8b before giving up.  Groq's free tier has
    much higher RPM allowances than Gemini's free tier.

    Returns {"genre": str} or {} on failure/uncertainty.
    Results are cached per (artist, title) to avoid duplicate calls.

    MK38.0 SEMANTIC WRITE BARRIER: This is the canonical AI-inference
    fallback path. Under freeze (default-ON) it short-circuits and returns
    {} — callers see "AI couldn't infer" and don't propagate. The
    "everything became Afrobeats" incident is consistent with this path
    being a primary contamination source: when upstream APIs fail at scale,
    Phase 4 is repeatedly called, AI hallucinates a plausible-sounding
    genre, and that hallucination gets cached + amplified.
    """
    # MK38.0 short-circuit
    try:
        from mk38_0_semantic_write_barrier import is_frozen as _frozen
        if _frozen():
            return {}
    except ImportError:
        pass

    cache_key = f"fallback:{_cache_key(artist, title)}"
    if cache_key in _GEMINI_GENRE_CACHE:
        return _GEMINI_GENRE_CACHE[cache_key]

    # MK11 ISSUE-C+D: Title-keyword pre-classifier (runs before any AI call)
    # Catches obvious cases that APIs and AI consistently miss: Jersey Club,
    # Marching Band, Lo-Fi, Chopped & Screwed, Anime Soundtrack, etc.
    _title_lc  = (title  or "").lower()
    _artist_lc = (artist or "").lower()
    _TITLE_KEYWORD_GENRES = [
        # Jersey Club — nearly always in the title
        ("jersey club",         "Jersey Club"),
        ("jclub",               "Jersey Club"),
        # Chopped & Screwed
        ("chopped not slopped", "Chopped & Screwed"),
        ("chopnotslop",         "Chopped & Screwed"),
        ("chop not slop",       "Chopped & Screwed"),
        ("chopped & screwed",   "Chopped & Screwed"),
        ("chopped not slop",    "Chopped & Screwed"),
        # Lo-Fi / Slowed
        ("lofi",                "Lo-Fi Hip-Hop"),
        ("lo-fi",               "Lo-Fi Hip-Hop"),
        ("lo fi",               "Lo-Fi Hip-Hop"),
        ("slowed reverb",       "Lo-Fi Hip-Hop"),
        ("slowed + reverb",     "Lo-Fi Hip-Hop"),
        ("vaporwave",           "Vaporwave"),
        # Marching Band — HBCU bands always put it in the title or artist
        ("marching band",       "Marching Band"),
        ("sonic boom",          "Marching Band"),   # Jackson State branding
        ("hbcu",                "Marching Band"),
        # Anime
        ("anime ost",           "Anime Soundtrack"),
        ("anime theme",         "Anime Soundtrack"),
        ("attack on titan",     "Anime Soundtrack"),
        ("guren no",            "Anime Soundtrack"),
        ("meets metal",         "Metal"),
        # Phonk / Drill clearly stated
        ("phonk",               "Phonk"),
        ("drill",               "Drill"),
        # Brazilian funk
        ("funk carioca",        "Funk Carioca"),
        ("baile funk",          "Funk Carioca"),
        ("faz de chicote",      "Funk Carioca"),   # Brazilian TikTok funk
    ]
    _ARTIST_KEYWORD_GENRES = [
        ("jackson state",       "Marching Band"),
        ("sonic boom of",       "Marching Band"),
        ("grambling",           "Marching Band"),
        ("bethune cookman",     "Marching Band"),
        ("southern university", "Marching Band"),
        ("prairie view",        "Marching Band"),
    ]
    _preclass = ""
    for kw, gl in _TITLE_KEYWORD_GENRES:
        if kw in _title_lc:
            _preclass = gl
            break
    if not _preclass:
        for kw, gl in _ARTIST_KEYWORD_GENRES:
            if kw in _artist_lc:
                _preclass = gl
                break
    if _preclass:
        log.debug(f"  [GenrePreClass] {artist!r} — {title!r} → {_preclass!r} (keyword)")
        return {"genre": _preclass}

    # Build the genre-inference prompt — MK11: overhauled to prevent Hip-Hop defaulting.
    # Previous prompt returned Hip-Hop for HBCU bands, Arabic music, Jersey Club tracks,
    # Lo-Fi compilations. Fix: explicit anti-default rules + comprehensive vocabulary.
    prompt = (
        f"You are a world-class music genre expert with encyclopedic knowledge of "
        f"global music, including African subgenres, diaspora music, internet "
        f"microgenres, and niche electronic scenes.\n\n"
        f"Classify this track into its most specific, accurate genre:\n"
        f"Artist: {artist}\n"
        f"Title: {title}\n\n"
        f"CRITICAL RULES:\n"
        f"  1. NEVER default to Hip-Hop unless it is genuinely rap/hip-hop. "
        f"     Returning Hip-Hop for uncertain tracks is WRONG.\n"
        f"  2. HBCU / Marching Bands (Jackson State, Grambling, Bethune, Southern): "
        f"     ALWAYS return 'Marching Band'.\n"
        f"  3. Arabic/Middle Eastern/North African artists: 'Arabic Music', 'Rai', "
        f"     'Gnawa', or 'World Music' — NOT Hip-Hop.\n"
        f"  4. Lo-Fi / Slowed+Reverb / Vaporwave / Phonk: use those exact names.\n"
        f"  5. Jersey Club / Chopped and Screwed: use those exact genre names.\n"
        f"  6. African specifics: Afrobeats=Nigerian, Afropop=pan-African, "
        f"     Bongo Flava=Tanzanian, Genge=Kenyan, Kizomba=Angolan, "
        f"     Soukous=Congolese, Highlife=Ghanaian. NEVER use Afrobeats for East Africa.\n"
        f"  7. If genuinely unknown, return empty string — do NOT guess.\n"
        f"  8. Use these spellings: Afrobeats, Amapiano, Bongo Flava, Genge, "
        f"     Kizomba, Soukous, Highlife, Kompa, Funk Carioca, Jersey Club, "
        f"     Chopped and Screwed, Lo-Fi Hip-Hop, Vaporwave, Phonk, "
        f"     Drill, UK Drill, Grime, UK Rap, Afroswing, Marching Band, "
        f"     Anime Soundtrack, Arabic Music, World Music, Classical Crossover.\n\n"
        f'Reply ONLY with JSON: {{"genre": "string"}} — empty string if not confident.'
    )

    # MK12: Build rich context block from all 5 data sources before calling AI.
    # This gives Claude/GPT-4o library evidence (collaborators, graph, device format)
    # so it can reason rather than pattern-match on the track name alone.
    _context_block = ""
    try:
        import context_builder as _cb
        _ctx = _cb.build_context(artist=artist, title=title)
        _context_block = _ctx.as_prompt_block()
        log.debug(
            f"  [ContextBuilder] Built context for {artist!r} — {title!r}: "
            f"cached={_ctx.cached_genre or '—'}, graph={_ctx.graph_genre or '—'}, "
            f"collabs={len(_ctx.collaborators)}, device={'✓' if _ctx.device_found else '✗'}"
        )
    except Exception as _cbe:
        log.debug(f"  [ContextBuilder] Skipped ({_cbe}) — using bare prompt")
        _context_block = ""

    out: dict = {}
    used_via = ""

    # ── Path 1: Gemini direct (most accurate; circuit breaker skips when quota exhausted)
    if not _gemini_circuit_should_skip():
        try:
            api_key = getattr(config, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
            if api_key:
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    # 2026-08-22: gemini-3.6-flash thinks by default — 64
                    # tokens gets entirely consumed by invisible reasoning,
                    # returning empty content. thinkingConfig.thinkingBudget
                    # is NOT a reliable cap (tested live); a generous
                    # maxOutputTokens floor is what actually works. See
                    # ai_helper.py's _gemini_direct() for the full writeup.
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 256},
                }
                response = requests.post(
                    f"{_GEMINI_ENDPOINT}?key={api_key}",
                    json=payload,
                    timeout=12,
                )
                _gemini_circuit_record(response.status_code)
                response.raise_for_status()
                data = response.json()
                content = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
                result = json.loads(content)
                genre = str(result.get("genre", "")).strip()
                if genre:
                    normed = _normalize_genre(genre)
                    if normed:
                        out = {"genre": normed}
                        used_via = "Gemini"
        except Exception as e:
            log.debug(f"  [Gemini/genre] failed for {artist!r}: {e}")

    # ── Path 2: Groq via call_ai waterfall (fallback when Gemini is quota-exhausted)
    if not out.get("genre"):
        try:
            from ai_helper import call_ai as _call_ai, validate_ai_genre_result as _validate_ai
            result = _call_ai(prompt, max_tokens=256, temperature=0.2,
                              context_block=_context_block)
            result = _validate_ai(result)
            if result and isinstance(result, dict):
                _raw_g = str(result.get("genre", "")).strip()
                _raw_y = str(result.get("year", "")).strip()
                genre, _val_year = _validate_ai_genre(_raw_g, _raw_y)
                if genre:
                    normed = _normalize_genre(genre)
                    if normed:
                        out = {"genre": normed}
                        used_via = "Groq"
        except Exception as e:
            log.debug(f"  [AI/genre] Groq fallback failed for {artist!r}: {e}")

    # Cache result (positive or empty) and log if we got something
    with _GEMINI_GENRE_LOCK:
        _GEMINI_GENRE_CACHE[cache_key] = out
    if out:
        log.info(f"  [AI/genre] {artist!r} — {title!r} → {out['genre']!r} (via {used_via})")
    return out


def _claude_normalize_unusual_genre(raw: str, artist: str, title: str) -> str:
    """
    Ask Gemini to map a non-standard/foreign genre string to a clean English
    genre tag. E.g. 'Musique africaine' → 'African Music', 'Coupé-Décalé' →
    'Coupe Decale', 'Neo-traditionnel congolais' → 'Congolese Traditional'.
    Only called when the raw string looks non-standard (non-ASCII or very long).
    Returns the normalized genre string, or the original if Gemini can't improve it.
    """
    cache_key = f"norm:{raw.lower()}"
    if cache_key in _GEMINI_GENRE_CACHE:
        return _GEMINI_GENRE_CACHE[cache_key]

    # REV 14.6 — respect the Gemini breaker; same quota as fallback endpoint.
    if _gemini_circuit_should_skip():
        return raw

    try:
        api_key = getattr(config, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            return raw

        prompt = (
            f"Convert this music genre tag to a clean, standard English ID3 genre string.\n\n"
            f"Raw genre: {raw!r}\n"
            f"Artist: {artist}\n"
            f"Title: {title}\n\n"
            f"Rules:\n"
            f"  1. Output should be English, title case, suitable for an ID3 genre tag\n"
            f"  2. Keep it specific — don't broaden unnecessarily\n"
            f"  3. Max 30 characters\n"
            f'  4. Reply ONLY with JSON: {{"genre": "cleaned string"}}'
        )

        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            # 2026-08-22: see the sibling payload above / ai_helper.py —
            # gemini-3.6-flash needs a generous maxOutputTokens floor, not
            # thinkingConfig, to avoid MAX_TOKENS-before-any-answer.
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 256,
            }
        }

        response = requests.post(
            f"{_GEMINI_ENDPOINT}?key={api_key}",
            json=payload,
            timeout=10
        )
        _gemini_circuit_record(response.status_code)
        response.raise_for_status()
        data = response.json()
        content = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
        result = json.loads(content)
        cleaned = str(result.get("genre", "")).strip()[:30]

        with _GEMINI_GENRE_LOCK:
            _GEMINI_GENRE_CACHE[cache_key] = cleaned or raw
        return cleaned or raw

    except Exception as e:
        log.debug(f"  [Gemini/norm] normalize failed for {raw!r}: {e}")
        return raw

# ---------------------------------------------------------------------------
# Phase 1.5: MusicFetch  (inserted between Deezer and MusicBrainz)
# ---------------------------------------------------------------------------
# MusicFetch is a music link converter and metadata API.  Its primary input
# is a canonical streaming URL (Spotify, Apple Music, YouTube, etc.) and it
# returns resolved links for other services plus rich track metadata.
#
# Python equivalent of the official JS SDK:
#
#   import Musicfetch from 'musicfetch';
#   const musicfetch = new Musicfetch({ token: YOUR_TOKEN });
#   const result = await musicfetch.url({
#       url: 'https://open.spotify.com/track/2hiJe5kecnjIiywY9R1HVy',
#       services: ['appleMusic', 'youtube'],
#   });
#
# How we use it:
#   - Input:   full Spotify track URL built from the track's spotify_id
#   - Services: ['appleMusic', 'deezer'] — Apple Music gives genre from its
#               genre taxonomy (more specific than Deezer alone); Deezer gives
#               a cross-check on year.  We skip YouTube to save quota.
#   - Output:  metadata block on the result (title, artist, releaseDate,
#              genres, isrc, label) + resolved service links
#
# Why Phase 1.5?
#   - Requires a Spotify track URL, so it's only called when spotify_id is
#     known. Tracks without a Spotify ID skip MusicFetch gracefully and fall
#     through to MusicBrainz as before.
#   - Apple Music genre taxonomy is significantly more specific than Deezer's
#     top-level genres and far more accurate for Afrobeats, Amapiano, Bongo
#     Flava, City Pop, etc. — exactly the genres our library struggles with.
#   - Only called for Phase 1 misses, so quota impact is minimal.
#
# Free tier: limited requests/day.  Set MUSICFETCH_API_KEY in .env.
# Docs: https://musicfetch.io/docs

_MF_ENDPOINT   = "https://api.musicfetch.io/url"   # .url() method endpoint
_MF_SERVICES   = ["appleMusic", "deezer"]           # services to resolve; skip youtube to save quota
_MF_CALL_LOCK  = __import__("threading").Lock()
_MF_CALL_DELAY = 0.25    # seconds between calls — conservative for free tier

# Backoff state: if we hit quota we stop trying for this run
_MF_BACKED_OFF_UNTIL: float = 0.0


def _musicfetch(spotify_id: str, artist: str = "", title: str = "") -> dict:
    """
    Phase 1.5: Resolve a Spotify track URL via MusicFetch.

    Python equivalent of:
        musicfetch.url({ url: 'https://open.spotify.com/track/{id}',
                         services: ['appleMusic', 'deezer'] })

    Args:
        spotify_id: Spotify track ID (required — MusicFetch is URL-first).
                    If empty, returns {} immediately (falls through to Phase 2).
        artist:     Used only for log messages.
        title:      Used only for log messages.

    Returns:
        dict with zero or more of:
            "genre"  — normalized genre string (from Apple Music taxonomy)
            "year"   — 4-digit release year string
            "isrc"   — ISRC code (useful for dedup / audit)
            "label"  — record label name
            "links"  — dict of {service: url} for resolved platforms
        Empty dict on: no spotify_id, missing key, rate-limit, quota, or error.
    """
    global _MF_BACKED_OFF_UNTIL

    # MK37/P0-4 + S5: offline capability + provider health check
    try:
        from mk37_wiring import (
            offline_provider_capability_check, is_provider_available,
        )
        offline_provider_capability_check("musicfetch")
        if not is_provider_available("musicfetch"):
            return {}
    except ImportError:
        pass

    mf_key = getattr(config, "MUSICFETCH_API_KEY", "") or os.environ.get("MUSICFETCH_API_KEY", "")
    if not mf_key or not spotify_id:
        return {}

    if time.time() < _MF_BACKED_OFF_UNTIL:
        return {}

    spotify_url = f"https://open.spotify.com/track/{spotify_id}"
    result: dict = {}

    try:
        payload = {
            "url":      spotify_url,
            "services": _MF_SERVICES,
        }

        with _MF_CALL_LOCK:
            time.sleep(_MF_CALL_DELAY)
            r = requests.post(
                _MF_ENDPOINT,
                json=payload,
                headers={
                    "Authorization": f"Bearer {mf_key}",
                    "Content-Type":  "application/json",
                },
                timeout=10,
            )

        if r.status_code == 429:
            log.warning("  [MusicFetch] 429 rate-limit — backing off 60s")
            _MF_BACKED_OFF_UNTIL = time.time() + 60.0
            return {}
        if r.status_code in (402, 403):
            log.warning(f"  [MusicFetch] HTTP {r.status_code} — quota exhausted or token invalid; disabling for this run")
            _MF_BACKED_OFF_UNTIL = time.time() + 86400.0   # effectively disable for the run
            return {}
        if r.status_code != 200:
            log.debug(f"  [MusicFetch] HTTP {r.status_code} for {artist!r} — {title!r}")
            return {}

        data = r.json()

        # ── Metadata block ──────────────────────────────────────────────────
        # The result object contains a 'metadata' sub-object (or top-level
        # fields depending on API version) with canonical track info sourced
        # from the resolved services.
        meta = data.get("metadata") or data.get("track") or data

        # ── Release year ────────────────────────────────────────────────────
        release_date = (
            meta.get("releaseDate")
            or meta.get("release_date")
            or meta.get("date")
            or ""
        )
        if release_date:
            m = re.match(r"((?:19|20)\d{2})", str(release_date))
            if m:
                result["year"] = m.group(1)

        # ── Genre — prefer Apple Music taxonomy (most specific) ─────────────
        # Apple Music returns genres as an array, most specific first.
        # e.g. ["Afrobeats", "Music"] — we walk until we find a non-broad one.
        am_data  = (data.get("links") or {}).get("appleMusic") or {}
        am_genres: list = (
            am_data.get("genres")
            or am_data.get("genreNames")
            or meta.get("genres")
            or meta.get("genre")
            or []
        )
        if isinstance(am_genres, str):
            am_genres = [am_genres]

        for raw_g in am_genres:
            normed = _normalize_genre(str(raw_g))
            if normed and not _is_broad(normed):
                result["genre"] = normed
                break

        # Fallback: Deezer genre from resolved link block
        if "genre" not in result:
            dz_data   = (data.get("links") or {}).get("deezer") or {}
            dz_genres: list = dz_data.get("genres") or dz_data.get("genre") or []
            if isinstance(dz_genres, str):
                dz_genres = [dz_genres]
            for raw_g in dz_genres:
                normed = _normalize_genre(str(raw_g))
                if normed and not _is_broad(normed):
                    result["genre"] = normed
                    break

        # Weakest fallback: first genre from any source, even if broad
        if "genre" not in result and am_genres:
            normed = _normalize_genre(str(am_genres[0]))
            if normed:
                result["genre"] = normed

        # ── ISRC ────────────────────────────────────────────────────────────
        isrc = meta.get("isrc") or meta.get("ISRC") or ""
        if isrc:
            result["isrc"] = isrc

        # ── Label ───────────────────────────────────────────────────────────
        label = meta.get("label") or meta.get("recordLabel") or ""
        if label:
            result["label"] = label

        # ── Resolved platform links (stored for potential future use) ────────
        links_out: dict = {}
        for svc in _MF_SERVICES:
            svc_data = (data.get("links") or {}).get(svc) or {}
            link_url = svc_data.get("url") or svc_data.get("link") or ""
            if link_url:
                links_out[svc] = link_url
        if links_out:
            result["links"] = links_out

        if result:
            log.debug(
                f"  [MusicFetch] {artist!r} — {title!r} → "
                f"year={result.get('year', '')!r}  "
                f"genre={result.get('genre', '')!r}"
                + (f"  isrc={isrc!r}" if isrc else "")
                + (f"  links={list(links_out)}" if links_out else "")
            )
        else:
            log.debug(f"  [MusicFetch] no metadata returned for {spotify_url!r}")

    except Exception as e:
        log.debug(f"  [MusicFetch] call failed for {artist!r} — {title!r}: {e}")

    return result


def _slow_lookup(
    artist: str,
    title: str,
    need_genre: bool,
    need_year: bool,
    spotify_id: str = "",
) -> dict:
    """
    Phase 1.5 → Phase 2 → Phase 3 → Phase 4:
      MusicFetch → MusicBrainz → Discogs → Google → AI (Groq/Gemini)
    """
    result: dict = {}

    # Phase 1.5: MusicFetch — URL-first, Apple Music genre taxonomy
    # Only fires when we have a Spotify ID (builds the full track URL internally).
    mf_key = getattr(config, "MUSICFETCH_API_KEY", "") or os.environ.get("MUSICFETCH_API_KEY", "")
    if mf_key and spotify_id and (need_genre or need_year):
        mf = _musicfetch(spotify_id, artist=artist, title=title)
        if mf.get("genre") and need_genre:
            result["genre"] = mf["genre"]
            need_genre = False
        if mf.get("year") and need_year:
            result["year"] = mf["year"]
            need_year = False

    # Phase 2a: MusicBrainz
    if need_genre or need_year:
        mb = _musicbrainz(artist, title)
        if mb.get("genre") and need_genre:
            result["genre"] = mb["genre"]
            need_genre = False
        if mb.get("year") and need_year:
            result["year"] = mb["year"]
            need_year = False

    # Phase 2b: Discogs
    if (need_genre or need_year) and config.DISCOGS_TOKEN:
        dc = _discogs(artist, title)
        if dc.get("genre") and need_genre:
            result["genre"] = dc["genre"]
            need_genre = False
        if dc.get("year") and need_year:
            result["year"] = dc["year"]
            need_year = False

    # Phase 3: Google search scrape — only when all API providers failed on genre
    if need_genre:
        log.debug(f"  [Google] Phase 3 scrape for genre: {artist!r} — {title!r}")
        gg = _google_genre(artist, title)
        if gg.get("genre"):
            result["genre"] = gg["genre"]
            log.debug(f"  [Google] genre={gg['genre']!r} for {artist!r} — {title!r}")
            need_genre = False

    # Phase 4: AI knowledge fallback — when ALL providers + Google failed on genre
    if need_genre:
        gem = _claude_genre_fallback(artist, title)
        if gem.get("genre"):
            result["genre"] = gem["genre"]

    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def enrich_metadata(tracks: list[dict]) -> dict[str, dict]:
    """
    Multi-phase enrichment for a list of track dicts.
    Each dict must have: track_id, artist, title, spotify_year.
    Optional: spotify_id (Spotify track ID) — passed to MusicFetch for
              more accurate resolution when MUSICFETCH_API_KEY is set.

    Returns {track_id: {"genre": str, "year": str}}.
    spotify_year is used as final fallback only if all providers fail.
    """
    cache    = _get_genre_cache()  # MK15: singleton — no disk read per call
    # FIX 17: treat entries with empty genre as cache misses so they are
    # retried next sync.  Previously "Unknown" was written permanently,
    # meaning tracks whose genre APIs failed once were never re-attempted.
    #
    # FIX 5 (2026-04-17): also treat cache entries as misses when the
    # stored genre is broad AND _artist_override has a specific genre
    # for this artist.  This rescues artists whose previous enrichment
    # landed a useless bucket value like "Rap/Hip Hop" or "Alternative
    # Rock" before the override dict was expanded — the override can now
    # supersede the stale entry on the next run.
    to_fetch = [
        t for t in tracks
        if _should_refetch(t, cache.get(_cache_key(t["artist"], t["title"])))
    ]

    if not to_fetch:
        log.info("Genre cache: all tracks already cached, skipping API calls.")
    else:
        total = len(to_fetch)
        log.info(
            f"Genre/year lookup — Phase 1 (Last.fm + Deezer): "
            f"{total} track(s) across {config.GENRE_WORKERS} workers ..."
        )

        # Phase 1: parallel fast lookup
        phase1_results: dict[str, dict] = {}
        completed = 0

        # MK17 FIX-OWN-B: honour resource_monitor throttle signal before spinning workers.
        try:
            import resource_monitor as _rm
            _rm.wait_if_throttled(timeout=10.0)
        except Exception:
            pass

        # MK20 G3: Initialize adaptive worker count for this session
        _initial_workers = int(getattr(config, "GENRE_WORKERS", 25))
        _GROQ_ACTIVE_WORKERS[0] = _initial_workers
        _groq_budget = int(getattr(config, "GROQ_MAX_CALLS_PER_RUN", 2000))
        _GROQ_CALLS_THIS_RUN[0] = 0

        # MK20 NG3: use GENRE_WORKERS_AI (not GENRE_WORKERS) for AI phase.
        # ROOT CAUSE (2026-05-04): 25 concurrent Groq calls → 429 cascade.
        # 8 workers at ~6s/call ≈ 1.3 req/s, safely under free-tier limit.
        _ai_workers = int(getattr(config, "GENRE_WORKERS_AI", 8))
        with ThreadPoolExecutor(max_workers=_ai_workers) as pool:
            futures = {
                pool.submit(_fast_lookup, t["artist"], t["title"]): t
                for t in to_fetch
            }
            for future in as_completed(futures):
                t   = futures[future]
                key = _cache_key(t["artist"], t["title"])
                try:
                    phase1_results[key] = future.result()
                except Exception as exc:
                    log.debug(f"Phase 1 error for '{t['title']}': {exc}")
                    phase1_results[key] = {}
                completed += 1
                if completed % 100 == 0 or completed == total:
                    log.info(f"  Phase 1 progress: {completed}/{total} tracks processed")

        # Phase 2: sequential slow fallback — ONLY real misses
        phase2_needed = [
            t for t in to_fetch
            if "genre" not in phase1_results.get(_cache_key(t["artist"], t["title"]), {})
            or "year"  not in phase1_results.get(_cache_key(t["artist"], t["title"]), {})
        ]

        if phase2_needed:
            # MK37/S5: global short-circuit. If provider health registry shows
            # all providers unavailable (cooldown/disabled), skip the entire
            # phase-2 loop instead of waiting per-track. Closes LOG_6_5_26.txt's
            # 1,498 sequential fail-opens (each tracking 80s wall-clock).
            try:
                from mk37_wiring import (
                    should_short_circuit_enrichment, get_provider_registry,
                )
                if should_short_circuit_enrichment():
                    _reg_snap = get_provider_registry().snapshot()
                    log.warning(
                        f"  [MK37/S5] ENRICHMENT_SHORT_CIRCUIT — all providers "
                        f"unavailable: {_reg_snap}. Skipping phase-2 for "
                        f"{len(phase2_needed)} tracks (using phase-1 results)."
                    )
                    for _t in phase2_needed:
                        _key = _cache_key(_t["artist"], _t["title"])
                        _p1 = phase1_results.get(_key, {})
                        phase1_results[_key] = dict(_p1)
                    return phase1_results
            except ImportError:
                pass

            mf_active = bool(getattr(config, "MUSICFETCH_API_KEY", "") or os.environ.get("MUSICFETCH_API_KEY", ""))
            slow_providers = (
                ("MusicFetch → " if mf_active else "") +
                "MusicBrainz + Discogs + Google + AI"
            )
            log.info(
                f"Genre/year lookup — Phase 1.5+2 ({slow_providers}): "
                f"{len(phase2_needed)} track(s) "
                f"({len(phase2_needed) * 100 // total}% of total) ..."
            )
            for i, t in enumerate(phase2_needed, 1):
                key        = _cache_key(t["artist"], t["title"])
                p1         = phase1_results.get(key, {})
                need_genre = "genre" not in p1
                need_year  = "year"  not in p1

                # MK20 G4: fail-open check — if all providers failing repeatedly,
                # skip AI for this track and continue pipeline rather than stalling.
                # ROOT CAUSE: Gemini disabled + Groq throttled = each track waits
                # 60s+20s before giving up. 4431 tracks × stalls = multi-hour delays.
                # FIX A3: track consecutive all-fail streak; at ENRICHMENT_FAIL_OPEN_THRESHOLD
                # emit marker and skip to next track. Tracks retried on next run.
                try:
                    _fo_thresh = int(getattr(config, "ENRICHMENT_FAIL_OPEN_THRESHOLD", 10))
                except Exception:
                    _fo_thresh = 10

                if _ENRICH_ALL_FAIL_STREAK[0] >= _fo_thresh:
                    if not _FAIL_OPEN_WARNED[0]:  # MK15: emit exactly once per session
                        _FAIL_OPEN_WARNED[0] = True
                        log.warning(
                            f"  [AI/Genre] ENRICHMENT_FAIL_OPEN_APPLIED — "
                            f"{_ENRICH_ALL_FAIL_STREAK[0]} consecutive all-provider "
                            f"failures. Falling back to Phase-1 genre for remaining tracks. "
                            f"MK22 B1.2: fail-open uses Phase-1 result, not blank skip."
                        )
                    # Skip AI — use whatever phase1 got (may be empty)
                    merged = dict(p1)
                    phase1_results[key] = merged
                    if i % 25 == 0 or i == len(phase2_needed):
                        log.info(f"  Phase 1.5+2 progress: {i}/{len(phase2_needed)}")
                    continue

                # 2026-07-24: _slow_lookup has no internal try/except around its
                # own provider calls, so ANY single-track failure (most
                # concretely: MusicBrainz/Discogs/MusicFetch raising
                # OfflineProviderViolation when config.OFFLINE_MODE=True, per
                # the MK37/P0-4 hard gate — but this also covers a genuine
                # network error/timeout from any provider) propagated all the
                # way up through enrich_metadata and crashed the ENTIRE run.
                # Confirmed live: a preview-genre-consolidation run spent 4+
                # hours grinding through Phase 1 for 20119 tracks, then died
                # on the very first Phase 1.5+2 track. Catch here and treat
                # as "this track got nothing from Phase 1.5+2" (matches the
                # existing fail-open philosophy a few lines up) rather than
                # losing the whole run's progress — does not change the
                # underlying OFFLINE_MODE enforcement at all, since the
                # blocked provider still never returns a result either way.
                try:
                    p2 = _slow_lookup(
                        t["artist"], t["title"], need_genre, need_year,
                        spotify_id=t.get("spotify_id", ""),
                    )
                except Exception as _slow_err:
                    log.debug(
                        f"  [Genre] Phase 1.5+2 lookup failed for "
                        f"{t['artist']!r}/{t['title']!r}: {_slow_err}"
                    )
                    p2 = {}

                # Track all-fail streak for G4
                if not p2:
                    _ENRICH_ALL_FAIL_STREAK[0] += 1
                else:
                    _ENRICH_ALL_FAIL_STREAK[0] = 0

                merged     = dict(p1)
                merged.update(p2)
                phase1_results[key] = merged
                if i % 25 == 0 or i == len(phase2_needed):
                    log.info(f"  Phase 1.5+2 progress: {i}/{len(phase2_needed)}")
        else:
            log.info("Phase 2 skipped — all tracks resolved in Phase 1.")

        # Commit to disk cache
        for t in to_fetch:
            key = _cache_key(t["artist"], t["title"])
            hit = phase1_results.get(key, {})
            raw_genre = hit.get("genre") or ""
            norm_genre = _normalize_genre(raw_genre) if raw_genre else ""
            # If genre looks non-standard (non-ASCII or unusually long),
            # ask Gemini to map it to a clean English ID3 tag
            if norm_genre and norm_genre != "Unknown":
                try:
                    import unicodedata as _ud
                    is_nonstandard = (
                        len(norm_genre) > 25
                        or any(ord(ch) > 127 for ch in norm_genre)
                        or not _ud.normalize("NFKD", norm_genre).isascii()
                    )
                except Exception:
                    is_nonstandard = False
                if is_nonstandard:
                    norm_genre = _claude_normalize_unusual_genre(
                        norm_genre, t["artist"], t["title"]
                    )
            # MK15: use _genre_cache_set() → batched dirty-write every 50 entries
            # MK28 E1-T2 (A1): Add provenance fields to new cache entries.
            # CTO: "no provenance tracking" — now every entry records source+confidence.
            # Old entries without these fields treated as legacy (confidence=1.0).
            import time as _t_e1
            _entry_source   = hit.get("_source", "phase1") if hit else "phase1"
            _entry_conf     = float(hit.get("confidence", 0.90)) if hit else 0.90
            _genre_cache_set(key, {
                # FIX 17: store "" (not "Unknown") so the next sync retries.
                "genre":      norm_genre or "",
                "year":       hit.get("year")  or "",
                "source":     _entry_source,
                "confidence": _entry_conf,
                "ts":         str(int(_t_e1.time())),
            })
            # MK17/MK18 FIX-F4: track-precise metakey invalidation
            if norm_genre:
                try:
                    import meta_key as _mk_ref
                    _a, _ti = (key.split("|", 1) + [""])[:2]
                    _mk_ref.invalidate_by_track_id(_a, _ti)
                except Exception:
                    pass

        # MK15: flush at end of enrich_metadata batch (catches remaining dirty entries)
        flush_genre_cache()
        log.info(f"Genre/year lookup complete. Cache now has {len(cache)} entries.")

    # Build result map keyed by track_id
    result: dict[str, dict] = {}
    for t in tracks:
        key = _cache_key(t["artist"], t["title"])
        hit = cache.get(key, {})
        result[t["track_id"]] = {
            "genre": hit.get("genre") or "Unknown",
            "year":  hit.get("year")  or t.get("spotify_year", ""),
        }
    return result