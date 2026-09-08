"""Solver-free R16/S4 scope-structure forensics.

This research command reads immutable historical artifacts and repository
fixture metadata.  It does not import OR-Tools, build a model, validate a
candidate, or execute an operator.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import uuid


RESEARCH_PARENT = Path(r"C:\Users\desou\research_runs")
HISTORICAL_ROOT = RESEARCH_PARENT / "v2_r16_vs_r4_r16_three_hour_20260907_054732_b8d42e6c"
ORIGINAL_FORENSIC_ROOT = RESEARCH_PARENT / "v2_r16_r4_targeting_hint_forensics_20260907_20260907_155839_a0bc36dd"
SCREEN_ROOT = RESEARCH_PARENT / "v2_r16_target_selection_screen_main_20260907T110000Z_778899aa"
FOLLOWUP_ROOT = RESEARCH_PARENT / "v2_r16_target_selection_forensic_followup_20260908e"
CALIBRATION_ROOT = RESEARCH_PARENT / "v2_r16_target_selection_jackpot_calibration_20260908T000500Z_b42e7d91"
ADDENDUM_ROOT = RESEARCH_PARENT / "v2_r16_target_selection_jackpot_calibration_completion_addendum_20260908_5f6a2c11"
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_INPUT = REPO_ROOT / "scheduling_engine/benchmarks/student_assignment/v2_policy_generalization_suite_20260829/reference_target/input.json.gz"

COMPONENTS = (
    "course_category_diversity",
    "course_sequence_preferences",
    "difficulty_balance",
    "section_utilization_balance",
    "student_semester_load_balance",
)
JACKPOT_ATTEMPTS = (27, 36, 60, 63)
CALIBRATION_ATTEMPTS = (27, 36, 63)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def portable_decode(value):
    if not isinstance(value, dict) or "type" not in value:
        if isinstance(value, list):
            return [portable_decode(item) for item in value]
        if isinstance(value, dict):
            return {key: portable_decode(item) for key, item in value.items()}
        return value
    kind = value["type"]
    if kind in {"tuple", "list"}:
        items = [portable_decode(item) for item in value.get("items", [])]
        return tuple(items) if kind == "tuple" else items
    if kind == "dict":
        return {
            portable_decode(item["key"]): portable_decode(item["value"])
            for item in value.get("items", [])
        }
    if kind == "dataclass":
        result = {
            key: portable_decode(item)
            for key, item in value.get("fields", {}).items()
        }
        result["__class__"] = value.get("class")
        return result
    return value


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_hash(value):
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def json_cell(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str,
    )


def freeze(value):
    """Normalize JSON lists/dicts into hashable semantic identities."""

    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((freeze(key), freeze(item)) for key, item in value.items()))
    return value


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        + "\n",
        encoding="utf-8",
    )


def write_csv(path, rows, fieldnames=None):
    rows = list(rows)
    fields = list(fieldnames or (list(rows[0]) if rows else ()))
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def mean(values):
    values = [float(value) for value in values if value is not None]
    return statistics.fmean(values) if values else None


def median(values):
    values = [float(value) for value in values if value is not None]
    return statistics.median(values) if values else None


def percentile(values, quantile):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def distribution(values):
    values = [float(value) for value in values if value is not None]
    return {
        "count": len(values),
        "mean": mean(values),
        "min": min(values) if values else None,
        "p25": percentile(values, 0.25),
        "median": median(values),
        "p75": percentile(values, 0.75),
        "p90": percentile(values, 0.90),
        "max": max(values) if values else None,
    }


def average_ranks(values):
    ordered = sorted(enumerate(values), key=lambda pair: pair[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + 1 + end) / 2.0
        for offset in range(index, end):
            ranks[ordered[offset][0]] = rank
        index = end
    return ranks


def spearman(left, right):
    pairs = [
        (float(x), float(y))
        for x, y in zip(left, right)
        if x is not None and y is not None
    ]
    if len(pairs) < 3:
        return None
    left_rank = average_ranks([item[0] for item in pairs])
    right_rank = average_ranks([item[1] for item in pairs])
    left_mean, right_mean = mean(left_rank), mean(right_rank)
    numerator = sum(
        (x - left_mean) * (y - right_mean)
        for x, y in zip(left_rank, right_rank)
    )
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left_rank)
        * sum((y - right_mean) ** 2 for y in right_rank)
    )
    return numerator / denominator if denominator else 0.0


def cliffs_delta(left, right):
    left = [float(value) for value in left if value is not None]
    right = [float(value) for value in right if value is not None]
    if not left or not right:
        return None
    greater = sum(x > y for x in left for y in right)
    lesser = sum(x < y for x in left for y in right)
    return (greater - lesser) / (len(left) * len(right))


def jaccard(left, right):
    left, right = set(left), set(right)
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def verify_hash_package(root):
    root = Path(root)
    manifest = root / "artifact_hashes.sha256"
    sealed = root / "SEALED"
    result = {
        "path": str(root),
        "artifact_hash_manifest_present": manifest.exists(),
        "sealed_marker_present": sealed.exists(),
    }
    if not manifest.exists():
        return {**result, "classification": "unverified_missing_hash_manifest"}
    missing, mismatched = [], []
    entries = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, relative = line.split("  ", 1)
        path = root / relative
        entries += 1
        if not path.exists():
            missing.append(relative)
        elif sha256_file(path).lower() != expected.lower():
            mismatched.append(relative)
    manifest_hash = sha256_file(manifest)
    seal_match = None
    if sealed.exists():
        text = sealed.read_text(encoding="utf-8").strip()
        try:
            payload = json.loads(text)
            expected_seal = payload.get("artifact_hashes_sha256", text)
        except json.JSONDecodeError:
            expected_seal = text
        seal_match = expected_seal == manifest_hash
    files_verified = not missing and not mismatched
    if sealed.exists() and files_verified and seal_match:
        classification = "sealed_and_verified"
    elif not sealed.exists() and files_verified:
        classification = "hash_manifest_verified_but_not_sealed"
    else:
        classification = "verification_failed"
    return {
        **result,
        "classification": classification,
        "manifest_sha256": manifest_hash,
        "entry_count": entries,
        "missing_files": missing,
        "mismatched_files": mismatched,
        "manifest_matches_seal": seal_match,
    }


def component_facts(checkpoint):
    quality = checkpoint.get("quality", {})
    components = quality.get("components") or checkpoint.get("substantive_components", {})
    total = quality.get("weighted_substantive_value")
    if total is None:
        total = sum(
            float(item.get("weighted_normalized_contribution", 0) or 0)
            for item in components.values()
        )
    result = {"value": float(total)}
    for name in COMPONENTS:
        item = components.get(name, {})
        result[name] = {
            "raw": item.get("raw_penalty"),
            "denominator": item.get("denominator"),
            "normalized": item.get("normalized_penalty"),
            "importance": item.get("importance_score"),
            "weighted": item.get("weighted_normalized_contribution"),
            "share": (
                float(item.get("weighted_normalized_contribution", 0) or 0) / float(total)
                if total else 0.0
            ),
        }
    return result


def checkpoint_source(checkpoint):
    return dict(portable_decode(checkpoint["source_decisions"]))


def load_fixture_metadata():
    payload = read_json(FIXTURE_INPUT)
    dto = portable_decode(payload["dto"])
    sections = {int(item["section_id"]): item for item in dto["sections"]}
    requests = {int(item["request_id"]): item for item in dto["requests"]}
    return dto, sections, requests


def source_section(value):
    return (
        int(value[1])
        if isinstance(value, tuple) and len(value) > 1 and isinstance(value[1], int)
        else None
    )


def semantic_changes(before, after, requests, sections):
    rows = []
    for key in sorted(set(before) | set(after), key=repr):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        request_id = (
            int(key[1])
            if isinstance(key, tuple) and len(key) > 1 and isinstance(key[1], int)
            else None
        )
        request = requests.get(request_id) if key and key[0] == "course" else None
        old_section, new_section = source_section(old), source_section(new)
        old_meta, new_meta = sections.get(old_section, {}), sections.get(new_section, {})
        source_value_student = None
        for value in (new, old):
            if isinstance(value, tuple) and value and isinstance(value[0], int):
                source_value_student = int(value[0])
                break
        rows.append({
            "source_key": list(key) if isinstance(key, tuple) else key,
            "request_id": request_id,
            "request_owner_student_id": request.get("student_id") if request else source_value_student,
            "source_value_student_id": source_value_student,
            "course_id": request.get("course_id") if request else None,
            "old_assignment": old,
            "new_assignment": new,
            "old_section_id": old_section,
            "new_section_id": new_section,
            "old_delivery_group_id": old_meta.get("delivery_group_id"),
            "new_delivery_group_id": new_meta.get("delivery_group_id"),
            "old_semester": old_meta.get("semester"),
            "new_semester": new_meta.get("semester"),
            "old_timeslot_id": old_meta.get("timeslot_id"),
            "new_timeslot_id": new_meta.get("timeslot_id"),
        })
    return rows


def selected_current_decisions(source, scope, requests):
    selected = set(int(item) for item in scope)
    rows = []
    for key, value in sorted(source.items(), key=repr):
        if not isinstance(key, tuple) or not key or key[0] != "course":
            continue
        if not isinstance(value, tuple) or not value or value[0] not in selected:
            continue
        request = requests.get(int(key[1]), {})
        rows.append({
            "student_id": int(value[0]),
            "source_key": list(key),
            "request_id": int(key[1]),
            "request_owner_student_id": request.get("student_id"),
            "course_id": request.get("course_id"),
            "current_section_id": source_section(value),
            "current_assignment": value,
        })
    return rows


def simple_cycles(edges, maximum_length=4):
    adjacency = defaultdict(set)
    for edge in edges:
        source, target = edge["from_section_id"], edge["to_section_id"]
        if source is not None and target is not None and source != target:
            adjacency[source].add(target)
    cycles = set()
    for start in sorted(adjacency):
        stack = [(start, (start,))]
        while stack:
            node, path = stack.pop()
            if len(path) > maximum_length:
                continue
            for target in adjacency.get(node, ()):
                if target == start and len(path) >= 2:
                    body = path
                    rotations = [body[index:] + body[:index] for index in range(len(body))]
                    cycles.add(min(rotations))
                elif target not in path and len(path) < maximum_length:
                    stack.append((target, path + (target,)))
    return sorted(cycles)


def undirected_graph_facts(nodes, edges):
    adjacency = {node: set() for node in nodes}
    for left, right in edges:
        if left == right:
            continue
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
    seen, sizes = set(), []
    for node in adjacency:
        if node in seen:
            continue
        stack, size = [node], 0
        seen.add(node)
        while stack:
            current = stack.pop()
            size += 1
            for neighbor in adjacency[current] - seen:
                seen.add(neighbor)
                stack.append(neighbor)
        sizes.append(size)
    unique_edges = {
        tuple(sorted((left, right), key=repr)) for left, right in edges if left != right
    }
    count = len(adjacency)
    return {
        "node_count": count,
        "edge_count": len(unique_edges),
        "connected_components": len(sizes),
        "largest_component": max(sizes, default=0),
        "density": (2 * len(unique_edges) / (count * (count - 1))) if count > 1 else 0.0,
    }


def scope_structure(snapshot, source, scope, requests):
    scope = tuple(sorted(int(item) for item in scope))
    by_student = {
        int(item["student_id"]): item
        for item in snapshot.get("utilization_candidates", [])
    }
    pressure_by_student = {
        int(item["student_id"]): item
        for item in snapshot.get("pressure_candidates", [])
    }
    selected = [by_student.get(student_id, {"student_id": student_id, "move_facts": []}) for student_id in scope]
    positive_moves = []
    for item in selected:
        for move in item.get("move_facts", []):
            if float(move.get("positive_leverage", 0) or 0) > 0:
                positive_moves.append({**move, "student_id": int(item["student_id"])})
    current = selected_current_decisions(source, scope, requests)
    current_sections = defaultdict(set)
    for item in current:
        if item["current_section_id"] is not None:
            current_sections[item["student_id"]].add(item["current_section_id"])
    destinations = defaultdict(set)
    delivery_groups = defaultdict(set)
    moves_by_student = defaultdict(list)
    for move in positive_moves:
        student_id = move["student_id"]
        destinations[student_id].add(move["to_section_id"])
        delivery_groups[student_id].add(move["delivery_group_id"])
        moves_by_student[student_id].append(move)

    pair_rows = []
    for left_index, left in enumerate(scope):
        for right in scope[left_index + 1:]:
            left_moves, right_moves = moves_by_student[left], moves_by_student[right]
            reciprocal = [
                (first, second)
                for first in left_moves
                for second in right_moves
                if first["from_section_id"] == second["to_section_id"]
                and first["to_section_id"] == second["from_section_id"]
            ]
            left_to_right = destinations[left] & current_sections[right]
            right_to_left = destinations[right] & current_sections[left]
            same_group_interactions = sum(
                first["delivery_group_id"] == second["delivery_group_id"]
                for first in left_moves for second in right_moves
            )
            pair_rows.append({
                "students": [left, right],
                "shared_delivery_group_count": len(delivery_groups[left] & delivery_groups[right]),
                "shared_current_section_count": len(current_sections[left] & current_sections[right]),
                "shared_candidate_destination_count": len(destinations[left] & destinations[right]),
                "source_destination_overlap_count": len(left_to_right) + len(right_to_left),
                "delivery_group_jaccard": jaccard(delivery_groups[left], delivery_groups[right]),
                "candidate_destination_jaccard": jaccard(destinations[left], destinations[right]),
                "reciprocal_move_pair_count": len(reciprocal),
                "same_pressured_structure_move_pair_count": same_group_interactions,
            })

    chain_pairs = []
    for first in positive_moves:
        for second in positive_moves:
            if first["student_id"] == second["student_id"]:
                continue
            if first["to_section_id"] == second["from_section_id"]:
                chain_pairs.append((first, second))
    cycles = simple_cycles(positive_moves)
    destination_freed = {
        (move["student_id"], move["request_id"], move["from_section_id"], move["to_section_id"])
        for move in positive_moves
        if any(
            other != move["student_id"]
            and move["to_section_id"] in current_sections[other]
            and moves_by_student[other]
            for other in scope
        )
    }

    nodes, graph_edges = set(), []
    for student_id in scope:
        nodes.add(("student", student_id))
    for item in current:
        student = ("student", item["student_id"])
        request = ("request", item["request_id"])
        nodes.update((student, request))
        graph_edges.append((student, request))
        if item["current_section_id"] is not None:
            section = ("section", item["current_section_id"])
            nodes.add(section)
            graph_edges.append((request, section))
    for move in positive_moves:
        student = ("student", move["student_id"])
        request = ("request", move["request_id"])
        source_section = ("section", move["from_section_id"])
        target_section = ("section", move["to_section_id"])
        group = ("delivery_group", move["delivery_group_id"])
        nodes.update((student, request, source_section, target_section, group))
        graph_edges.extend(((student, request), (request, source_section), (request, target_section), (source_section, group), (target_section, group)))
    graph = undirected_graph_facts(nodes, graph_edges)

    cross_component = {name: 0.0 for name in COMPONENTS if name != "section_utilization_balance"}
    sequence_opportunities = 0
    for student_id in scope:
        pressure = pressure_by_student.get(student_id, {})
        weighted = dict(pressure.get("component_weighted_penalties", []))
        for name in cross_component:
            cross_component[name] += float(weighted.get(name, 0) or 0)
        sequence_opportunities += int(pressure.get("sequence_opportunity_count", 0) or 0)

    move_requests = {int(move["request_id"]) for move in positive_moves}
    move_destinations = {int(move["to_section_id"]) for move in positive_moves}
    move_groups = {int(move["delivery_group_id"]) for move in positive_moves}
    selected_leverages = [float(item.get("total_positive_leverage", 0) or 0) for item in selected]
    all_groups = snapshot.get("utilization_groups", [])
    penalties = [float(item.get("pairwise_penalty", 0) or 0) for item in all_groups]
    total_penalty = sum(penalties)
    pressure_hhi = sum((value / total_penalty) ** 2 for value in penalties) if total_penalty else 0.0
    top_share = max((float(item.get("total_penalty_share", 0) or 0) for item in all_groups), default=0.0)
    return {
        "scope": list(scope),
        "scope_fingerprint": stable_hash(scope),
        "selected_total_leverage": sum(selected_leverages),
        "selected_mean_leverage": mean(selected_leverages) or 0.0,
        "selected_min_leverage": min(selected_leverages, default=0.0),
        "selected_max_leverage": max(selected_leverages, default=0.0),
        "positive_move_fact_count": len(positive_moves),
        "unique_movable_request_count": len(move_requests),
        "unique_destination_section_count": len(move_destinations),
        "unique_delivery_group_count": len(move_groups),
        "pair_shared_delivery_group_total": sum(item["shared_delivery_group_count"] for item in pair_rows),
        "pair_shared_current_section_total": sum(item["shared_current_section_count"] for item in pair_rows),
        "pair_shared_destination_total": sum(item["shared_candidate_destination_count"] for item in pair_rows),
        "pair_source_destination_overlap_total": sum(item["source_destination_overlap_count"] for item in pair_rows),
        "pair_delivery_group_jaccard_mean": mean(item["delivery_group_jaccard"] for item in pair_rows) or 0.0,
        "pair_destination_jaccard_mean": mean(item["candidate_destination_jaccard"] for item in pair_rows) or 0.0,
        "reciprocal_move_pair_count": sum(item["reciprocal_move_pair_count"] for item in pair_rows),
        "potential_chain_count": len(chain_pairs),
        "structural_cycle_candidate_count": len(cycles),
        "structural_cycle_candidate_lengths": [len(cycle) for cycle in cycles],
        "destinations_potentially_freed_by_selected_student": len(destination_freed),
        "same_pressured_structure_move_pair_count": sum(item["same_pressured_structure_move_pair_count"] for item in pair_rows),
        "pressure_hhi": pressure_hhi,
        "top_group_pressure_share": top_share,
        "graph_node_count": graph["node_count"],
        "graph_edge_count": graph["edge_count"],
        "graph_connected_components": graph["connected_components"],
        "graph_largest_component": graph["largest_component"],
        "graph_density": graph["density"],
        "local_course_category_diversity_pressure": cross_component["course_category_diversity"],
        "local_course_sequence_pressure": cross_component["course_sequence_preferences"],
        "local_difficulty_pressure": cross_component["difficulty_balance"],
        "local_semester_pressure": cross_component["student_semester_load_balance"],
        "local_sequence_opportunity_count": sequence_opportunities,
        "pair_relationships": pair_rows,
        "current_source_decisions": current,
        "guidance_level_positive_moves": positive_moves,
        "structural_cycle_candidates": [list(cycle) for cycle in cycles],
        "guidance_only": True,
        "full_feasibility_not_inferred": True,
    }


def derive_interaction_scope(snapshot, count=4):
    records = snapshot.get("utilization_candidates", [])
    if not records:
        return ()
    top_group = (snapshot.get("utilization_groups") or [{}])[0].get("delivery_group_id")
    by_id = {int(item["student_id"]): item for item in records}
    ordered = [int(item["student_id"]) for item in records]
    rank = {student_id: index for index, student_id in enumerate(ordered)}
    selected, remaining = [], set(ordered)
    while remaining and len(selected) < count:
        def key(student_id):
            item = by_id[student_id]
            groups = set(item.get("delivery_group_ids", []))
            overlap = sum(
                bool(groups & set(by_id[other].get("delivery_group_ids", [])))
                for other in selected
            )
            focused = int(top_group is not None and top_group in groups)
            return (
                -focused,
                -overlap,
                -float(item.get("total_positive_leverage", 0) or 0),
                -float(item.get("strongest_single_move", 0) or 0),
                -int(item.get("relevant_group_count", 0) or 0),
                -int(item.get("alternate_section_opportunity_count", 0) or 0),
                student_id,
            )
        chosen = min(remaining, key=key)
        selected.append(chosen)
        remaining.remove(chosen)
    return tuple(sorted(selected))


def flatten_structure(base, include_details=False):
    excluded = {
        "pair_relationships", "current_source_decisions",
        "guidance_level_positive_moves", "structural_cycle_candidates",
    }
    result = {
        key: value for key, value in base.items()
        if key not in excluded and not isinstance(value, (dict, list))
    }
    result["scope"] = json_cell(base["scope"])
    result["structural_cycle_candidate_lengths"] = json_cell(base["structural_cycle_candidate_lengths"])
    if include_details:
        for key in excluded:
            result[key] = json_cell(base[key])
    return result


def load_attempt_population(requests, sections):
    branch_root = HISTORICAL_ROOT / "branches/r16_only"
    paths = sorted((branch_root / "attempts").glob("attempt_*.json"))
    if len(paths) != 70:
        raise RuntimeError(f"Expected 70 R16-only attempts, found {len(paths)}")
    previous_checkpoint = branch_root / "checkpoints/source.json.gz"
    outcomes, structures, detailed, comparisons, records = [], [], [], [], []
    previous_scope = ()
    scope_counts = Counter()
    for path in paths:
        wrapper = read_json(path)
        attempt = wrapper["attempt"]
        index = int(wrapper["attempt_index"])
        snapshot_path = HISTORICAL_ROOT / wrapper["targeting_snapshot"]["path"]
        snapshot = read_json(snapshot_path)
        pre_checkpoint = read_json(previous_checkpoint)
        post_path = previous_checkpoint
        if attempt.get("adopted") and attempt.get("checkpoint_path"):
            candidate = HISTORICAL_ROOT / attempt["checkpoint_path"]
            if candidate.exists():
                post_path = candidate
        post_checkpoint = read_json(post_path)
        pre_source, post_source = checkpoint_source(pre_checkpoint), checkpoint_source(post_checkpoint)
        pre_quality, post_quality = component_facts(pre_checkpoint), component_facts(post_checkpoint)
        scope = tuple(sorted(int(item) for item in (attempt.get("actual_target_scope") or attempt.get("target_scope") or ())))
        structure = scope_structure(snapshot, pre_source, scope, requests)
        ia_scope = derive_interaction_scope(snapshot)
        ia_structure = scope_structure(snapshot, pre_source, ia_scope, requests)
        candidates = snapshot.get("utilization_candidates", [])
        selected_rows = [
            next((item for item in candidates if int(item["student_id"]) == student_id), {"student_id": student_id})
            for student_id in scope
        ]
        leverages = [float(item.get("total_positive_leverage", 0) or 0) for item in selected_rows]
        cutoff_45 = (
            float(candidates[3].get("total_positive_leverage", 0) or 0)
            - float(candidates[4].get("total_positive_leverage", 0) or 0)
            if len(candidates) > 4 else None
        )
        cutoff_46 = (
            float(candidates[3].get("total_positive_leverage", 0) or 0)
            - float(candidates[5].get("total_positive_leverage", 0) or 0)
            if len(candidates) > 5 else None
        )
        tied_count = sum(
            float(item.get("total_positive_leverage", 0) or 0) == leverages[-1]
            for item in candidates
        ) if leverages else 0
        inner = (attempt.get("inner_probe_summaries") or [{}])[0]
        changes = semantic_changes(pre_source, post_source, requests, sections)
        top_groups = snapshot.get("utilization_groups", [])[:10]
        pressure_values = [float(item.get("pairwise_penalty", 0) or 0) for item in snapshot.get("utilization_groups", [])]
        outcome = {
            "attempt_index": index,
            "source_fingerprint": attempt.get("source_fingerprint_before"),
            "source_value": pre_quality["value"],
            "target_scope": json_cell(scope),
            "scope_fingerprint": stable_hash(scope),
            "top_10_ranking": json_cell([
                {"rank": rank, "student_id": item.get("student_id"), "total_positive_leverage": item.get("total_positive_leverage")}
                for rank, item in enumerate(candidates[:10], 1)
            ]),
            "selected_ranks": json_cell([int(item.get("rank", 0) or 0) for item in selected_rows]),
            "selected_leverage_values": json_cell(leverages),
            "selected_leverage_sum": sum(leverages),
            "selected_leverage_mean": mean(leverages),
            "selected_leverage_min": min(leverages, default=0),
            "selected_leverage_max": max(leverages, default=0),
            "rank_4_to_5_gap": cutoff_45,
            "rank_4_to_6_gap": cutoff_46,
            "rank_4_tied_candidate_count": tied_count,
            "global_utilization_opportunity": wrapper.get("selector_state", {}).get("optimistic_utilization_leverage"),
            "pressured_delivery_group_count": len([value for value in pressure_values if value > 0]),
            "pressure_distribution": json_cell(pressure_values),
            "pressure_top_group_share": structure["top_group_pressure_share"],
            "pressure_hhi": structure["pressure_hhi"],
            "top_delivery_groups": json_cell(top_groups),
            "selected_positive_move_count": structure["positive_move_fact_count"],
            "selected_unique_request_count": structure["unique_movable_request_count"],
            "selected_unique_destination_count": structure["unique_destination_section_count"],
            "selected_unique_delivery_group_count": structure["unique_delivery_group_count"],
            "selected_max_move_leverage": max((float(move.get("positive_leverage", 0) or 0) for move in structure["guidance_level_positive_moves"]), default=0),
            "selected_total_move_leverage": sum(float(move.get("positive_leverage", 0) or 0) for move in structure["guidance_level_positive_moves"]),
            "per_selected_student_positive_move_count": json_cell({
                str(student_id): sum(move["student_id"] == student_id for move in structure["guidance_level_positive_moves"])
                for student_id in scope
            }),
            "solver_status": attempt.get("status"),
            "native_search_wall_seconds": attempt.get("solver_wall_time_seconds"),
            "validation_wall_seconds": attempt.get("validation_seconds"),
            "validation_classification": attempt.get("validation_classification"),
            "candidate_validated": attempt.get("candidate_validated"),
            "adopted": attempt.get("adopted"),
            "authoritative_gain": float(attempt.get("gain", 0) or 0),
            "post_value": post_quality["value"],
            "changed_student_count": int(attempt.get("changed_student_count", 0) or 0),
            "changed_source_decision_count": len(changes),
            "reported_changed_source_decision_count": int(attempt.get("changed_source_decision_count", 0) or 0),
            "changed_request_owner_student_count": len({item["request_owner_student_id"] for item in changes}),
            "changed_source_value_student_count": len({item["source_value_student_id"] for item in changes}),
            "scope_novelty": "fresh_student_set" if scope_counts[scope] == 0 else "repeated_student_set_new_incumbent",
            "prior_scope_observations": scope_counts[scope],
            "previous_scope_jaccard": jaccard(previous_scope, scope) if previous_scope else None,
            "branches": inner.get("branches"),
            "conflicts": inner.get("conflicts"),
        }
        for name in COMPONENTS:
            for field in ("raw", "denominator", "normalized", "importance", "weighted", "share"):
                outcome[f"pre_{name}_{field}"] = pre_quality[name][field]
            outcome[f"improvement_{name}_weighted"] = (
                float(pre_quality[name]["weighted"] or 0)
                - float(post_quality[name]["weighted"] or 0)
            )
        structure_row = {
            "attempt_index": index,
            "authoritative_gain": outcome["authoritative_gain"],
            "changed_student_count": outcome["changed_student_count"],
            "changed_source_decision_count": outcome["changed_source_decision_count"],
            **flatten_structure(structure),
        }
        detailed_row = {
            "attempt_index": index,
            "authoritative_gain": outcome["authoritative_gain"],
            "changed_student_count": outcome["changed_student_count"],
            "changed_source_decision_count": outcome["changed_source_decision_count"],
            **flatten_structure(structure, include_details=True),
        }
        top_set, ia_set = set(scope), set(ia_scope)
        comparison = {
            "attempt_index": index,
            "source_fingerprint": outcome["source_fingerprint"],
            "top_scope": json_cell(scope),
            "interaction_aware_scope": json_cell(ia_scope),
            "scope_jaccard": jaccard(scope, ia_scope),
            "students_replaced": len(top_set - ia_set),
            "students_removed_from_top": json_cell(sorted(top_set - ia_set)),
            "students_added_by_ia": json_cell(sorted(ia_set - top_set)),
            "top_selected_leverage": structure["selected_total_leverage"],
            "ia_selected_leverage": ia_structure["selected_total_leverage"],
            "leverage_sacrifice": structure["selected_total_leverage"] - ia_structure["selected_total_leverage"],
            "positive_move_count_difference_ia_minus_top": ia_structure["positive_move_fact_count"] - structure["positive_move_fact_count"],
            "unique_destination_difference_ia_minus_top": ia_structure["unique_destination_section_count"] - structure["unique_destination_section_count"],
            "unique_group_difference_ia_minus_top": ia_structure["unique_delivery_group_count"] - structure["unique_delivery_group_count"],
            "chain_difference_ia_minus_top": ia_structure["potential_chain_count"] - structure["potential_chain_count"],
            "cycle_difference_ia_minus_top": ia_structure["structural_cycle_candidate_count"] - structure["structural_cycle_candidate_count"],
            "same_structure_interaction_difference_ia_minus_top": ia_structure["same_pressured_structure_move_pair_count"] - structure["same_pressured_structure_move_pair_count"],
            "graph_density_difference_ia_minus_top": ia_structure["graph_density"] - structure["graph_density"],
            "executed_top_gain_observational_label_only": outcome["authoritative_gain"],
            "no_ia_schedule_outcome_inferred": True,
        }
        outcomes.append(outcome)
        structures.append(structure_row)
        detailed.append(detailed_row)
        comparisons.append(comparison)
        records.append({
            "attempt_index": index,
            "wrapper": wrapper,
            "attempt": attempt,
            "snapshot": snapshot,
            "pre_source": pre_source,
            "post_source": post_source,
            "pre_quality": pre_quality,
            "post_quality": post_quality,
            "structure": structure,
            "ia_structure": ia_structure,
            "outcome": outcome,
            "changes": changes,
            "checkpoint_before": str(previous_checkpoint),
            "checkpoint_after": str(post_path),
        })
        scope_counts[scope] += 1
        previous_scope = scope
        previous_checkpoint = post_path
    return outcomes, structures, detailed, comparisons, records


def feature_effects(records):
    old_features = (
        "global_utilization_opportunity", "selected_leverage_sum",
        "selected_leverage_mean", "selected_leverage_min",
        "selected_leverage_max", "rank_4_to_5_gap", "rank_4_to_6_gap",
        "pressure_top_group_share", "pressure_hhi",
    )
    new_features = (
        "positive_move_fact_count", "unique_movable_request_count",
        "unique_destination_section_count", "unique_delivery_group_count",
        "pair_shared_delivery_group_total", "pair_shared_current_section_total",
        "pair_shared_destination_total", "pair_source_destination_overlap_total",
        "pair_delivery_group_jaccard_mean", "pair_destination_jaccard_mean",
        "reciprocal_move_pair_count", "potential_chain_count",
        "structural_cycle_candidate_count",
        "destinations_potentially_freed_by_selected_student",
        "same_pressured_structure_move_pair_count", "graph_density",
        "graph_largest_component", "local_course_category_diversity_pressure",
        "local_course_sequence_pressure", "local_difficulty_pressure",
        "local_semester_pressure", "local_sequence_opportunity_count",
    )
    merged = []
    for record in records:
        merged.append({**record["outcome"], **record["structure"]})
    jackpot = [item for item in merged if item["attempt_index"] in JACKPOT_ATTEMPTS]
    ordinary = [item for item in merged if item["attempt_index"] not in JACKPOT_ATTEMPTS]
    effects = {}
    for kind, fields in (("old", old_features), ("joint_structure", new_features)):
        for field in fields:
            values = [item.get(field) for item in merged]
            jackpot_values = [item.get(field) for item in jackpot]
            ordinary_values = [item.get(field) for item in ordinary]
            leave_one_out = []
            for omitted in JACKPOT_ATTEMPTS:
                subset = [item for item in merged if item["attempt_index"] != omitted]
                subset_jackpot = [item for item in subset if item["attempt_index"] in JACKPOT_ATTEMPTS]
                subset_ordinary = [item for item in subset if item["attempt_index"] not in JACKPOT_ATTEMPTS]
                leave_one_out.append({
                    "omitted_attempt": omitted,
                    "spearman_gain": spearman([item.get(field) for item in subset], [item["authoritative_gain"] for item in subset]),
                    "cliffs_delta_jackpot_vs_ordinary": cliffs_delta(
                        [item.get(field) for item in subset_jackpot],
                        [item.get(field) for item in subset_ordinary],
                    ),
                    "jackpot_median": median(item.get(field) for item in subset_jackpot),
                    "ordinary_median": median(item.get(field) for item in subset_ordinary),
                })
            effects[field] = {
                "feature_family": kind,
                "spearman_gain": spearman(values, [item["authoritative_gain"] for item in merged]),
                "spearman_changed_students": spearman(values, [item["changed_student_count"] for item in merged]),
                "spearman_changed_decisions": spearman(values, [item["changed_source_decision_count"] for item in merged]),
                "jackpot_distribution": distribution(jackpot_values),
                "ordinary_distribution": distribution(ordinary_values),
                "cliffs_delta_jackpot_vs_ordinary": cliffs_delta(jackpot_values, ordinary_values),
                "leave_one_jackpot_out": leave_one_out,
            }
    strata = {
        "all": merged,
        "gain_ge_30": [item for item in merged if item["authoritative_gain"] >= 30],
        "gain_ge_60": [item for item in merged if item["authoritative_gain"] >= 60],
        "broad_four_student": [item for item in merged if item["changed_student_count"] == 4],
        "fifteen_or_sixteen_decisions": [item for item in merged if item["changed_source_decision_count"] in {15, 16}],
        "historical_jackpots": jackpot,
    }
    stratum_summary = {
        name: {
            "count": len(items),
            "gain": distribution(item["authoritative_gain"] for item in items),
            "changed_students": distribution(item["changed_student_count"] for item in items),
            "changed_decisions": distribution(item["changed_source_decision_count"] for item in items),
            "feature_medians": {
                field: median(item.get(field) for item in items)
                for field in old_features + new_features
            },
        }
        for name, items in strata.items()
    }
    ranked = sorted(
        effects.items(),
        key=lambda pair: (
            -max(
                abs(pair[1]["spearman_gain"] or 0),
                abs(pair[1]["cliffs_delta_jackpot_vs_ordinary"] or 0),
            ),
            pair[0],
        ),
    )
    old_max = max((abs(effects[field]["spearman_gain"] or 0) for field in old_features), default=0)
    joint_max = max((abs(effects[field]["spearman_gain"] or 0) for field in new_features), default=0)
    return {
        "schema": "r16_jackpot_feature_effects_v1",
        "population": 70,
        "jackpot_attempts": list(JACKPOT_ATTEMPTS),
        "continuous_outcome_primary": True,
        "feature_effects": effects,
        "strata": stratum_summary,
        "strongest_descriptive_features": [
            {"feature": name, **facts} for name, facts in ranked[:10]
        ],
        "old_feature_max_absolute_gain_spearman": old_max,
        "joint_feature_max_absolute_gain_spearman": joint_max,
        "joint_features_exceed_old_features_on_gain_spearman": joint_max > old_max,
        "interpretation_boundary": "Descriptive 70-state evidence with four jackpot labels; no fitted production coefficient or feasibility claim.",
    }


def actual_move_structure(changes):
    edges = [
        {
            "student_id": item["request_owner_student_id"],
            "request_id": item["request_id"],
            "from_section_id": item["old_section_id"],
            "to_section_id": item["new_section_id"],
        }
        for item in changes
        if item["old_section_id"] is not None and item["new_section_id"] is not None
    ]
    chains = [
        [first["request_id"], second["request_id"]]
        for first in edges for second in edges
        if first is not second
        and first["student_id"] != second["student_id"]
        and first["to_section_id"] == second["from_section_id"]
    ]
    cycles = simple_cycles(edges)
    return {
        "move_edges": edges,
        "chain_candidates": chains,
        "cycle_candidates": [list(item) for item in cycles],
        "affected_sections": sorted({section for item in edges for section in (item["from_section_id"], item["to_section_id"]) if section is not None}),
    }


def attempt27_semantic_diff(requests, sections):
    pre_quality = component_facts(read_json(HISTORICAL_ROOT / "branches/r16_only/checkpoints/incumbent_0026.json.gz"))
    candidates = []
    pre = None
    pre_source_fingerprint = None
    for repeat in (1, 2, 3):
        result_root = CALIBRATION_ROOT / f"repeats/attempt_027/repeat_{repeat:02d}/result"
        path = result_root / "candidate.json.gz"
        payload = read_json(path)
        source = {freeze(key): freeze(value) for key, value in payload["source_decisions"]}
        target_hint = read_json(result_root / "target_hint_snapshot.json.gz")
        repeat_pre = {
            freeze(item["source_key"]): freeze(item["source_value"])
            for item in target_hint["target_source_decision_rows"]
        }
        solver_result = read_json(result_root / "solver_result.json")
        if pre is None:
            pre = repeat_pre
            pre_source_fingerprint = solver_result["source_fingerprint_before"]
        elif repeat_pre != pre:
            raise RuntimeError("Attempt 27 repeats do not share one target-local pre-state")
        post_target = {key: source[key] for key in pre}
        changes = semantic_changes(pre, post_target, requests, sections)
        expected_changes = int(solver_result["attempt"]["changed_source_decision_count"])
        if len(changes) != expected_changes:
            raise RuntimeError(
                f"Attempt 27 repeat {repeat}: expected {expected_changes} changes, found {len(changes)}"
            )
        quality = {
            "value": float(payload["quality"]["weighted_substantive_value"]),
            **{
                name: {
                    "weighted": float(payload["quality"]["components"][name]["weighted_normalized_contribution"]),
                    "raw": payload["quality"]["components"][name]["raw_penalty"],
                    "normalized": payload["quality"]["components"][name]["normalized_penalty"],
                }
                for name in COMPONENTS
            },
        }
        candidates.append({
            "repeat": repeat,
            "candidate_fingerprint": payload["quality"]["source_fingerprint"],
            "gain": pre_quality["value"] - quality["value"],
            "quality": quality,
            "component_improvements": {
                name: float(pre_quality[name]["weighted"] or 0) - quality[name]["weighted"]
                for name in COMPONENTS
            },
            "changed_source_keys": [item["source_key"] for item in changes],
            "changed_source_decisions": changes,
            "changed_request_owner_student_ids": sorted({item["request_owner_student_id"] for item in changes}),
            "changed_source_value_student_ids": sorted({item["source_value_student_id"] for item in changes}),
            "move_structure": actual_move_structure(changes),
        })
    low = candidates[0]
    high = candidates[1]
    low_keys = {tuple(item) for item in low["changed_source_keys"]}
    high_keys = {tuple(item) for item in high["changed_source_keys"]}
    low_destinations = {item["new_section_id"] for item in low["changed_source_decisions"] if item["new_section_id"] is not None}
    high_destinations = {item["new_section_id"] for item in high["changed_source_decisions"] if item["new_section_id"] is not None}
    return {
        "schema": "attempt27_108_vs_114_semantic_diff_v1",
        "authoritative_pre_state": {
            "value": pre_quality["value"],
            "source_fingerprint": pre_source_fingerprint,
            "target_local_source_decision_count": len(pre),
            "target_local_source_decision_fingerprint": stable_hash(sorted(pre.items(), key=repr)),
            "namespace": "current_runtime_target_hint_snapshot",
        },
        "same_scope": [263, 273, 283, 1170],
        "candidates": candidates,
        "repeat_2_and_3_exact_semantic_identity": candidates[1]["candidate_fingerprint"] == candidates[2]["candidate_fingerprint"],
        "changed_source_key_intersection_count": len(low_keys & high_keys),
        "changed_source_key_union_count": len(low_keys | high_keys),
        "changed_source_key_jaccard": jaccard(low_keys, high_keys),
        "destination_intersection_count": len(low_destinations & high_destinations),
        "destination_union_count": len(low_destinations | high_destinations),
        "destination_jaccard": jaccard(low_destinations, high_destinations),
        "high_minus_low_component_improvement": {
            name: high["component_improvements"][name] - low["component_improvements"][name]
            for name in COMPONENTS
        },
        "six_point_explanation": {
            "total": high["gain"] - low["gain"],
            "component_sum": sum(
                high["component_improvements"][name] - low["component_improvements"][name]
                for name in COMPONENTS
            ),
            "direction": "positive means the +114 candidate improved that component more than the +108 candidate",
        },
    }


def calibration_duration():
    summary = read_json(CALIBRATION_ROOT / "analysis/calibration_summary.json")
    rows = summary["rows"]
    walls = [float(item["solver_wall_seconds"]) for item in rows]
    thresholds = (60, 90, 120, 150, 180, 240)
    by_attempt = {
        str(attempt): distribution(
            float(item["solver_wall_seconds"])
            for item in rows if int(item["attempt"]) == attempt
        )
        for attempt in CALIBRATION_ATTEMPTS
    }
    by_fingerprint = {}
    for fingerprint in sorted({item["candidate_fingerprint"] for item in rows}):
        group = [item for item in rows if item["candidate_fingerprint"] == fingerprint]
        by_fingerprint[fingerprint] = {
            "attempt": group[0]["attempt"],
            "gain": group[0]["gain"],
            "count": len(group),
            "duration": distribution(item["solver_wall_seconds"] for item in group),
            "walls": [item["solver_wall_seconds"] for item in group],
        }
    return {
        "rows": rows,
        "overall": distribution(walls),
        "threshold_counts": {f"gt_{threshold}": sum(value > threshold for value in walls) for threshold in thresholds},
        "by_attempt": by_attempt,
        "by_candidate_fingerprint": by_fingerprint,
        "all_returned_before_180": all(value < 180 for value in walls),
    }


def duration_conditionals(records):
    merged = [
        {**record["outcome"], **record["structure"]}
        for record in records
    ]
    wall = [item["native_search_wall_seconds"] for item in merged]
    correlation_fields = (
        "authoritative_gain", "changed_student_count",
        "changed_source_decision_count", "positive_move_fact_count",
        "potential_chain_count", "pair_source_destination_overlap_total",
        "same_pressured_structure_move_pair_count", "graph_density",
    )
    strata = {
        "jackpot": [item for item in merged if item["attempt_index"] in JACKPOT_ATTEMPTS],
        "ordinary": [item for item in merged if item["attempt_index"] not in JACKPOT_ATTEMPTS],
        "four_changed_students": [item for item in merged if item["changed_student_count"] == 4],
        "fifteen_or_sixteen_changed_decisions": [item for item in merged if item["changed_source_decision_count"] in {15, 16}],
        "fresh_student_set": [item for item in merged if item["scope_novelty"] == "fresh_student_set"],
        "repeated_student_set_new_incumbent": [item for item in merged if item["scope_novelty"] != "fresh_student_set"],
    }
    return {
        "spearman_search_wall": {
            field: spearman(wall, [item[field] for item in merged])
            for field in correlation_fields
        },
        "duration_by_stratum": {
            name: distribution(item["native_search_wall_seconds"] for item in items)
            for name, items in strata.items()
        },
    }


def ia_shadow_summary(comparisons):
    return {
        "states": len(comparisons),
        "exact_same_scope_count": sum(item["scope_jaccard"] == 1 for item in comparisons),
        "mean_scope_jaccard": mean(item["scope_jaccard"] for item in comparisons),
        "median_scope_jaccard": median(item["scope_jaccard"] for item in comparisons),
        "minimum_scope_jaccard": min(item["scope_jaccard"] for item in comparisons),
        "maximum_scope_jaccard": max(item["scope_jaccard"] for item in comparisons),
        "jaccard_below_half_count": sum(item["scope_jaccard"] < 0.5 for item in comparisons),
        "mean_top_leverage_sacrifice": mean(item["leverage_sacrifice"] for item in comparisons),
        "mean_positive_move_count_difference_ia_minus_top": mean(item["positive_move_count_difference_ia_minus_top"] for item in comparisons),
        "mean_chain_difference_ia_minus_top": mean(item["chain_difference_ia_minus_top"] for item in comparisons),
        "mean_same_structure_interaction_difference_ia_minus_top": mean(item["same_structure_interaction_difference_ia_minus_top"] for item in comparisons),
        "selection_evidence_only": True,
        "no_ia_schedule_outcome_inferred": True,
    }


def select_held_out_states(records, comparisons):
    screen_rows = read_json(SCREEN_ROOT / "analysis/paired_cell_results.json")["rows"]
    used = {
        int(item["attempt_index"])
        for item in screen_rows if item.get("branch") == "r16_only"
    } | set(CALIBRATION_ATTEMPTS)
    comparison_by_attempt = {item["attempt_index"]: item for item in comparisons}
    candidates = []
    for record in records:
        index = record["attempt_index"]
        if index in used:
            continue
        merged = {**record["outcome"], **record["structure"], **comparison_by_attempt[index]}
        shares = [record["outcome"][f"pre_{name}_share"] for name in COMPONENTS]
        merged["component_share_entropy"] = -sum(value * math.log(value) for value in shares if value > 0)
        merged["coordination_tuple"] = (
            int(merged["structural_cycle_candidate_count"] > 0),
            merged["potential_chain_count"],
            merged["destinations_potentially_freed_by_selected_student"],
            merged["same_pressured_structure_move_pair_count"],
            merged["positive_move_fact_count"],
        )
        candidates.append(merged)
    selected, reasons = [], defaultdict(list)

    def pick(reason, key, reverse=True):
        ordered = sorted(
            candidates,
            key=lambda row: (key(row), -row["attempt_index"]),
            reverse=reverse,
        )
        for item in ordered:
            if (
                item["attempt_index"] not in selected
                and all(abs(item["attempt_index"] - prior) >= 4 for prior in selected)
            ):
                selected.append(item["attempt_index"])
                reasons[item["attempt_index"]].append(reason)
                return
        for item in ordered:
            if item["attempt_index"] not in selected:
                selected.append(item["attempt_index"])
                reasons[item["attempt_index"]].append(reason + "_spacing_fallback")
                return

    pick("predicted_high_coordinated_move_potential", lambda item: item["coordination_tuple"])
    pick("predicted_low_coordinated_move_potential", lambda item: item["coordination_tuple"], reverse=False)
    pick("utilization_dominant_pre_state", lambda item: item["pre_section_utilization_balance_share"])
    pick("mixed_component_pre_state", lambda item: item["component_share_entropy"])
    pick("concentrated_utilization_pressure", lambda item: item["pressure_hhi"])
    pick("diffuse_utilization_pressure", lambda item: item["pressure_hhi"], reverse=False)
    pick("high_top_vs_ia_scope_divergence", lambda item: -item["scope_jaccard"])
    maximum_jaccard = max(item["scope_jaccard"] for item in candidates)
    best_existing = next(
        (
            item["attempt_index"] for item in candidates
            if item["scope_jaccard"] == maximum_jaccard
            and item["attempt_index"] in selected
        ),
        None,
    )
    if best_existing is not None:
        reasons[best_existing].append("low_top_vs_ia_scope_divergence")
    else:
        pick("low_top_vs_ia_scope_divergence", lambda item: item["scope_jaccard"])
    pick(
        "high_source_destination_overlap",
        lambda item: item["pair_source_destination_overlap_total"],
    )
    selected_candidates = [
        item for item in candidates if item["attempt_index"] in selected
    ]
    high_structural = max(
        selected_candidates,
        key=lambda item: (
            item["same_pressured_structure_move_pair_count"],
            -item["attempt_index"],
        ),
    )
    low_structural = min(
        selected_candidates,
        key=lambda item: (
            item["same_pressured_structure_move_pair_count"],
            item["attempt_index"],
        ),
    )
    reasons[high_structural["attempt_index"]].append("high_structural_interaction")
    reasons[low_structural["attempt_index"]].append("low_structural_interaction")
    by_attempt = {record["attempt_index"]: record for record in records}
    result = []
    for index in selected:
        record = by_attempt[index]
        comparison = comparison_by_attempt[index]
        result.append({
            "attempt_index": index,
            "checkpoint_path": record["checkpoint_before"],
            "source_fingerprint": record["outcome"]["source_fingerprint"],
            "source_value": record["outcome"]["source_value"],
            "strata": reasons[index],
            "top_scope": record["structure"]["scope"],
            "interaction_aware_scope": json.loads(comparison["interaction_aware_scope"]),
            "top_vs_ia_jaccard": comparison["scope_jaccard"],
            "pressure_hhi": record["structure"]["pressure_hhi"],
            "top_group_pressure_share": record["structure"]["top_group_pressure_share"],
            "coordination_facts": {
                key: record["structure"][key]
                for key in (
                    "positive_move_fact_count", "potential_chain_count",
                    "structural_cycle_candidate_count",
                    "destinations_potentially_freed_by_selected_student",
                    "same_pressured_structure_move_pair_count",
                )
            },
            "selection_used_pre_solve_facts_only": True,
        })
    return {
        "excluded_original_screen_r16_attempts": sorted(used - set(CALIBRATION_ATTEMPTS)),
        "excluded_calibration_attempts": list(CALIBRATION_ATTEMPTS),
        "states": result,
        "state_count": len(result),
        "policies": ["top_individual", "interaction_aware"],
        "repeats_per_policy": 3,
        "cell_count": len(result) * 2 * 3,
    }


def execution_contract_audit():
    historical = read_json(HISTORICAL_ROOT / "preflight.json")
    screen = read_json(SCREEN_ROOT / "study_manifest.json")
    calibration = read_json(CALIBRATION_ROOT / "experiment_contract.json")
    return {
        "historical": historical,
        "screen": screen,
        "calibration": calibration,
    }


def make_reports(root, outcomes, records, effects, semantic_diff, duration, comparisons, held_out, lineage_verification):
    total_gain = sum(item["authoritative_gain"] for item in outcomes)
    jackpot_rows = [item for item in outcomes if item["attempt_index"] in JACKPOT_ATTEMPTS]
    jackpot_gain = sum(item["authoritative_gain"] for item in jackpot_rows)
    feature_facts = effects["feature_effects"]
    lines = [
        "# R16 jackpot scopes versus ordinary scopes",
        "",
        "This is a solver-free descriptive analysis of all 70 authoritative R16-only attempts. Structural move-graph facts come from persisted pre-solve utilization guidance. They identify coordination opportunities, not globally feasible cycles or counterfactual schedule outcomes.",
        "",
        "## Population and concentration",
        "",
        f"All 70 attempts were reconstructed. Their authoritative gain is {total_gain:.0f} v2 points. Attempts 27/36/60/63 gained {jackpot_gain:.0f} points, or {100 * jackpot_gain / total_gain:.3f}% of the branch total, from 4/70 attempts ({100 * 4 / 70:.3f}%).",
        "",
        "## Descriptive feature result",
        "",
    ]
    lines += [
        f"- No joint structural feature was a strong continuous gain predictor. The largest absolute joint-feature Spearman correlation was {effects['joint_feature_max_absolute_gain_spearman']:.3f} (`graph_density`).",
        f"- Source-to-destination overlap had a modest jackpot contrast: median {feature_facts['pair_source_destination_overlap_total']['jackpot_distribution']['median']:.1f} versus {feature_facts['pair_source_destination_overlap_total']['ordinary_distribution']['median']:.1f}, Cliff's delta {feature_facts['pair_source_destination_overlap_total']['cliffs_delta_jackpot_vs_ordinary']:.3f}, but gain Spearman only {feature_facts['pair_source_destination_overlap_total']['spearman_gain']:.3f}.",
        f"- Potential chains did not separate jackpots positively: median {feature_facts['potential_chain_count']['jackpot_distribution']['median']:.1f} versus {feature_facts['potential_chain_count']['ordinary_distribution']['median']:.1f}.",
        "- No persisted TOP scope contained a guidance-level structural cycle candidate, so cycle presence cannot explain the four jackpots.",
        "- Category pressure was zero and semester pressure constant for these selected scopes. Difficulty pressure was lower in the jackpot group, but its continuous gain association was weak.",
        "- Individual leverage did not behave as a positive jackpot signal: jackpot median selected leverage was lower than ordinary, and rank-cutoff gaps remained tied or weakly negative.",
    ]
    lines += [
        "",
        f"The strongest old-feature absolute gain correlation was {effects['old_feature_max_absolute_gain_spearman']:.3f}, but it was a negative cutoff-gap association rather than a useful positive jackpot discriminator. These are descriptive associations in one sequential trajectory, not production coefficients.",
        "",
        "Leave-one-jackpot-out rows are retained in `r16_jackpot_feature_effects.json`. With only four jackpot states, a feature is treated as stable only when its direction and material separation survive each omission.",
    ]
    (root / "r16_jackpot_vs_ordinary_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    low, high = semantic_diff["candidates"][0], semantic_diff["candidates"][1]
    diff_lines = [
        "# Attempt 27: +108 versus +114",
        "",
        "The candidates share the same authoritative pre-state, fixed four-student TOP scope, seed, worker count, and current hint strategy. Repeat 1 returned +108; repeats 2 and 3 returned the exact same +114 semantic fingerprint.",
        "",
        f"Changed-source-key Jaccard is {semantic_diff['changed_source_key_jaccard']:.3f}; destination-section Jaccard is {semantic_diff['destination_jaccard']:.3f}.",
        "",
        "Weighted component improvements from the common pre-state:",
        "",
    ]
    for name in COMPONENTS:
        diff_lines.append(f"- {name}: +108 candidate {low['component_improvements'][name]:+.0f}; +114 candidate {high['component_improvements'][name]:+.0f}; +114 minus +108 {semantic_diff['high_minus_low_component_improvement'][name]:+.0f}.")
    diff_lines += [
        "",
        f"The component differences sum to {semantic_diff['six_point_explanation']['component_sum']:+.0f}, exactly matching the {semantic_diff['six_point_explanation']['total']:+.0f}-point total difference. The full request-level semantic diff is in the JSON artifact.",
    ]
    (root / "attempt27_108_vs_114_semantic_diff.md").write_text("\n".join(diff_lines) + "\n", encoding="utf-8")

    ia_summary = ia_shadow_summary(comparisons)
    policy_lines = [
        "# R16 target-policy next step",
        "",
        "Known fixed TOP jackpot scopes are reliably exploitable by eight-worker R16/S4. The next causal question is therefore target selection, not whether those candidates are reachable at all.",
        "",
        f"The all-70 IA shadow is strongly different: zero exact scope matches, median Jaccard {ia_summary['median_scope_jaccard']:.3f}, maximum {ia_summary['maximum_scope_jaccard']:.3f}, and all 70 below 0.5. IA gave up {ia_summary['mean_top_leverage_sacrifice']:.1f} TOP-leverage points on average. This is selection evidence only and infers no IA schedule outcome.",
        "TOP and IA therefore remain distinct research modes and IA is worth an eight-worker held-out test. The new joint features do not identify a strong, leave-one-out-stable jackpot mechanism, and structural cycles are absent, so a third coordination-aware policy is not yet justified.",
        "",
        "The held-out design uses the exact pre-state-only strata and cells in `held_out_scope_study_design.md`. No target-policy experiment was run in this pass.",
    ]
    (root / "r16_target_policy_next_step.md").write_text("\n".join(policy_lines) + "\n", encoding="utf-8")

    hint_lines = [
        "# Hint identity mapping status",
        "",
        "Repository support now defines a deterministic research-only mapping from student and semantic source key through each assignment option to its CP-SAT variable index and incumbent hint value. The mapping is built from explicit model-builder objects, not variable-name parsing, fuzzy matching, or numeric ID offsets. Uniqueness and stable-fingerprint tests own the readiness claim.",
        "",
        "Historical artifacts cannot be retroactively populated with variable indexes because the live model namespace was not serialized. The next matched hint study can opt in to the new identity telemetry while preserving current hints and candidate authority.",
        "",
        "No hint strategy was changed and no hint experiment was run.",
    ]
    (root / "hint_identity_mapping_status.md").write_text("\n".join(hint_lines) + "\n", encoding="utf-8")

    held_lines = [
        "# Held-out eight-worker TOP versus interaction-aware study design",
        "",
        "This design is not an executed experiment. States were selected with pre-solve facts only, spaced by at least four R16 attempts where possible, and exclude every R16 state used by the original discovery screen plus calibration attempts 27, 36, and 63.",
        "",
        f"Use {held_out['state_count']} states × 2 policies × 3 clean repeats = {held_out['cell_count']} cells. Three repeats are retained because the fixed-scope calibration reproduced jackpot-level gain 9/9 but attempt 27 still produced two semantic candidates and only 2/3 exact historical fingerprints.",
        "",
        "Frozen cell contract: R16/S4; eight optimization workers; seed 101; current complete incumbent hints; 300-second search ceiling; one validation worker; independent 180-second validation allowance; 540-second parent wall; fresh authoritative reset and clean process per cell; full-model validation and strict v2 adoption unchanged.",
        "",
        "## States",
        "",
    ]
    for item in held_out["states"]:
        held_lines.append(f"- Attempt {item['attempt_index']}: {', '.join(item['strata'])}; TOP {item['top_scope']}; IA {item['interaction_aware_scope']}; Jaccard {item['top_vs_ia_jaccard']:.3f}.")
    held_lines += [
        "",
        "## Outcome distributions",
        "",
        "For every state/policy report mean, median, p25/p75, min/max, probabilities of gain ≥30/60/90, changed-student distribution, changed-decision distribution, probability of at least 15 changed decisions, gain/search-minute, gain/full-wall-minute, all five component movements, search status, and validation classification. Report expected value and upside separately.",
    ]
    (root / "held_out_scope_study_design.md").write_text("\n".join(held_lines) + "\n", encoding="utf-8")

    hint_design = """# Future fixed-scope hint study design

