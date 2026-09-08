# MK39.1 Simplicity Sprint — Architecture Brief & Scope Lock

**Status:** SCOPED. Code work begins next sprint.
**Source:** Council Forum-Suggestions Analysis 2026-05-10 + adversarial CTO+Senior-SWE review.
**Combined regression at brief lock:** 199/199.

---

## 1. Sprint scope (Council-locked, expanded from 5 to 9 targets)

| Target | Description | Status | Owner |
|---|---|---|---|
| **A** | Offline folder sync (zero API/AI/waterfall) | scoped | Dev |
| **B** | Self-service quarantine review CLI | **SHIPPED** (`quarantine_review.py`, MK39-A1.3-FINAL) | — |
| **C** | Configurable severity in `config.py` | scoped | Dev |
| **D** | Deprecate unused defensive features | scoped | Dev |
| **E** | Restore MK8-era audit speed (<1s for user-owned tracks) | scoped | Dev |
| **F** | **MusicBee integration** for device sync; archive ADB/COM transfer code | scoped (design brief locked) | Dev (large) |
| **G** | **Beets+AcoustID** acoustic fingerprinting | scoped (design brief locked) | Dev (medium-large) |
| **H** | **External-tool-friendly staging library** (Picard/MP3Tag-compatible) | scoped (depends on F) | Dev |
| **I** | **"Needs Review" quarantine flag + CLI** | **SHIPPED THIS DELIVERY** (`--needs-review` + `--needs-review-scan`) | — |

---

## 2. Sequencing — what unlocks what

```
                            [B SHIPPED]
                                │
                                ▼
[I SHIPPED]    [C]    [D]    [E]    [A]
                                          │
                                          ▼
                                         [F]
                                          │
                                          ▼
                                         [H]
                                         
                  [G]  ←── independent from F/H/A; can ship in parallel
```

- **B and I are SHIPPED** — operator review CLI (B) and needs-review surface (I) are both available now; F/H consumers can rely on them.
- **F unlocks H**: Picard-friendly staging only matters when MusicBee is the consumer of the staging dir.
- **G is independent**: Beets+AcoustID pre-processing happens before our enrichment pipeline regardless of device-sync architecture.
- **A is closest to the operator's daily pain** ("drop a folder, sync it"); ship FIRST among A/C/D/E.

---

## 3. Adversarial-review risks (this delivery's review locked these)

| Target | Risk | Mitigation |
|---|---|---|
| F | Untestable in dev sandbox without MusicBee.exe; integration code is dead until binary present | Use mock-MusicBee subprocess for tests; ship integration interface only after at least one operator validates real MusicBee call works |
| F | `archive ADB/COM code as fallback module` is a 2-week refactor, not a hotfix | Sprint break-out: Sprint F.1 = staging dir produced, F.2 = MusicBee invocation, F.3 = ADB/COM archived |
| G | Beets binary + fpcalc + AcoustID API key are external deps | Subprocess wrapper with feature-detect: if `beet` not on PATH, log INFO once and skip silently — graceful degradation, not crash |
| G | Council accelerated G to P0 but the previous backlog had it at P2/21SP | Honest sequencing: don't promise G ships in Sprint MK39.1 alongside everything else; queue as MK39.1.G subsprint |
| H | "No file locks" requires audit of every `open(..., 'w')` in walkman_sync — large surface | Use atomic-write pattern (`tempfile + os.replace`) everywhere; staging dir is read-only between platform passes |
| C/D/E | Risk of premature optimization | Council MK8-baseline accountability: ≤2× MK8 sync time on 5,000-track library (already locked) |

---

## 4. What this brief explicitly does NOT do

- ❌ **Implement F, G, or H code.** Untestable in sandbox; design-brief-only per adversarial review.
- ❌ **Modify MK39-A1.3 user-canonical** (locked under 5 binding constraints).
- ❌ **Modify Phase I containment** (deployed and verified resolving 9/9 forensic-log cases).
- ❌ **Add new write barriers** (MK38.0 freeze + MK39-Gate already cover that surface).
- ❌ **Add new MC sims** for the simplification work — Council didn't request them; the simplification gain is the deliverable, not the proof of safety.

---

## 5. Council recertification gate

Per Council Forum directive 2026-05-10 §4: *"the Simplicity Sprint is now explicitly renamed the External Tool Integration Sprint."* Council will recertify after:
1. F (MusicBee integration) reaches "operator-validated end-to-end sync" milestone.
2. G (Beets+AcoustID) reaches "any track with valid fingerprint matches MusicBrainz" milestone.
3. The MK8-baseline metric (≤2× MK8 time on 5,000-track 80%-user-owned library) is benchmarked.

Until then: Phase I containment is the operational baseline. Operators deploy `SpotifyTunerMK11_Phase_II_log_noise_demotion.zip` and run with the existing pipeline.

---

*— Architecture brief locked 2026-05-10 by adversarial CTO+Senior-SWE review of LOG_10_5_26.txt + Forum_Suggestions.txt.*
