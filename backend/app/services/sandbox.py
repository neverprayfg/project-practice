from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import docker
from docker.errors import DockerException
from requests.exceptions import RequestException

from app.config import Settings
from app.errors import AppError
from app.models import SandboxResult


@dataclass(frozen=True)
class GenerationJob:
    subtask_id: int
    seed: int
    output_relative: str
    case_id: int = 1
    runtime_arguments: dict[str, str] | None = None


@dataclass(frozen=True)
class GenerationOutcome:
    output_relative: str
    result: SandboxResult


@dataclass(frozen=True)
class ValidationJob:
    input_relative: str
    output_relative: str


@dataclass(frozen=True)
class ValidationOutcome:
    input_relative: str
    output_relative: str
    validation: SandboxResult
    solution: SandboxResult | None


class Sandbox(Protocol):
    async def compile(self, project_id: str, role: str) -> SandboxResult: ...

    async def generate(
        self, project_id: str, subtask_id: int, seed: int, output_relative: str
    ) -> SandboxResult: ...

    async def generate_batch(
        self, project_id: str, jobs: list[GenerationJob]
    ) -> list[GenerationOutcome]: ...

    async def validate(self, project_id: str, input_relative: str) -> SandboxResult: ...

    async def solve(
        self, project_id: str, input_relative: str, output_relative: str
    ) -> SandboxResult: ...

    async def validate_solve_batch(
        self, project_id: str, jobs: list[ValidationJob]
    ) -> list[ValidationOutcome]: ...


@dataclass(frozen=True)
class _CompileCacheEntry:
    source_key: str
    binary_digest: str