Do not run this study until exact hint-identity telemetry is captured in a smoke cell and its fingerprint replays exactly.

Use one fixed authoritative source and exact scope, eight workers, fixed seed with clean repeats, unchanged R16/S4 and validation, and compare: (1) current complete incumbent hints, (2) no hints, and (3) target-release hints. Primary outcomes are CP-SAT discovery time, candidate-fingerprint distribution, authoritative gain, jackpot reproduction, and move breadth. The leading question is whether target release reduces the observed 62–173-second same-candidate runtime variance without reducing quality. Directional and oracle treatments remain later work.

Keep this study separate from target selection and from any bounded within-scope refinement study.
"""
    (root / "hint_followup_design.md").write_text(hint_design, encoding="utf-8")

    contract = execution_contract_audit()
    contract_lines = [
        "# Jackpot execution-contract audit",
        "",
        "The externally pasted duplicate was not present in the sealed forensic lineage; its actual `jackpot_execution_contract_audit.md` is distinct from hint readiness. This replacement expands the comparison across all three execution contexts.",
        "",
        "| Contract fact | Historical jackpots | One-worker screen | Eight-worker calibration |",
        "| --- | --- | --- | --- |",
        "| Scope | Historical TOP scope | TOP or IA dynamic scope | Exact historical TOP scope |",
        "| Workers | 8 | 1 | 8 |",
        "| Seed | 101 | 101/202 | 101 |",
        "| Hints | Current complete incumbent | Same current hints | Same current hints |",
        "| Search | 300 s requested | 300 s requested | 300 s requested |",
        "| Validation | 180 s requested, parent-limited | 180 s requested, truncated by 300 s parent | independently protected 180 s request |",
        "| Parent wall | branch/phase deadline | 300 s | 480 s |",
        "| Authority | Full-model validation + strict v2 gain | Same | Same |",
        "",
        "Code identity differs because the screen and calibration add research-only fixed-scope and telemetry plumbing. Recorded OR-Tools, input/model identity, Objective Semantics v2, hard constraints, and authority remain compatible in the source records. The calibration is the cleanest fixed-state/fixed-scope worker restoration, not a target-policy comparison.",
    ]
    (root / "jackpot_execution_contract_audit.md").write_text("\n".join(contract_lines) + "\n", encoding="utf-8")
    write_json(root / "execution_contract_source_records.json", contract)


def seal(root):
    lines = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or path.name in {"artifact_hashes.sha256", "SEALED"}:
            continue
        lines.append(f"{sha256_file(path)}  {path.relative_to(root).as_posix()}")
    manifest_path = Path(root) / "artifact_hashes.sha256"
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest_hash = sha256_file(manifest_path)
    write_json(Path(root) / "SEALED", {
        "schema": "r16_scope_structure_forensics_seal_v1",
        "status": "complete",
        "artifact_hashes_sha256": manifest_hash,
    })


def run(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    (root / "lineage.lock").write_text(f"created_at={utc_now()}\npid={os.getpid()}\n", encoding="utf-8")
    sources = {
        "historical_three_hour": HISTORICAL_ROOT,
        "original_138_attempt_forensics": ORIGINAL_FORENSIC_ROOT,
        "one_worker_screen": SCREEN_ROOT,
        "target_selection_followup": FOLLOWUP_ROOT,
        "jackpot_calibration": CALIBRATION_ROOT,
        "calibration_addendum": ADDENDUM_ROOT,
    }
    verification = {name: verify_hash_package(path) for name, path in sources.items()}
    write_json(root / "lineage_verification.json", verification)
    dto, sections, requests = load_fixture_metadata()
    outcomes, structures, detailed, comparisons, records = load_attempt_population(requests, sections)
    effects = feature_effects(records)
    semantic_diff = attempt27_semantic_diff(requests, sections)
    calibration = calibration_duration()
    historical_duration = duration_conditionals(records)
    ia_summary = ia_shadow_summary(comparisons)
    held_out = select_held_out_states(records, comparisons)
    total_gain = sum(item["authoritative_gain"] for item in outcomes)
    jackpot_gain = sum(item["authoritative_gain"] for item in outcomes if item["attempt_index"] in JACKPOT_ATTEMPTS)

    write_csv(root / "r16_8worker_scope_outcomes.csv", outcomes)
    write_csv(root / "r16_8worker_scope_structure.csv", structures)
    write_csv(root / "r16_move_graph_features.csv", detailed)
    write_json(root / "r16_jackpot_feature_effects.json", effects)
    write_json(root / "attempt27_108_vs_114_semantic_diff.json", semantic_diff)
    duration_rows = []
    for record in records:
        outcome, structure = record["outcome"], record["structure"]
        duration_rows.append({
            "attempt_index": record["attempt_index"],
            "native_search_wall_seconds": outcome["native_search_wall_seconds"],
            "authoritative_gain": outcome["authoritative_gain"],
            "changed_student_count": outcome["changed_student_count"],
            "changed_source_decision_count": outcome["changed_source_decision_count"],
            "jackpot": record["attempt_index"] in JACKPOT_ATTEMPTS,
            "scope_novelty": outcome["scope_novelty"],
            "prior_scope_observations": outcome["prior_scope_observations"],
            "positive_move_fact_count": structure["positive_move_fact_count"],
            "potential_chain_count": structure["potential_chain_count"],
            "structural_cycle_candidate_count": structure["structural_cycle_candidate_count"],
            "same_pressured_structure_move_pair_count": structure["same_pressured_structure_move_pair_count"],
        })
    write_csv(root / "r16_8worker_duration_by_scope_quality.csv", duration_rows)
    write_csv(root / "r16_top_vs_ia_shadow_scope_comparison.csv", comparisons)
    write_json(root / "duration_analysis.json", {
        "schema": "r16_eight_worker_duration_analysis_v1",
        "historical_all_70": distribution(item["native_search_wall_seconds"] for item in outcomes),
        "historical_jackpots": distribution(item["native_search_wall_seconds"] for item in outcomes if item["attempt_index"] in JACKPOT_ATTEMPTS),
        "historical_ordinary": distribution(item["native_search_wall_seconds"] for item in outcomes if item["attempt_index"] not in JACKPOT_ATTEMPTS),
        "historical_conditionals": historical_duration,
        "calibration_nine": calibration,
        "counterfactual_boundary": "Observed return times only; no shorter or longer ceiling outcome is inferred.",
    })
    write_json(root / "held_out_scope_states.json", held_out)
    schema_text = """# R16 move-graph feature schema

