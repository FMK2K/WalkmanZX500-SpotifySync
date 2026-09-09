# Marks src/ as a package for tooling (pytest rootdir detection, editors).
# The modules inside are flat-imported (`import config`, not `from src.config
# import ...`) — src/ is placed on sys.path by run.py / conftest.py / the
# script's own directory when run directly. Keep this file empty.
