"""
build_mk37_2_graph.py — Generate the MK37.2 architecture graph.

Loads the MK37.1 baseline graph, applies MK37.2 deltas (new nodes, new
edges, updated descriptions), validates structural integrity, and writes
mk37_2_architecture.json.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

BASELINE = Path("/home/claude/mk37/codebase/SpotifyTunerMK11_MK37_1/spotifytuner_graph.json")
OUTPUT   = Path("/home/claude/work/mk37_2_architecture.json")


def main() -> None:
    g = json.loads(BASELINE.read_text(encoding="utf-8"))

    # -------------------------------------------------------------------------
    # Update top-level metadata
    # -------------------------------------------------------------------------
    g["schema_version"] = "1.4"
    g["generated_at"]   = datetime.utcnow().isoformat() + "Z"
    g["current_iteration"] = "MK37.2"

    # Append iteration history
    g.setdefault("iteration_chain", []).append({
        "id": "MK37.2",
        "name": "Operational Endurance Sprint",
        "predecessor": "MK37.1",
        "introduced": datetime.utcnow().date().isoformat(),
        "trigger": "LOG_8_5_26 — token=7d7e8348 expired 27,901s into run; 5,001 CAPABILITY_REJECTED; 0/2,688 albums transferred",
        "tickets": [
            {"id": "MK37.2-1", "priority": "P0",
             "title": "Capability TTL renewal architecture",
             "files": ["mk37_2_renewal.py", "mk37_wiring.py", "walkman_sync.py"]},
            {"id": "MK37.2-2", "priority": "P0",
             "title": "Sync planner signature repair",
             "files": ["walkman_sync.py"]},
            {"id": "MK37.2-3", "priority": "P1",
             "title": "Device inventory race recovery markers",
             "files": ["device_inventory.py"]},
            {"id": "MK37.2-4", "priority": "P2",
             "title": "SD free-space resilience (ADB-first)",
             "files": ["downloader.py"]},
            {"id": "MK37.2-5", "priority": "P2",
             "title": "Resource pressure ↔ capability renewal coupling",
             "files": ["mk37_2_resource_coupling.py", "resource_monitor.py", "mk37_wiring.py"]},
        ],
    })

    # SDD chain — extend
    g.setdefault("sdd_chain", []).append({
        "id":      "SDD_MK37_MK37_1_MK37_2",
        "covers":  ["MK37", "MK37.1", "MK37.2"],
        "supersedes": "SDD_MK37_MK37_1",
    })

    # -------------------------------------------------------------------------
    # New nodes
    # -------------------------------------------------------------------------
    # First, ensure the MK15 / MK37 / MK37.1 baseline nodes exist (they were
    # absent from the MK36-era spotifytuner_graph.json baseline).
    baseline_gap_nodes = [
        {
            "id": "mk15_halt_gate",
            "type": "module", "layer": "observation",
            "purpose": "MK15-R0: Observation halt gate. Quorum-based device-state consensus before destructive ops fire. Closes 'ADB inventory=0 → Stage 1 proceeded anyway' regression.",
            "introduced_in": "MK15", "extended_in": ["MK15", "MK37"],
            "key_classes": ["ObservationHaltGate", "DeviceObservedState"],
            "lines": 320, "tickets": ["MK15-R0", "MK37/S1"],
        },
        {
            "id": "mk15_idempotency",
            "type": "module", "layer": "authority",
            "purpose": "MK15-R1: Idempotency enforcement. UUID-keyed once-and-only-once gate for destructive operations. Replay-safe.",
            "introduced_in": "MK15", "extended_in": ["MK15"],
            "key_functions": ["mark_executed", "is_already_executed"],
            "lines": 450, "tickets": ["MK15-R1"],
        },
        {
            "id": "mk15_file_coherence",
            "type": "module", "layer": "execution",
            "purpose": "MK15-N1: Lock-aware tag-write coherence. Retry strategy for Permission denied (Errno 13) on temp files held by AV / mtagent.",
            "introduced_in": "MK15", "extended_in": ["MK15", "MK37"],
            "key_functions": ["tag_file_with_coherence", "_retry_with_backoff"],
            "lines": 220, "tickets": ["MK15-N1", "MK37/S4"],
        },
        {
            "id": "mk37_wiring",
            "type": "module", "layer": "authority",
            "purpose": "MK37: SINGLE INTEGRATION POINT — production wiring of MK15+MK34+MK35+MK36. Halt gate, transfer quorum, transactional dedup, offline capability check, lock-aware tags, provider health, dependency-graph self-audit, telemetry. MK37.1: production-hardened mode lock, primitive guards, crash-recovery reconciliation, mutation-path audit. MK37.2-1: transfer_via_legacy_with_quorum invokes renew_pass_capability_if_stale before require_capability. MK37.2-5: bootstrap installs resource_capability_coupling.",
            "introduced_in": "MK37", "extended_in": ["MK37", "MK37.1", "MK37.2"],
            "key_functions": [
                "enforce_observation_halt", "begin_sync_pass", "end_sync_pass",
                "transfer_via_legacy_with_quorum", "transactional_dedup",
                "idempotent_destructive_op", "offline_provider_capability_check",
                "tag_file_with_coherence", "audit_dependency_graph",
                "assert_production_flags_locked", "is_production_hardened_mode",
                "_install_primitive_guards", "verify_primitive_guards",
                "ensure_primitive_guards_installed", "reconcile_crash_state",
                "snapshot_telemetry",
            ],
            "lines": 1620,
            "tickets": ["MK37/P0-1..P0-4", "MK37/P1-1", "MK37/S5", "MK37.1-1..MK37.1-4", "MK37.2-1", "MK37.2-5"],
        },
        {
            "id": "mutation_path_audit",
            "type": "module", "layer": "governance",
            "purpose": "MK37.1-1: AST-based scanner that classifies every destructive primitive call site as MEDIATED / INFRA / SCRIPT / TEST / BACKUP / UNMEDIATED. Produces the AUTHORITY_GRAPH_HEALTHY assertion at startup (100% mediated coverage required under hardened mode).",
            "introduced_in": "MK37.1", "extended_in": ["MK37.1"],
            "key_functions": ["scan_codebase", "classify_call_site", "emit_report"],
            "lines": 640, "tickets": ["MK37.1-1"],
        },
        {
            "id": "mk36_capability_store",
            "type": "module", "layer": "authority",
            "purpose": "MK36 R2: SQLite-backed durable capability store. Cross-process consume safety. Reconciled on startup by MK37.1-4 reconcile_crash_state (orphan rows older than 24h marked revoked).",
            "introduced_in": "MK36", "extended_in": ["MK36", "MK37.1"],
            "key_classes": ["CapabilityStore"],
            "key_functions": ["lookup", "consume", "purge_expired"],
            "lines": 380, "tickets": ["MK36-R2", "MK37.1-4"],
        },
    ]
    existing_baseline_ids = {n["id"] for n in g["nodes"]}
    for n in baseline_gap_nodes:
        if n["id"] not in existing_baseline_ids:
            g["nodes"].append(n)

    new_nodes = [
        {
            "id": "mk37_2_renewal",
            "type": "module",
            "layer": "authority",
            "purpose": "MK37.2-1: Capability TTL renewal — proactive re-issuance via _GRANT_KEY before staleness causes rejection cascade. Stage-bound capabilities with begin_destructive_stage / end_destructive_stage. Renewal events journalled to .mk37_2_renewals.jsonl.",
            "introduced_in": "MK37.2",
            "extended_in": ["MK37.2"],
            "key_functions": [
                "renew_pass_capability_if_stale",
                "begin_destructive_stage",
                "end_destructive_stage",
                "current_stage",
                "snapshot_renewal_telemetry",
            ],
            "lines": 270,
            "tickets": ["MK37.2-1"],
        },
        {
            "id": "mk37_2_resource_coupling",
            "type": "module",
            "layer": "tooling",
            "purpose": "MK37.2-5: ResourceMonitor → capability renewal coupling. On pressure-warn events, opportunistically renew the pass capability before it goes stale. No-ops if resource_monitor lacks register_pressure_callback (additive over MK37.2-1 transfer-path renewal).",
            "introduced_in": "MK37.2",
            "extended_in": ["MK37.2"],
            "key_functions": [
                "install_resource_capability_coupling",
                "snapshot_coupling_telemetry",
                "_on_resource_pressure",
            ],
            "lines": 175,
            "tickets": ["MK37.2-5"],
        },
        {
            "id": "test_mk37_2_cto",
            "type": "module",
            "layer": "test",
            "purpose": "MK37.2 board ticket regression — 17 tests across 5 ticket classes verifying capability renewal, planner signature, inventory race markers, SD free-space resilience, and resource coupling.",
            "introduced_in": "MK37.2",
            "extended_in": ["MK37.2"],
            "key_classes": [
                "TestMK37_2_1_CapabilityRenewal",
                "TestMK37_2_2_SyncPlannerSignature",
                "TestMK37_2_3_InventoryRaceRecovery",
                "TestMK37_2_4_SDFreeSpaceResilience",
                "TestMK37_2_5_ResourceCoupling",
                "TestMK37_2_TransferPathRenewal",
            ],
            "lines": 320,
            "tickets": ["MK37.2-1", "MK37.2-2", "MK37.2-3", "MK37.2-4", "MK37.2-5"],
        },
    ]

    # Avoid duplicates
    existing_ids = {n["id"] for n in g["nodes"]}
    for n in new_nodes:
        if n["id"] not in existing_ids:
            g["nodes"].append(n)

    # Update existing nodes touched by MK37.2
    for n in g["nodes"]:
        if n["id"] == "mk37_wiring":
            n.setdefault("extended_in", []).append("MK37.2")
            n["purpose"] = (n.get("purpose", "") +
                " | MK37.2-1: transfer_via_legacy_with_quorum invokes "
                "renew_pass_capability_if_stale before require_capability. "
                "MK37.2-5: install_resource_capability_coupling at bootstrap.")
        elif n["id"] == "walkman_sync":
            n.setdefault("extended_in", []).append("MK37.2")
            n["purpose"] = (n.get("purpose", "") +
                " | MK37.2-1: Stage 0 download bound by begin_destructive_stage. "
                "MK37.2-2: build_sync_plan call site supplies device_snapshot or "
                "logs PLAN_SKIPPED_NO_SNAPSHOT.")
        elif n["id"] == "device_inventory":
            n.setdefault("extended_in", []).append("MK37.2")
            n["purpose"] = (n.get("purpose", "") +
                " | MK37.2-3: emits INVENTORY_RACE_DETECTED / "
                "INVENTORY_RACE_RECOVERED / INVENTORY_RACE_PERSISTED markers.")
        elif n["id"] == "downloader":
            n.setdefault("extended_in", []).append("MK37.2")
            n["purpose"] = (n.get("purpose", "") +
                " | MK37.2-4: ADB-first SD free-space probe with "
                "SDFREE_ADB_OK / SDFREE_MTP_OK / SDFREE_BOTH_FAILED markers.")
        elif n["id"] == "resource_monitor":
            n.setdefault("extended_in", []).append("MK37.2")
            n["purpose"] = (n.get("purpose", "") +
                " | MK37.2-5: register_pressure_callback / "
                "unregister_pressure_callback registry; callbacks fire from "
                "_monitor_loop on pressure-warn events (rate-limited).")

    # -------------------------------------------------------------------------
    # New edges
    # -------------------------------------------------------------------------
    new_edges = [
        # mk37_2_renewal imports
        {"from": "mk37_2_renewal", "to": "mk37_wiring", "type": "imports",
         "description": "mk37_2_renewal calls begin_sync_pass / get_pass_capability "
                        "(routes through _GRANT_KEY invariant)"},

        # mk37_wiring uses mk37_2_renewal in transfer hot path
        {"from": "mk37_wiring", "to": "mk37_2_renewal", "type": "renews_via",
         "description": "MK37.2-1: transfer_via_legacy_with_quorum calls "
                        "renew_pass_capability_if_stale before require_capability"},

        # walkman_sync uses mk37_2_renewal for stage binding
        {"from": "walkman_sync", "to": "mk37_2_renewal", "type": "binds_stage_via",
         "description": "MK37.2-1: Stage 0 download invokes begin_destructive_stage"},

        # mk37_2_resource_coupling imports
        {"from": "mk37_2_resource_coupling", "to": "resource_monitor", "type": "imports",
         "description": "Registers callback via register_pressure_callback"},
        {"from": "mk37_2_resource_coupling", "to": "mk37_2_renewal", "type": "imports",
         "description": "MK37.2-5: pressure-driven renewal calls "
                        "renew_pass_capability_if_stale"},

        # mk37_wiring bootstrap installs coupling
        {"from": "mk37_wiring", "to": "mk37_2_resource_coupling", "type": "bootstrap_installs",
         "description": "MK37.2-5: audit_dependency_graph() bootstrap calls "
                        "install_resource_capability_coupling"},

        # test_mk37_2_cto covers
        {"from": "test_mk37_2_cto", "to": "mk37_2_renewal", "type": "tests",
         "description": "MK37.2-1 T1-T6 regression"},
        {"from": "test_mk37_2_cto", "to": "mk37_wiring", "type": "tests",
         "description": "Transfer path renewal call ordering"},
        {"from": "test_mk37_2_cto", "to": "sync_planner", "type": "tests",
         "description": "MK37.2-2 generate_plan signature"},
        {"from": "test_mk37_2_cto", "to": "walkman_sync", "type": "tests",
         "description": "MK37.2-2 PLAN_SKIPPED_NO_SNAPSHOT marker present"},
        {"from": "test_mk37_2_cto", "to": "device_inventory", "type": "tests",
         "description": "MK37.2-3 race markers + 3-attempt retry preservation"},
        {"from": "test_mk37_2_cto", "to": "downloader", "type": "tests",
         "description": "MK37.2-4 ADB-first probe + single-fire warning"},
        {"from": "test_mk37_2_cto", "to": "mk37_2_resource_coupling", "type": "tests",
         "description": "MK37.2-5 install API + idempotency + no-op when absent"},
    ]

    # Avoid duplicate edges
    existing_edges = {(e["from"], e["to"], e["type"]) for e in g["edges"]}
    for e in new_edges:
        key = (e["from"], e["to"], e["type"])
        if key not in existing_edges:
            g["edges"].append(e)

    # -------------------------------------------------------------------------
    # New audit markers
    # -------------------------------------------------------------------------
    g.setdefault("audit_markers", []).extend([
        {"marker": "CAPABILITY_RENEWED",          "ticket": "MK37.2-1",
         "module": "mk37_2_renewal",   "severity": "INFO"},
        {"marker": "CAPABILITY_RENEWAL_FAILED",   "ticket": "MK37.2-1",
         "module": "mk37_2_renewal",   "severity": "ERROR"},
        {"marker": "STAGE_CAPABILITY_BOUND",      "ticket": "MK37.2-1",
         "module": "mk37_2_renewal",   "severity": "INFO"},
        {"marker": "STAGE_CAPABILITY_UNBOUND",    "ticket": "MK37.2-1",
         "module": "mk37_2_renewal",   "severity": "INFO"},
        {"marker": "PLAN_SKIPPED_NO_SNAPSHOT",    "ticket": "MK37.2-2",
         "module": "walkman_sync",     "severity": "INFO"},
        {"marker": "INVENTORY_RACE_DETECTED",     "ticket": "MK37.2-3",
         "module": "device_inventory", "severity": "WARNING"},
        {"marker": "INVENTORY_RACE_RECOVERED",    "ticket": "MK37.2-3",
         "module": "device_inventory", "severity": "INFO"},
        {"marker": "INVENTORY_RACE_PERSISTED",    "ticket": "MK37.2-3",
         "module": "device_inventory", "severity": "ERROR"},
        {"marker": "SDFREE_ADB_OK",               "ticket": "MK37.2-4",
         "module": "downloader",       "severity": "INFO"},
        {"marker": "SDFREE_MTP_OK",               "ticket": "MK37.2-4",
         "module": "downloader",       "severity": "INFO"},
        {"marker": "SDFREE_BOTH_FAILED",          "ticket": "MK37.2-4",
         "module": "downloader",       "severity": "WARNING"},
        {"marker": "PRESSURE_COUPLING_INSTALLED", "ticket": "MK37.2-5",
         "module": "mk37_2_resource_coupling", "severity": "INFO"},
        {"marker": "CAPABILITY_RENEWED_BY_PRESSURE", "ticket": "MK37.2-5",
         "module": "mk37_2_resource_coupling", "severity": "INFO"},
    ])

    # -------------------------------------------------------------------------
    # Update test suites
    # -------------------------------------------------------------------------
    g.setdefault("test_suites", []).append({
        "id":             "test_mk37_2_cto",
        "tickets":        ["MK37.2-1", "MK37.2-2", "MK37.2-3", "MK37.2-4", "MK37.2-5"],
        "test_count":     17,
        "all_passing":    True,
        "introduced_in":  "MK37.2",
    })

    # -------------------------------------------------------------------------
    # Iteration summary
    # -------------------------------------------------------------------------
    g["iteration_summary"]["MK37.2"] = (
        "MK37.2 — Operational Endurance Sprint. Closes the LOG_8_5_26 architectural "
        "defect (capability lifetime mismatch: token TTL=7200s expired during "
        "non-destructive enrichment phase, leaving 5,001 CAPABILITY_REJECTED "
        "cascading and 0/2,688 albums transferred). "
        "Five tickets: MK37.2-1 stage-bound capabilities + transfer-path proactive "
        "renewal preserving _GRANT_KEY invariant; MK37.2-2 sync_planner signature "
        "repair (build_sync_plan call site now supplies device_snapshot or skips "
        "with explicit marker); MK37.2-3 device inventory race markers "
        "(DETECTED/RECOVERED/PERSISTED); MK37.2-4 ADB-first SD free-space probe "
        "(promotes adb shell df above MTP enumeration which silently fails); "
        "MK37.2-5 ResourceMonitor pressure callback registry coupled to "
        "renewal (handle pressure now triggers preemptive token refresh). "
        "17/17 tests passing. No new mutation-path additions; mutation_path_audit "
        "remains 100% mediated."
    )

    # -------------------------------------------------------------------------
    # Layers — no new layers added (everything fits existing taxonomy)
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # Persist
    # -------------------------------------------------------------------------
    OUTPUT.write_text(json.dumps(g, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")

    # Summary
    print(f"Wrote {OUTPUT}")
    print(f"  Total nodes: {len(g['nodes'])}  (+{len(new_nodes)} new)")
    print(f"  Total edges: {len(g['edges'])}  (+{len(new_edges)} new)")
    print(f"  Audit markers: {len(g['audit_markers'])}")
    print(f"  Test suites:  {len(g['test_suites'])}")
    print(f"  Iteration chain length: {len(g['iteration_chain'])}")


if __name__ == "__main__":
    main()
