from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from pathlib import Path
from threading import Lock

import pytest

from app.config import Settings
from app.models import SandboxResult
from app.services.sandbox import DockerSandbox


def bare_sandbox(settings: Settings) -> DockerSandbox:
    sandbox = object.__new__(DockerSandbox)
    sandbox.settings = settings
    sandbox._slots = asyncio.Semaphore(settings.runner_concurrency)
    sandbox._compile_locks = defaultdict(asyncio.Lock)
    sandbox._compile_cache = {}
    return sandbox


def test_runner_container_keeps_all_security_limits(tmp_path: Path) -> None:
    settings = Settings(storage_root=tmp_path, model_mode="mock")
    sandbox = bare_sandbox(settings)
    captured: dict = {}

    class FakeContainers:
        @staticmethod
        def run(image: str, **kwargs: object) -> bytes:
            captured["image"] = image
            captured.update(kwargs)
            return b'{"ok":true,"exit_code":0,"stdout":"","stderr":""}\n'

    class FakeClient:
        containers = FakeContainers()

        @staticmethod
        def close() -> None:
            captured["closed"] = True

    sandbox._new_client = lambda: FakeClient()  # type: ignore[method-assign]

    result = sandbox._run_sync(["compile", "a" * 32, "generator"], "runner-image")

    assert result["ok"] is True
    assert captured["image"] == "runner-image"
    assert captured["remove"] is True
    assert captured["network_disabled"] is True
    assert captured["read_only"] is True
    assert captured["user"] == "runner"
    assert captured["working_dir"] == "/workspace"
    assert captured["volumes"] == {
        settings.storage_volume: {"bind": "/workspace", "mode": "rw"}
    }
    assert captured["tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=64m"}
    assert captured["mem_limit"] == settings.runner_memory
    assert captured["nano_cpus"] == settings.runner_nano_cpus
    assert captured["pids_limit"] == 128
    assert captured["cap_drop"] == ["ALL"]
    assert captured["security_opt"] == ["no-new-privileges"]
    assert captured["environment"] == {
        "RUNNER_TIMEOUT_SECONDS": str(settings.runner_timeout_seconds)
    }
    assert captured["closed"] is True


def test_docker_request_timeout_outlives_largest_two_step_batch(tmp_path: Path) -> None:
    settings = Settings(
        storage_root=tmp_path,
        model_mode="mock",
        runner_timeout_seconds=30,
        runner_batch_size=16,
    )
    sandbox = bare_sandbox(settings)

    assert sandbox._docker_request_timeout() == 1020


@pytest.mark.asyncio
async def test_sync_runner_is_offloaded_and_limited_to_configured_concurrency(
    tmp_path: Path,
) -> None:
    sandbox = bare_sandbox(
        Settings(storage_root=tmp_path, runner_concurrency=2, model_mode="mock")
    )
    active = 0
    maximum = 0
    ticks = 0
    lock = Lock()

    def run_sync(_command: list[str], _image: str) -> dict:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.06)
        with lock:
            active -= 1
        return {"ok": True}

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.01)
            ticks += 1

    sandbox._run_sync = run_sync  # type: ignore[method-assign]
    await asyncio.gather(
        ticker(),
        *(sandbox._run_payload(["test"], "image") for _ in range(4)),
    )

    assert maximum == 2
    assert ticks == 10


@pytest.mark.asyncio
async def test_compile_cache_detects_source_changes_and_binary_tampering(
    tmp_path: Path,
) -> None:
    settings = Settings(storage_root=tmp_path, model_mode="mock")
    sandbox = bare_sandbox(settings)
    project_id = "a" * 32
    source = tmp_path / project_id / "generated" / "generator.cpp"
    binary = tmp_path / project_id / "bin" / "generator"
    source.parent.mkdir(parents=True)
    binary.parent.mkdir(parents=True)
    source.write_text("int main(){}", encoding="utf-8")
    compile_calls = 0

    async def compile_once(_command: list[str], _image: str) -> SandboxResult:
        nonlocal compile_calls
        compile_calls += 1
        binary.write_bytes(f"binary-{compile_calls}".encode())
        return SandboxResult(ok=True, exit_code=0)

    sandbox._run_result = compile_once  # type: ignore[method-assign]

    first = await sandbox.compile(project_id, "generator")
    second = await sandbox.compile(project_id, "generator")
    source.write_text("int main(){return 1;}", encoding="utf-8")
    third = await sandbox.compile(project_id, "generator")
    binary.write_bytes(b"tampered")
    fourth = await sandbox.compile(project_id, "generator")

    assert first.ok and third.ok and fourth.ok
    assert second.stdout == "compile cache hit"
    assert compile_calls == 3
