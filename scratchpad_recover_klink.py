import sys
sys.path.insert(0, ".")
import downloader
import catalog_fill_download as cf

tracks = [cf._mk_track("Smino", "KLINK", "NOIR")]
track_map = {"__recover_klink__": tracks}
cancelled = [False]
stats = downloader.run_downloads(track_map, cancelled, liked_album_ids=set(), sp_client=None)
print("RECOVER STATS:", stats)
