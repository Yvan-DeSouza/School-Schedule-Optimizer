import json
import os

from scheduling_engine.benchmark_r16_jackpot_calibration import (
    JACKPOT_ATTEMPTS,
    PARENT_SECONDS,
    SEARCH_SECONDS,
    SEED,
    VALIDATION_SECONDS,
    VALIDATION_WORKERS,
    WORKERS,
    append_resource_sample,
)


def test_jackpot_calibration_contract_reserves_validation_budget():
    assert JACKPOT_ATTEMPTS == (27, 36, 63)
    assert SEED == 101
    assert WORKERS == 8
    assert VALIDATION_WORKERS == 1
    assert SEARCH_SECONDS == 300.0
    assert VALIDATION_SECONDS == 180.0
    assert PARENT_SECONDS == SEARCH_SECONDS + VALIDATION_SECONDS


def test_resource_sample_is_streamable_and_process_scoped(tmp_path):
    path = tmp_path / "resource_samples.jsonl"
    append_resource_sample(
        path,
        root_pid=os.getpid(),
        context={"attempt": 1, "repeat": 1},
    )
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["schema"] == "r16_jackpot_calibration_resource_sample_v1"
    assert record["attempt"] == 1
    assert record["repeat"] == 1
    assert record["root_pid"] == os.getpid()
    assert record["descendant_pids"]
