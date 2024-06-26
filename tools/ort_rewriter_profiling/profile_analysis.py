"""This script analyzes the profile file generated by onnxruntime/nsys profiling.

It creates an in memory report of the per operator duration profile and prints it out.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from typing import Callable

import tabulate

WARM_UP_ROUNDS = 1


@dataclasses.dataclass
class ProfileEntry:
    op_type: str
    entries: list[tuple[str, int]]  # Entries of (node_name, duration)

    def total_count(self, iteration: int = 1) -> int:
        return len(self.entries) / iteration

    def total_duration(self, iteration: int = 1) -> int:
        return sum(entry[1] for entry in self.entries) / iteration / 1000 / 1000


@dataclasses.dataclass
class ModelProfile:
    op_profiles: list[ProfileEntry]
    iteration: int
    model_name: str | None = None
    compiler_name: str | None = None
    _with_warmup: bool = True
    op_profiles_dict: dict[str, ProfileEntry] | None = dataclasses.field(init=False)

    def __post_init__(self):
        # Discard warmup rounds
        if self._with_warmup:
            for op_profile in self.op_profiles:
                op_profile.entries = op_profile.entries[
                    len(op_profile.entries)
                    // (WARM_UP_ROUNDS + self.iteration)
                    * WARM_UP_ROUNDS :
                ]
        sorted_op_profiles = sorted(
            self.op_profiles, key=lambda x: x.total_duration(), reverse=True
        )
        self.op_profiles_dict = {
            op_profile.op_type: op_profile for op_profile in sorted_op_profiles
        }

    def filter_op_profiles(self, op_types: set[str]) -> ModelProfile:
        """Return a new ModelProfile with only the op types in op_types."""
        return ModelProfile(
            [op_profile for op_profile in self.op_profiles if op_profile.op_type in op_types],
            self.iteration,
            self.model_name,
            self.compiler_name,
            _with_warmup=False,
        )

    def op_duration(self, op_type: str) -> float:
        return (
            self.op_profiles_dict[op_type].total_duration(self.iteration)
            if op_type in self.op_profiles_dict
            else 0
        )

    def op_count(self, op_type: str) -> int:
        return int(
            self.op_profiles_dict[op_type].total_count(self.iteration)
            if op_type in self.op_profiles_dict
            else 0
        )

    def total_op_count(self) -> int:
        return sum(op_profile.total_count(self.iteration) for op_profile in self.op_profiles)

    def total_duration(self) -> float:
        """Total duration of all ops in the model in ms."""
        return sum(
            op_profile.total_duration(self.iteration)
            for op_profile in self.op_profiles
            if not op_profile.op_type.startswith("Batch-")
        )

    @property
    def sorted_op_report(self) -> str:
        sorted_op_profiles = sorted(
            self.op_profiles, key=lambda x: x.total_duration(), reverse=True
        )
        return "\n".join(
            f"Node {op_profile.op_type} has {op_profile.total_count(self.iteration)} instances "
            f"and total duration {op_profile.total_duration(self.iteration)} ms"
            for op_profile in sorted_op_profiles
        )


def tabulate_diff(
    base_report: ModelProfile,
    comp_report: ModelProfile,
    additional_row_lambdas: list[tuple[str, Callable, Callable]] | None = None,
) -> str:
    base_compiler = base_report.compiler_name
    comp_compiler = comp_report.compiler_name

    base_compiler_count_header = f"{base_compiler} count"
    comp_compiler_count_header = f"{comp_compiler} count"
    base_compiler_perf_header = f"{base_compiler} perf (ms)"
    comp_compiler_perf_header = f"{comp_compiler} perf (ms)"

    def _diff(base, comp):
        return comp - base

    def _diff_percent(base, comp):
        return f"{(comp - base) / base * 100:.2f}%" if base and comp else "N/A"

    def _construct_tabulate_dict(
        op_type: str,
        base_count: int,
        comp_count: int,
        base_perf: float,
        comp_perf: float,
    ):
        return {
            "OpType": op_type,
            "Diff": _diff(base_perf, comp_perf),
            "Diff%": _diff_percent(base_perf, comp_perf),
            base_compiler_count_header: base_count,
            comp_compiler_count_header: comp_count,
            base_compiler_perf_header: base_perf,
            comp_compiler_perf_header: comp_perf,
        }

    ## Every op type
    tabulate_data = sorted(
        [
            _construct_tabulate_dict(
                op_type,
                base_report.op_count(op_type),
                comp_report.op_count(op_type),
                base_report.op_duration(op_type),
                comp_report.op_duration(op_type),
            )
            for op_type in set(base_report.op_profiles_dict.keys())
            | set(comp_report.op_profiles_dict.keys())
            if not op_type.startswith("Batch-")
        ],
        key=lambda x: x["Diff"],
        reverse=True,
    )
    tabulate_data.append(
        _construct_tabulate_dict(
            "Total",
            base_report.total_op_count(),
            comp_report.total_op_count(),
            base_report.total_duration(),
            comp_report.total_duration(),
        )
    )
    if additional_row_lambdas:
        for name, count_lambda, duration_lambda in additional_row_lambdas:
            tabulate_data.append(
                _construct_tabulate_dict(
                    name,
                    count_lambda(base_report),
                    count_lambda(comp_report),
                    duration_lambda(base_report),
                    duration_lambda(comp_report),
                )
            )
    return tabulate.tabulate(tabulate_data, headers="keys", tablefmt="grid")


def analyze_profile(profile_path: str):
    with open(profile_path, encoding="utf-8") as f:
        profile = f.read()
    profile_json = json.loads(profile)
    report = {}
    for entry in profile_json:
        if entry.get("cat") != "Node" or not entry.get("dur"):
            continue
        op_type = entry["args"]["op_name"]
        report.setdefault(op_type, ProfileEntry(op_type, []))
        report[op_type].entries.append((entry["name"], entry["dur"]))

    sorted_node_report = sorted(report.values(), key=lambda x: x.total_duration, reverse=True)
    for node_report in sorted_node_report:
        print(
            f"Node {node_report.op_type} has {node_report.total_count()} instances and total duration {node_report.total_duration()} ms"
        )
    print(
        f"Total duration: {sum(node_report.total_duration() for node_report in sorted_node_report)} ms"
    )


def analyze_profile_nvtx(
    profile_path: str,
    iteration: int,
    model_name: str | None = None,
    compiler_name: str | None = None,
) -> ModelProfile:
    report = {}
    with open(profile_path, encoding="utf-8") as f:
        line = f.readline()

        while line:
            entry = json.loads(line)
            line = f.readline()

            if (event := entry.get("NvtxEvent")) is None:
                continue
            op_type = event["Text"].split(".")[0]
            report.setdefault(op_type, ProfileEntry(op_type, []))
            report[op_type].entries.append(
                (event["Text"], int(event["EndTimestamp"]) - int(event["Timestamp"]))
            )

    model_profile = ModelProfile(report.values(), iteration, model_name, compiler_name)

    print(model_profile.sorted_op_report)
    print(f"Total duration: {model_profile.total_duration()} ms")
    return model_profile


def compare_node_reports(
    base_report: ModelProfile,
    comp_report: ModelProfile,
):
    ## Every op type
    print(tabulate_diff(base_report, comp_report))

    ## Matmul family + Add
    matmul_core_op_types = {
        "MatMul",
        "Gemm",
        "FusedMatMul",
    }
    matmul_add_op_types = matmul_core_op_types | {"Add"}

    filtered_base_report = base_report.filter_op_profiles(matmul_add_op_types)
    filtered_comp_report = comp_report.filter_op_profiles(matmul_add_op_types)

    print(
        tabulate_diff(
            filtered_base_report,
            filtered_comp_report,
            additional_row_lambdas=[
                (
                    # Show count of core MatMul like ops, while include Add when counting total duration.
                    "Total MatMul like ops",
                    lambda report: sum(
                        report.op_count(op_type) for op_type in matmul_core_op_types
                    ),
                    lambda report: report.total_duration(),
                ),
            ],
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile-path", "--profile_path", help="Path to profile file", required=True
    )
    parser.add_argument(
        "--type", help="Type of profile file", choices=["ortcpu", "nvtx"], required=True
    )
    parser.add_argument(
        "--iteration",
        required=True,
        help="Number of iterations for nvtx profile. Help remove warmup rounds.",
        type=int,
    )

    args = parser.parse_args()
    profile_path = args.profile_path
    type = args.type
    iteration = args.iteration

    if type == "ortcpu":
        analyze_profile(profile_path)
    elif type == "nvtx":
        analyze_profile_nvtx(profile_path, iteration)
    else:
        raise ValueError(f"Unknown profile type {type}")


if __name__ == "__main__":
    main()
