from __future__ import annotations

from app.docker_proxy import is_allowed


def test_docker_proxy_exposes_only_runner_lifecycle() -> None:
    assert is_allowed("GET", "/version")
    assert is_allowed("POST", "/v1.47/containers/create")
    assert is_allowed("POST", "/v1.47/containers/abc123/start")
    assert is_allowed("DELETE", "/v1.47/containers/abc123?force=1")
    assert not is_allowed("POST", "/v1.47/containers/abc123/exec")
    assert not is_allowed("POST", "/v1.47/build")
    assert not is_allowed("POST", "/v1.47/images/create?fromImage=busybox")
    assert not is_allowed("GET", "/v1.47/volumes")
