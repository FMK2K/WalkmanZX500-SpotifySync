"""pytest bootstrap — put src/ on sys.path so the flat-imported app modules
(`import config`, `import walkman_dedup`, ...) resolve from the test files.

Run the suite from the repo root:  pytest tests/
"""
import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
