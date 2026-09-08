"""Standalone test of the _consolidate_album_genres logic from patch 2."""
from collections import Counter, defaultdict

_BROAD_GENRES = frozenset({
    "", "unknown", "world", "world music", "pop", "rock", "r&b",
    "hip hop", "hip-hop", "electronic", "alternative", "indie",
})


def consolidate(tracks):
    albums = defaultdict(list)
    for t in tracks:
        aa = (t.get("album_artist") or t.get("artist") or "").strip().lower()
        al = (t.get("album") or "").strip().lower()
        if aa and al:
            albums[(aa, al)].append(t)

    unified = backfilled = 0
    for (aa, al), group in albums.items():
        if len(group) < 2:
            continue
        specific = [
            g for g in (t.get("genre", "") for t in group)
            if g and g.strip().lower() not in _BROAD_GENRES
        ]
        if not specific:
            continue
        modal, _ = Counter(specific).most_common(1)[0]
        for t in group:
            current = (t.get("genre") or "").strip()
            if current == modal:
                continue
            if current.lower() in _BROAD_GENRES:
                t["genre"] = modal
                if current:
                    unified += 1
                else:
                    backfilled += 1
            elif current.lower() != modal.lower():
                t["genre"] = modal
                unified += 1
    return unified, backfilled


# Case 1: African Giant pattern — 3 tagged correctly, rest broad/blank
african_giant = [
    {"album_artist": "Burna Boy", "album": "African Giant", "title": f"t{i}",
     "genre": g}
    for i, g in enumerate([
        "Afrobeats", "Afrobeats", "Afrobeats",
        "Pop", "Unknown", "", "World", "R&B",
        "Pop", "Pop", "Unknown", "", "Afrobeats",
        "World", "Pop", "Unknown", "", "Pop", "Unknown",
    ])
]
u, b = consolidate(african_giant)
print(f"African Giant: unified={u}, backfilled={b}")
genres = {t["genre"] for t in african_giant}
print(f"  Final genres: {genres}")
assert genres == {"Afrobeats"}, f"Should all be Afrobeats, got {genres}"

# Case 2: Album where every track is broad — should leave alone
broad_album = [
    {"album_artist": "X", "album": "Y", "title": f"t{i}", "genre": "Pop"}
    for i in range(5)
]
broad_album.append({"album_artist": "X", "album": "Y", "title": "t5", "genre": ""})
u, b = consolidate(broad_album)
print(f"All-broad album: unified={u}, backfilled={b}")
assert u == 0 and b == 0, "Should not touch all-broad albums"
assert broad_album[5]["genre"] == "", "Blank should stay blank when no specific exists"

# Case 3: Singleton album — should not touch
singleton = [{"album_artist": "X", "album": "Single", "title": "t1", "genre": "Pop"}]
u, b = consolidate(singleton)
print(f"Singleton: unified={u}, backfilled={b}")
assert u == 0 and b == 0
assert singleton[0]["genre"] == "Pop"

# Case 4: Tied specific genres — first-seen wins (Counter.most_common is stable)
tied = [
    {"album_artist": "A", "album": "B", "title": "t1", "genre": "Trap"},
    {"album_artist": "A", "album": "B", "title": "t2", "genre": "Drill"},
    {"album_artist": "A", "album": "B", "title": "t3", "genre": "Pop"},  # broad → backfill
]
u, b = consolidate(tied)
print(f"Tied specifics: unified={u}, backfilled={b}, t3.genre={tied[2]['genre']!r}")
# t3 was broad, gets unified to one of Trap/Drill — either is acceptable
assert tied[2]["genre"] in ("Trap", "Drill")

# Case 5: Mostly correct, one outlier — outlier flips to majority
outlier = [
    {"album_artist": "A", "album": "B", "title": f"t{i}", "genre": "Afrobeats"}
    for i in range(9)
]
outlier.append({"album_artist": "A", "album": "B", "title": "t9", "genre": "Amapiano"})
u, b = consolidate(outlier)
print(f"Outlier: unified={u}, backfilled={b}, t9={outlier[9]['genre']!r}")
assert outlier[9]["genre"] == "Afrobeats", "Outlier should flip to majority"

print("\nAll consolidation tests passed.")
