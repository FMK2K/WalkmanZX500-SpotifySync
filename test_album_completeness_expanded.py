"""Phase B — album_completeness a/b/c set builder + run_completeness_pass."""
import album_completeness as ac


def test_build_set_rule_a_saved():
    tm = {"P": [{"artist": "X", "album": "Saved One", "title": "t1"}]}
    lib = [{"artist": "X", "album": "Saved One", "uri": "spotify:album:AAA"}]
    s = ac.build_completion_album_set(tm, {"AAA"}, lib, min_liked=3, artist_has_saved=False)
    assert any(e["album"] == "Saved One" and e["album_id"] == "AAA" for e in s)
    assert any("saved-album" in e["reason"] for e in s)


def test_build_set_rule_b_min_liked():
    tm = {"Liked Songs": [
        {"artist": "Y", "album": "B Album", "title": f"t{i}", "is_liked": True}
        for i in range(4)
    ]}
    s = ac.build_completion_album_set(tm, set(), [], min_liked=3, artist_has_saved=False)
    assert any(e["album"] == "B Album" for e in s)
    # 2 liked -> excluded
    tm2 = {"L": [{"artist": "Z", "album": "Two", "title": f"t{i}", "is_liked": True} for i in range(2)]}
    s2 = ac.build_completion_album_set(tm2, set(), [], min_liked=3, artist_has_saved=False)
    assert not any(e["album"] == "Two" for e in s2)


def test_build_set_rule_c_artist_has_saved():
    tm = {"P": [
        {"artist": "Radiohead", "album": "OK Computer", "title": "Airbag"},
        {"artist": "Radiohead", "album": "Kid A", "title": "Idioteque"},
    ]}
    lib = [{"artist": "Radiohead", "album": "In Rainbows", "uri": "spotify:album:IR"}]
    s = ac.build_completion_album_set(tm, {"IR"}, lib, min_liked=3, artist_has_saved=True)
    albums = {e["album"] for e in s}
    assert "OK Computer" in albums and "Kid A" in albums   # rule c pulls both
    # with rule c off, only the saved "In Rainbows" survives
    s_off = ac.build_completion_album_set(tm, {"IR"}, lib, min_liked=3, artist_has_saved=False)
    assert {e["album"] for e in s_off} == {"In Rainbows"}


def test_run_pass_disabled(monkeypatch):
    import config
    monkeypatch.setattr(config, "ALBUM_COMPLETENESS_ENABLED", False, raising=False)
    assert ac.run_completeness_pass({}, set()) == []