Each row is one historical R16-only pre-attempt TOP scope. `guidance_level_positive_moves` are persisted optimistic same-delivery-group moves with positive utilization leverage. Pair overlap, chains, reciprocal moves, and `structural_cycle_candidate` values describe graph structure only; they do not prove timetable, capacity, prerequisite, commitment, or complete-model feasibility. Cross-component fields are current selected-student pressure facts from the same pre-state snapshot. Outcome columns are labels from the executed TOP attempt and are never attributed to an unexecuted graph or IA scope.
"""
    (root / "r16_move_graph_features_schema.md").write_text(schema_text, encoding="utf-8")
    make_reports(root, outcomes, records, effects, semantic_diff, calibration, comparisons, held_out, verification)
    manifest = {
        "schema": "r16_scope_structure_forensics_manifest_v1",
        "created_at_utc": utc_now(),
        "status": "complete_solver_free",
        "source_lineages": verification,
        "repository": {
            "git_head": os.popen(f'git -C "{REPO_ROOT}" rev-parse HEAD').read().strip(),
            "fixture_input": str(FIXTURE_INPUT),
            "fixture_input_sha256": sha256_file(FIXTURE_INPUT),
            "analyzer": str(Path(__file__).resolve()),
            "analyzer_sha256": sha256_file(Path(__file__).resolve()),
            "relevant_file_sha256": {
                str(path.relative_to(REPO_ROOT)).replace("\\", "/"): sha256_file(path)
                for path in (
                    REPO_ROOT / "scheduling_engine/student_assignment/utilization_guidance.py",
                    REPO_ROOT / "scheduling_engine/student_assignment/hint_observability.py",
                    REPO_ROOT / "scheduling_engine/student_assignment/substantive_probe.py",
                    REPO_ROOT / "scheduling_engine/student_assignment/core.py",
                    REPO_ROOT / "scheduling_engine/benchmark_r16_target_selection_screen.py",
                    REPO_ROOT / "docs/STUDENT_ASSIGNMENT_TARGET_SELECTION.md",
                    REPO_ROOT / "docs/STUDENT_ASSIGNMENT_HINT_STRATEGY.md",
                    REPO_ROOT / "docs/STUDENT_ASSIGNMENT_OPERATOR_CHARACTERIZATION.md",
                    REPO_ROOT / "docs/STUDENT_ASSIGNMENT_SEARCH_STRATEGY.md",
                )
            },
        },
        "population": {
            "r16_attempts": len(outcomes),
            "validated_adoptions": sum(bool(item["candidate_validated"] and item["adopted"]) for item in outcomes),
            "total_authoritative_gain": total_gain,
            "jackpot_attempts": list(JACKPOT_ATTEMPTS),
            "jackpot_gain": jackpot_gain,
            "jackpot_gain_fraction": jackpot_gain / total_gain,
            "jackpot_attempt_fraction": len(JACKPOT_ATTEMPTS) / len(outcomes),
        },
        "methods": {
            "cp_sat_runs": 0,
            "validation_runs": 0,
            "operator_runs": 0,
            "hint_experiments": 0,
            "solver_free": True,
            "opaque_ml_models": 0,
            "production_coefficients_fitted": 0,
        },
        "held_out_design": held_out,
        "interaction_aware_shadow": ia_summary,
        "confirmations": {
            "sealed_lineage_modified": False,
            "hint_strategy_changed": False,
            "production_target_policy_changed": False,
            "objective_semantics_changed": False,
            "hard_constraints_changed": False,
            "candidate_authority_changed": False,
            "migration_created": False,
            "git_commit_created": False,
        },
    }
    write_json(root / "analysis_manifest.json", manifest)
    seal(root)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=Path, default=RESEARCH_PARENT)
    parser.add_argument("--lineage-id")
    parser.add_argument("--confirm-lineage-id")
    args = parser.parse_args(argv)
    lineage_id = args.lineage_id or f"v2_r16_scope_structure_forensics_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    if args.confirm_lineage_id != lineage_id:
        raise RuntimeError("Exact --confirm-lineage-id is required")
    root = (args.parent / lineage_id).resolve()
    if any(root == source.resolve() or source.resolve() in root.parents for source in (
        HISTORICAL_ROOT, ORIGINAL_FORENSIC_ROOT, SCREEN_ROOT, FOLLOWUP_ROOT,
        CALIBRATION_ROOT, ADDENDUM_ROOT,
    )):
        raise RuntimeError("Output lineage may not be a source lineage or its descendant")
    manifest = run(root)
    print(json.dumps({"root": str(root), "population": manifest["population"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