class DockerSandbox:
    ALLOWED_ROLES = {"solution", "generator", "validator"}
    SOURCE_BY_ROLE = {
        "solution": "source/solution.cpp",
        "generator": "generated/generator.cpp",
        "validator": "generated/validator.cpp",
    }
    COMPILER_FINGERPRINT = "g++-std=c++17-O2-pipe-Wall-Wextra-testlib-jngen-pch-v2"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._slots = asyncio.Semaphore(settings.runner_concurrency)
        self._compile_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._compile_cache: dict[tuple[str, str], _CompileCacheEntry] = {}
        try:
            client = self._new_client()
            client.close()
        except DockerException as exc:
            raise AppError(
                "SANDBOX_UNAVAILABLE",
                "Docker client initialization failed",
                status_code=503,
                details=str(exc),
            ) from exc

    async def compile(self, project_id: str, role: str) -> SandboxResult:
        if role not in self.ALLOWED_ROLES:
            raise AppError("TOOL_DENIED", "unsupported compile role", details={"role": role})
        self._validate_project_id(project_id)
        cache_key = (project_id, role)
        async with self._compile_locks[cache_key]:
            source_key = await asyncio.to_thread(self._source_key, project_id, role)
            cached = self._compile_cache.get(cache_key)
            if cached and cached.source_key == source_key:
                binary = self._binary_path(project_id, role)
                binary_digest = await asyncio.to_thread(self._digest_file, binary)
                if binary_digest and binary_digest == cached.binary_digest:
                    return SandboxResult(
                        ok=True,
                        exit_code=0,
                        stdout="compile cache hit",
                    )

            result = await self._run_result(
                ["compile", project_id, role],
                self.settings.runner_compile_image,
            )
            if result.ok:
                binary_digest = await asyncio.to_thread(
                    self._digest_file, self._binary_path(project_id, role)
                )
                if binary_digest:
                    self._compile_cache[cache_key] = _CompileCacheEntry(
                        source_key=source_key,
                        binary_digest=binary_digest,
                    )
            return result

    async def generate(
        self, project_id: str, subtask_id: int, seed: int, output_relative: str
    ) -> SandboxResult:
        outcomes = await self.generate_batch(
            project_id,
            [GenerationJob(subtask_id, seed, output_relative)],
        )
        return outcomes[0].result

    async def generate_batch(
        self, project_id: str, jobs: list[GenerationJob]
    ) -> list[GenerationOutcome]:
        self._validate_project_id(project_id)
        if not jobs:
            return []
        if len(jobs) > self.settings.runner_batch_size:
            raise AppError("TOOL_DENIED", "runner batch exceeds configured size")
        arguments = ["generate-batch", project_id]
        expected: set[str] = set()
        for job in jobs:
            if job.subtask_id <= 0:
                raise AppError("TOOL_DENIED", "subtask id must be positive")
            if job.case_id <= 0:
                raise AppError("TOOL_DENIED", "case id must be positive")
            self._validate_relative(job.output_relative, {"preview", "data"})
            if job.output_relative in expected:
                raise AppError("TOOL_DENIED", "duplicate batch output path")
            expected.add(job.output_relative)
            runtime_arguments = self._validated_runtime_arguments(
                job.runtime_arguments or {}
            )
            serialized = ";".join(
                f"{name}={value}" for name, value in sorted(runtime_arguments.items())
            )
            arguments.append(
                f"{job.subtask_id}|{job.case_id}|{job.seed}|"
                f"{job.output_relative}|{serialized}"
            )
        payload = await self._run_payload(arguments, self.settings.runner_execute_image)
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise self._invalid_runner_response("batch results are missing")
        outcomes = [
            GenerationOutcome(
                output_relative=str(item["output_file"]),
                result=SandboxResult.model_validate(item["result"]),
            )
            for item in raw_results
        ]
        if {item.output_relative for item in outcomes} != expected:
            raise self._invalid_runner_response("batch generation result paths do not match")
        return outcomes

    async def validate(self, project_id: str, input_relative: str) -> SandboxResult:
        self._validate_relative(input_relative, {"preview", "data"})
        return await self._run_result(
            ["validate", project_id, input_relative],
            self.settings.runner_execute_image,
        )

    async def solve(
        self, project_id: str, input_relative: str, output_relative: str
    ) -> SandboxResult:
        self._validate_relative(input_relative, {"preview", "data"})
        self._validate_relative(output_relative, {"preview", "data"})
        return await self._run_result(
            ["solve", project_id, input_relative, output_relative],
            self.settings.runner_execute_image,
        )

    async def validate_solve_batch(
        self, project_id: str, jobs: list[ValidationJob]
    ) -> list[ValidationOutcome]:
        self._validate_project_id(project_id)
        if not jobs:
            return []
        if len(jobs) > self.settings.runner_batch_size:
            raise AppError("TOOL_DENIED", "runner batch exceeds configured size")
        arguments = ["validate-solve-batch", project_id]
        expected: set[tuple[str, str]] = set()
        for job in jobs:
            self._validate_relative(job.input_relative, {"preview", "data"})
            self._validate_relative(job.output_relative, {"preview", "data"})
            key = (job.input_relative, job.output_relative)
            if key in expected:
                raise AppError("TOOL_DENIED", "duplicate validation batch path")
            expected.add(key)
            arguments.append(f"{job.input_relative}|{job.output_relative}")
        payload = await self._run_payload(arguments, self.settings.runner_execute_image)
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise self._invalid_runner_response("batch results are missing")
        outcomes = [
            ValidationOutcome(
                input_relative=str(item["input_file"]),
                output_relative=str(item["output_file"]),
                validation=SandboxResult.model_validate(item["validation"]),
                solution=(
                    SandboxResult.model_validate(item["solution"])
                    if item.get("solution") is not None
                    else None
                ),
            )
            for item in raw_results
        ]
        if {(item.input_relative, item.output_relative) for item in outcomes} != expected:
            raise self._invalid_runner_response("batch validation result paths do not match")
        return outcomes

    async def _run_result(self, command: list[str], image: str) -> SandboxResult:
        return SandboxResult.model_validate(await self._run_payload(command, image))

    async def _run_payload(self, command: list[str], image: str) -> dict[str, Any]:
        async with self._slots:
            return await asyncio.to_thread(self._run_sync, command, image)

    def _run_sync(self, command: list[str], image: str) -> dict[str, Any]:
        client = self._new_client()
        try:
            output = client.containers.run(
                image,
                command=command,
                remove=True,
                network_disabled=True,
                read_only=True,
                user="runner",
                working_dir="/workspace",
                volumes={self.settings.storage_volume: {"bind": "/workspace", "mode": "rw"}},
                tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
                mem_limit=self.settings.runner_memory,
                ulimits=[
                    docker.types.Ulimit(
                        name="stack",
                        soft=self.settings.runner_stack_soft_bytes,
                        hard=-1,
                    )
                ],
                nano_cpus=self.settings.runner_nano_cpus,
                pids_limit=128,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                environment={
                    "RUNNER_TIMEOUT_SECONDS": str(self.settings.runner_timeout_seconds)
                },
            )
            line = output.decode("utf-8", errors="replace").strip().splitlines()[-1]
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("runner response is not an object")
            return value
        except (
            DockerException,
            RequestException,
            ValueError,
            IndexError,
            json.JSONDecodeError,
        ) as exc:
            raise AppError(
                "SANDBOX_UNAVAILABLE",
                "runner container failed to return a valid result",
                status_code=503,
                details=str(exc)[:1000],
            ) from exc
        finally:
            client.close()

    def _new_client(self) -> docker.DockerClient:
        timeout = self._docker_request_timeout()
        if self.settings.docker_host:
            return docker.DockerClient(base_url=self.settings.docker_host, timeout=timeout)
        return docker.from_env(timeout=timeout)

    def _docker_request_timeout(self) -> int:
        # A batch may run validation and solution once per item. The Docker HTTP
        # connection must outlive those bounded child-process timeouts so their
        # structured results can be returned to the agent.
        return max(
            60,
            self.settings.runner_timeout_seconds * self.settings.runner_batch_size * 2
            + 60,
        )

    def _source_key(self, project_id: str, role: str) -> str:
        source = self.settings.storage_root / project_id / self.SOURCE_BY_ROLE[role]
        digest = hashlib.sha256()
        digest.update(self.settings.runner_compile_image.encode())
        digest.update(self.COMPILER_FINGERPRINT.encode())
        digest.update(role.encode())
        if source.is_file():
            digest.update(source.read_bytes())
        return digest.hexdigest()

    def _binary_path(self, project_id: str, role: str) -> Path:
        return self.settings.storage_root / project_id / "bin" / role

    @staticmethod
    def _digest_file(path: Path) -> str | None:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _validate_project_id(project_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", project_id):
            raise AppError("TOOL_DENIED", "invalid project id")

    @staticmethod
    def _validate_relative(value: str, roots: set[str]) -> None:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] not in roots:
            raise AppError("TOOL_DENIED", "invalid managed workspace path")

    @staticmethod
    def _validated_runtime_arguments(arguments: dict[str, str]) -> dict[str, str]:
        if len(arguments) > 24:
            raise AppError("TOOL_DENIED", "too many generator runtime arguments")
        reserved = {"seed", "subtask", "case"}
        for name, value in arguments.items():
            if name in reserved or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
                raise AppError("TOOL_DENIED", "invalid generator runtime argument name")
            if not re.fullmatch(r"[A-Za-z0-9_.:+-]{1,64}", value):
                raise AppError("TOOL_DENIED", "invalid generator runtime argument value")
        return arguments

    @staticmethod
    def _invalid_runner_response(message: str) -> AppError:
        return AppError("SANDBOX_UNAVAILABLE", message, status_code=503)


class UnavailableSandbox:
    """Used only when app startup cannot reach Docker; every operation fails closed."""

    async def compile(self, project_id: str, role: str) -> SandboxResult:
        del project_id, role
        return self._raise()

    async def generate(
        self, project_id: str, subtask_id: int, seed: int, output_relative: str
    ) -> SandboxResult:
        del project_id, subtask_id, seed, output_relative
        return self._raise()

    async def generate_batch(
        self, project_id: str, jobs: list[GenerationJob]
    ) -> list[GenerationOutcome]:
        del project_id, jobs
        return self._raise()

    async def validate(self, project_id: str, input_relative: str) -> SandboxResult:
        del project_id, input_relative
        return self._raise()

    async def solve(
        self, project_id: str, input_relative: str, output_relative: str
    ) -> SandboxResult:
        del project_id, input_relative, output_relative
        return self._raise()

    async def validate_solve_batch(
        self, project_id: str, jobs: list[ValidationJob]
    ) -> list[ValidationOutcome]:
        del project_id, jobs
        return self._raise()

    @staticmethod
    def _raise() -> Any:
        raise AppError(
            "SANDBOX_UNAVAILABLE",
            "Docker is unavailable; no host fallback is permitted",
            status_code=503,
        )
