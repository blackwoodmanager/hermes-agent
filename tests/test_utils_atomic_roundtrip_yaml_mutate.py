"""Regression tests for bounded atomic round-trip YAML transactions."""

import os
import subprocess
import sys

import pytest
import yaml

import utils


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX flock holder")
def test_atomic_roundtrip_yaml_mutate_times_out_on_held_sidecar_lock(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("value: old\n", encoding="utf-8")
    lock_path = target.with_name(f".{target.name}.lock")
    script = (
        "import fcntl,sys,time; "
        "f=open(sys.argv[1], 'a+'); "
        "fcntl.flock(f, fcntl.LOCK_EX); "
        "print('locked', flush=True); time.sleep(5)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(TimeoutError, match="YAML transaction lock"):
            utils.atomic_roundtrip_yaml_mutate(
                target, lambda document: document.update(value="new"), lock_timeout=0.05
            )
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    assert yaml.safe_load(target.read_text(encoding="utf-8")) == {"value": "old"}


def test_atomic_roundtrip_yaml_mutate_is_reentrant_for_same_path(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("value: old\n", encoding="utf-8")

    def outer(document):
        utils.atomic_roundtrip_yaml_mutate(
            target, lambda nested: nested.update(nested=True), lock_timeout=0.05
        )
        document["value"] = "outer"

    utils.atomic_roundtrip_yaml_mutate(target, outer, lock_timeout=0.05)

    assert yaml.safe_load(target.read_text(encoding="utf-8"))["value"] == "outer"


def test_unlock_error_does_not_mask_mutator_error(tmp_path, monkeypatch):
    target = tmp_path / "config.yaml"
    target.write_text("value: old\n", encoding="utf-8")

    def fail_unlock(_handle):
        raise OSError("unlock failed")

    monkeypatch.setattr(utils, "_release_yaml_transaction_file_lock", fail_unlock)

    def fail_mutation(_document):
        raise RuntimeError("mutation failed")

    with pytest.raises(RuntimeError, match="mutation failed"):
        utils.atomic_roundtrip_yaml_mutate(target, fail_mutation)
