# CLAUDE.md — Project Rules & Execution Profile

## Core Architecture: Three-Layer Stack

This project runs on a **Council of Two Skills** for planning and execution, plus a **persistent knowledge layer** (gbrain) underneath both. Nothing gets coded without the two Council skills engaging in sequence; gbrain doesn't replace that handoff — it's where cross-session project knowledge lives so it doesn't have to be re-derived from scratch each time.

| Layer | Source | Role |
|-------|--------|------|
| **planning-with-files** | https://github.com/OthmanAdi/planning-with-files.git | Planning, requirements, design, task breakdown, progress tracking (per-session: `task_plan.md`, `findings.md`, `progress.md`) |
| **ponytail** | https://github.com/DietrichGebert/ponytail.git | Minimalist implementation — writes the absolute least code that works |
| **gbrain** | https://github.com/garrytan/gbrain | Persistent, cross-session knowledge/memory — search and synthesis over project knowledge (device troubleshooting history, past findings, decisions) that outlives any one session's `task_plan.md`/`findings.md`/`progress.md` |

---

## Skill 1: planning-with-files — The Architect

**What it is**: A persistent file-based planning skill that keeps `task_plan.md`, `findings.md`, and `progress.md` on disk so the agent survives context loss, `/clear`, and crashes.[reference:0]

**When it activates**: At the start of any non-trivial task (3+ steps, research, or multi-step coding work).[reference:1]

**What it does**:
1. Creates `task_plan.md` — tracks phases, tasks, and progress[reference:2]
2. Creates `findings.md` — stores research and discoveries[reference:3]
3. Creates `progress.md` — session log and test results[reference:4]
4. Re-reads the plan before major decisions[reference:5]
5. Logs ALL errors to avoid repetition[reference:6]
6. Follows the **2-Action Rule** — saves findings after every 2 view/browser operations[reference:7]

**The Core Principle**: Context Window = RAM (volatile, limited). Filesystem = Disk (persistent, unlimited). Anything important gets written to disk.[reference:8]

**Output**: A complete, signed-off plan in `docs/planning/` before any code is written.

---

## Skill 2: ponytail — The Lazy Senior Dev

**What it is**: Makes your AI agent think like the laziest senior dev in the room. The best code is the code you never wrote.[reference:9]

**The Persona**: Long ponytail, oval glasses. Has been at the company longer than version control. You show him fifty lines; he looks at them, says nothing, and replaces them with one.[reference:10]

**The Numbers** (measured on real Claude Code sessions, FastAPI + React):[reference:11][reference:12]
- **54% less code** (up to 94% on over-built features)
- **22% fewer tokens**
- **20% cheaper**
- **27% faster**
- **100% safe** — keeps every safety guard

**What it does**:
- Before writing anything, it looks for a reason not to[reference:13]
- Checks YAGNI first[reference:14]
- Writes the absolute minimum necessary
- Reaches for native/stdlib solutions before custom code
- Never over-delivers or adds speculative features

**Decision Ladder** (must walk this before any code):
1. **YAGNI** — Does this need to exist at all?
2. **Native / Stdlib First** — Can this be done with built-in features?
3. **Leverage Existing Footprint** — Is there already an abstraction for this?
4. **Absolute Minimal Delta** — If code is unavoidable, write the bare minimum.

---

## Layer 3: gbrain — The Persistent Brain

**What it is**: A local-first memory/knowledge system (PGLite/Postgres-backed, no Docker) that stores project knowledge as searchable, synthesized pages instead of raw session logs. Runs as an MCP server (`gbrain serve`) registered in this project's local Claude Code config.

**Local setup on this machine**:
```
bun install -g github:garrytan/gbrain
gbrain init --pglite            # local brain at ~/.gbrain/brain.pglite
claude mcp add gbrain -- gbrain serve
```

**How it fits alongside the two Council skills**: it does NOT replace `planning-with-files`. `task_plan.md`/`findings.md`/`progress.md` remain the per-session, in-progress record for the Architect skill — those keep getting written and re-read exactly as before. gbrain is for knowledge that should persist and stay searchable *across* sessions once it's settled: device troubleshooting history, resolved bugs and their root causes, architectural decisions and why they were made. Think of it as the layer underneath the Council, not a third member of it.

**Optional skill packs**: `gbrain init` surfaces a list of recommended skills (book-mirror, article-enrichment, strategic-reading, etc.) via `gbrain skillpack scaffold --all`. These are general-purpose knowledge-work skills unrelated to this project's actual work (audio pipeline, device sync) — not installed by default; only install if explicitly asked.

---

## Repository Layout (2026-09-08 src/ reorg)

```
run.py            # launcher — puts src/ on sys.path, calls main.main()
src/              # all ~120 application modules, FLAT-imported (import config,
                  #   import walkman_dedup as wd, ...). NOT a package to import
                  #   from; src/ goes on sys.path via run.py / conftest / the
                  #   script's own dir when run directly.
tests/            # 84 test_*.py + conftest.py (adds src/ to path; run: pytest tests/)
state/            # ALL runtime JSON/db/lock state — gitignored. config.BASE_DIR
                  #   points here; `config.BASE_DIR / "x.json"` == state/x.json.
outputs/reports/  # generated HTML/JSON reports
docs/             # SDDs (+ sdd_history/), planning docs
logs/  data/  archive/   # gitignored: logs, Spotify exports, snapshot zips + HiRes decompile
task_plan.md progress.md findings.md   # planning-with-files, stay at root
```

**Path rules for any new code in `src/`:** state/output files go to
`config.STATE_DIR` / `config.REPORTS_DIR` (or `config.BASE_DIR` for state),
never `Path(__file__).parent`. Subprocess launches of sibling scripts use
`cwd=config.SRC_DIR`. `config.PROJECT_ROOT` is the repo root.

**Running things:** GUI = `python run.py`. A single curation stage = its
button in the GUI Tools panel (**🧰 Curation Pipeline — Individual Stages**),
or `python src/cli_runner.py run-tool <script.py> [flags]`. The whole
pipeline = **🧹 CURATE LIBRARY** or `python src/cli_runner.py curate [--dry-run]`.

---

## Waterfall Process: The Handoff
