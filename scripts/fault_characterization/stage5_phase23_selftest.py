#!/usr/bin/env python3
"""Synthetic end-to-end self-test for Stage-5 Phase 2 and Phase 3."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


def import_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def event_text(mode: str, action: str) -> str:
    return (
        f"H\tF2A_ASSERT_EVENTS\t1\t{mode}\n"
        "A\t0\t73\t780 NS\tPREEXISTING_TB_ASSERTION\tout_of_bounds_write"
        f"\tILLEGAL_MEMORY_WRITE\txxxxxxxx\t00000000\tf\t{action}\n"
    )


def result_payload(purpose: str, mode: str, status: str, completion: str, outcome: str, arch: str, action: str) -> dict:
    return {
        "schema_version": "3.0",
        "verdict_engine_version": "4.0.0",
        "phase": "run",
        "run_kind": "fault",
        "run_purpose": purpose,
        "assertion_mode": mode,
        "xrun_exit_status": 2 if "TIMEOUT" in status or status == "EXISTING_ASSERTION_DETECTED" else 0,
        "status": status,
        "reason": "synthetic",
        "recommended_exit_code": 2 if status != "DIAGNOSTIC_OUTPUT_MATCH" else 0,
        "markers": {},
        "raw_facts": {
            "tool": {"status": "OK", "infrastructure_error_count": 0},
            "execution": {
                "completion": completion,
                "valid_experiment_execution": True,
                "run_purpose": purpose,
                "assertion_mode": mode,
            },
            "workload": {"outcome": outcome, "architectural_outcome": arch},
            "existing_detector_baseline": {
                "events": [
                    {
                        "event_index": 0,
                        "cycle": 73,
                        "simulation_time": "780 NS",
                        "detector_origin": "PREEXISTING_TB_ASSERTION",
                        "assertion_leaf_name": "out_of_bounds_write",
                        "detector_reported_effect_hint": "ILLEGAL_MEMORY_WRITE",
                        "address": "xxxxxxxx",
                        "write_data": "00000000",
                        "byte_enable": "f",
                        "action": action,
                    }
                ]
            },
            "intervention": {
                "assertion_mode": mode,
                "termination_suppressed": mode != "native",
                "transaction_quarantine": mode == "diagnostic_quarantine",
                "counterfactual_after_first_detector_event": mode != "native",
                "intervention_applied": mode != "native",
            },
        },
    }


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    prep = root / "platform/cv32e40p/prepare_stage5_mm_ram.py"
    policy = root / "platform/cv32e40p/stage5_assertion_policy_v1.json"
    verdict_path = root / "scripts/fault_characterization/stage5_verdict.py"
    oracle_path = root / "scripts/fault_characterization/stage5_multidim_oracle.py"
    validator_path = root / "scripts/fault_characterization/stage5_multidim_oracle_validate.py"
    inventory_path = root / "scripts/fault_characterization/stage5_detector_inventory.py"
    prep_module = import_module(prep, "f2a_phase23_prep")
    verdict = import_module(verdict_path, "f2a_phase23_verdict")
    oracle_module = import_module(oracle_path, "f2a_phase23_oracle")

    with tempfile.TemporaryDirectory(prefix="f2a_phase23_selftest_") as tmp_value:
        tmp = Path(tmp_value)
        source = tmp / "mm_ram.sv"
        synthetic = (
            "module mm_ram;\n"
            + prep_module.DECLARATION_ANCHOR
            + "  always_comb begin\n"
            + prep_module.WRITE_BRANCH_OLD
            + "  end\n"
            + prep_module.ASSERTION_OLD
            + "endmodule\n"
        )
        write(source, synthetic)
        prepared = tmp / "mm_ram.stage5.sv"
        prep_report = tmp / "prep.json"
        subprocess.run(
            [sys.executable, str(prep), str(source), str(prepared), "--policy", str(policy), "--report", str(prep_report)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        prepared_text = prepared.read_text(encoding="utf-8")
        assert prep_module.MARKER in prepared_text
        assert prepared_text.count("out_of_bounds_write :") == 1
        assert "RECORD_AND_QUARANTINE" in prepared_text

        inventory_report = tmp / "detector_inventory.json"
        subprocess.run(
            [
                sys.executable,
                str(inventory_path),
                "--policy",
                str(policy),
                "--source",
                str(source),
                "--output",
                str(inventory_report),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        inventory = json.loads(inventory_report.read_text(encoding="utf-8"))
        assert inventory["readiness"]["known_out_of_bounds_write_smoke"] is True
        assert inventory["guardrails"]["inventory_does_not_modify_sources"] is True

        native_events = tmp / "native.events"
        observe_events = tmp / "observe.events"
        quarantine_events = tmp / "quarantine.events"
        write(native_events, event_text("native", "FATAL_TERMINATION"))
        write(observe_events, event_text("observe", "RECORD_ONLY"))
        write(quarantine_events, event_text("diagnostic_quarantine", "RECORD_AND_QUARANTINE"))
        native_log = (
            "F2A_ASSERT_EVENT\tFATAL_TERMINATION\tout_of_bounds_write\n"
            "xmsim: *F,ASRTST: (/x/verification/shared/tb/mm_ram.sv,367): "
            "(time 780 NS) Assertion tb_top.wrapper_i.ram_i.out_of_bounds_write has failed\n"
            "Simulation terminated via $fatal(1) at time 780 NS + 0\n"
        )
        v_native = verdict.compute_verdict(
            phase="run", run_kind="fault", run_purpose="NATIVE_CHARACTERIZATION",
            xrun_exit_status=2, log_text=native_log, event_path=native_events,
        )
        assert v_native.status == "EXISTING_ASSERTION_DETECTED", v_native
        observe_log = "Simulation aborted due to maximum cycle limit\n"
        v_observe = verdict.compute_verdict(
            phase="run", run_kind="fault", run_purpose="DIAGNOSTIC_OBSERVE",
            xrun_exit_status=2, log_text=observe_log, event_path=observe_events,
        )
        assert v_observe.status == "DIAGNOSTIC_TIMEOUT", v_observe
        quarantine_log = (
            "CRC32 PASS: vector=cbf43926 signature=2d6352b3 last=5650ac83\n"
            "EXIT SUCCESS\n"
        )
        v_quarantine = verdict.compute_verdict(
            phase="run", run_kind="fault", run_purpose="DIAGNOSTIC_QUARANTINE",
            xrun_exit_status=0, log_text=quarantine_log, event_path=quarantine_events,
        )
        assert v_quarantine.status == "DIAGNOSTIC_OUTPUT_MATCH", v_quarantine
        assert v_quarantine.raw_facts["workload"]["architectural_outcome"] == "COUNTERFACTUAL_PASS"

        fault_id = "TF000002_SA0"
        selection_id = "TS000002"
        fault_spec = tmp / "fault.json"
        write(
            fault_spec,
            json.dumps(
                {
                    "stage": "stage_05_fault_materialization",
                    "fault_id": fault_id,
                    "selection_id": selection_id,
                    "site_id": "SITE2",
                    "design": "cv32e40p",
                    "workload": "crc32",
                    "fault_class": "control_path",
                    "polarity": "SA0",
                    "stuck_at": 0,
                    "site": {"source_net": "site_net"},
                    "receiver_signals": [{"expression": "receiver", "role": "direct_receiver_output"}],
                    "fault_spec_digest_sha256": "synthetic",
                },
                indent=2,
            ) + "\n",
        )
        golden = tmp / "golden.trace.tsv"
        write(
            golden,
            f"G\t{selection_id}\t70\t750\ttb.scope.site\t1\t0\n"
            f"GA\t{selection_id}\t70\t750\ttb.scope.site\tSRC\t1\n"
            f"GS\t{selection_id}\ttb.scope.site\t0\t1\n",
        )
        fault_trace_text = (
            f"H\tFAULT\t{fault_id}\n"
            f"F\t{fault_id}\t70\t750\ttb.scope.site\t1\t0\t1\n"
            f"FA\t{fault_id}\t70\t750\ttb.scope.site\tPRE\t1\n"
            f"FA\t{fault_id}\t70\t750\ttb.scope.site\tOBS\t0\n"
            f"FS\t{fault_id}\ttb.scope.site\t0\t1\t1\t0\n"
        )
        mode_data = {
            "native": (
                "NATIVE_CHARACTERIZATION", "EXISTING_ASSERTION_DETECTED",
                "TERMINATED_BY_EXISTING_ASSERTION", "NOT_REACHED", "CENSORED",
                "FATAL_TERMINATION",
            ),
            "observe": (
                "DIAGNOSTIC_OBSERVE", "DIAGNOSTIC_TIMEOUT", "TIMED_OUT",
                "NOT_REACHED", "COUNTERFACTUAL_CENSORED", "RECORD_ONLY",
            ),
            "diagnostic_quarantine": (
                "DIAGNOSTIC_QUARANTINE", "DIAGNOSTIC_OUTPUT_MATCH", "COMPLETED",
                "PASS", "COUNTERFACTUAL_PASS", "RECORD_AND_QUARANTINE",
            ),
        }
        run_dirs = {}
        trace_paths = {}
        for mode, values in mode_data.items():
            purpose, status, completion, outcome, arch, action = values
            run = tmp / mode
            run.mkdir()
            write(run / "result.json", json.dumps(result_payload(purpose, mode, status, completion, outcome, arch, action), indent=2) + "\n")
            write(run / "xrun.log", "synthetic\n")
            write(run / "assertion_events.tsv", event_text(mode, action))
            trace = tmp / f"{mode}.trace.tsv"
            write(trace, fault_trace_text)
            run_dirs[mode] = run
            trace_paths[mode] = trace

        args = SimpleNamespace(
            fault_json=fault_spec,
            assertion_policy=policy,
            golden_trace=golden,
            native_run=run_dirs["native"],
            native_trace=trace_paths["native"],
            observe_run=run_dirs["observe"],
            observe_trace=trace_paths["observe"],
            quarantine_run=run_dirs["diagnostic_quarantine"],
            quarantine_trace=trace_paths["diagnostic_quarantine"],
        )
        oracle = oracle_module.build_oracle(args)
        assert oracle["dimensions"]["activation_class"] == "ACTIVATED"
        assert oracle["dimensions"]["injection_class"] == "EFFECTIVE"
        assert oracle["dimensions"]["propagation_class"] == "ARCHITECTURAL_INTERFACE_REACHED"
        assert "ILLEGAL_MEMORY_WRITE" in oracle["dimensions"]["effect_classes"]
        assert "UNKNOWN_ADDRESS_AT_MEMORY_INTERFACE" in oracle["dimensions"]["effect_classes"]
        output = tmp / "oracle.json"
        write(output, json.dumps(oracle, indent=2) + "\n")
        completed = subprocess.run(
            [sys.executable, str(validator_path), "--oracle", str(output), "--analyzer", str(oracle_path)],
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stdout + completed.stderr)

        tampered = json.loads(output.read_text(encoding="utf-8"))
        tampered["raw_facts"]["activation"]["activated"] = False
        tampered_path = tmp / "oracle.tampered.json"
        write(tampered_path, json.dumps(tampered, indent=2) + "\n")
        tampered_completed = subprocess.run(
            [sys.executable, str(validator_path), "--oracle", str(tampered_path), "--analyzer", str(oracle_path)],
            text=True,
            capture_output=True,
        )
        if tampered_completed.returncode == 0:
            raise AssertionError("tampered oracle was incorrectly accepted")

    print("Stage-5 Phase 2/3 synthetic self-test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
