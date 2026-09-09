"""SpotifyTuner — GUI launcher.

The application code lives in ``src/`` (flat-imported: ``import config``,
``import walkman_dedup as wd`` etc.). This shim puts ``src/`` on ``sys.path``
and hands off to ``main.main()``. Runtime state, logs, and data exports live
in sibling dirs off this file's directory — see ``src/config.py``.

    python run.py
"""
import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from main import main  # noqa: E402

if __name__ == "__main__":
    main()
