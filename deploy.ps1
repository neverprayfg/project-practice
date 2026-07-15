$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Assert-LastExit([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

docker version *> $null
Assert-LastExit "Docker Engine 不可用，请先启动 Docker Desktop。"
docker compose version *> $null
Assert-LastExit "当前 Docker 未提供 Compose v2。"
if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
    throw "未找到 tar，无法创建跨路径 Docker 构建上下文。"
}

$Model = Read-Host "模型名称 [deepseek-chat]"
if ([string]::IsNullOrWhiteSpace($Model)) { $Model = "deepseek-chat" }
$BaseUrl = if ([string]::IsNullOrWhiteSpace($env:MODEL_BASE_URL)) {
    "https://api.deepseek.com/v1"
} else {
    $env:MODEL_BASE_URL
}
$SecureApiKey = Read-Host "模型 API Key" -AsSecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureApiKey)
$ApiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
if ([string]::IsNullOrWhiteSpace($ApiKey)) { throw "API Key 不能为空。" }

try {
    Write-Host "`n[1/5] 初始化固定版本依赖"
    git submodule sync --recursive
    Assert-LastExit "子模块同步失败。"
    git submodule update --init --recursive testlib jngen
    Assert-LastExit "子模块拉取失败。"

    Write-Host "`n[2/5] 拉取并核验锁定镜像"
    foreach ($Line in Get-Content "docker/images.lock") {
        if ([string]::IsNullOrWhiteSpace($Line) -or $Line.StartsWith("#")) { continue }
        $Parts = $Line -split '\|', 2
        if (-not $Parts[1].Contains("@sha256:")) { throw "$($Parts[0]) 未使用 digest 锁定。" }
        docker pull $Parts[1]
        Assert-LastExit "镜像拉取失败：$($Parts[0])"
    }

    Write-Host "`n[3/5] 写入 LangGraph 后端模型配置"
    $EnvLines = @(
        "MODEL_MODE=remote"
        "MODEL_BASE_URL=$BaseUrl"
        "MODEL_API_KEY=$ApiKey"
        "MODEL_NAME=$Model"
        "MODEL_TIMEOUT_SECONDS=300"
        "AGENT_MAX_ITERATIONS=4"
    )
    [IO.File]::WriteAllLines(
        (Join-Path $Root ".env"),
        $EnvLines,
        [Text.UTF8Encoding]::new($false)
    )
    $ApiKey = $null

    Write-Host "`n[4/5] 构建后端与受限运行器"
    $BackendContext = Join-Path ([IO.Path]::GetTempPath()) "contest-backend-$([guid]::NewGuid()).tar"
    $RunnerContext = Join-Path ([IO.Path]::GetTempPath()) "contest-runner-$([guid]::NewGuid()).tar"
    try {
        tar -cf $BackendContext backend/pyproject.toml backend/uv.lock backend/app demo前端样式设计 docker/backend.Dockerfile
        Assert-LastExit "后端构建上下文创建失败。"
        & cmd.exe /d /c "docker build --pull=false --progress=plain -t contest-dataset-backend:0.1.0 -f docker/backend.Dockerfile - < `"$BackendContext`""
        Assert-LastExit "后端镜像构建失败。"
        tar -cf $RunnerContext docker/runner/runner.cpp docker/runner.Dockerfile testlib/testlib.h jngen/jngen.h
        Assert-LastExit "runner 构建上下文创建失败。"
        & cmd.exe /d /c "docker build --pull=false --progress=plain --target compiler -t contest-dataset-runner-compiler:0.2.0 -f docker/runner.Dockerfile - < `"$RunnerContext`""
        Assert-LastExit "runner 编译镜像构建失败。"
        & cmd.exe /d /c "docker build --pull=false --progress=plain --target executor -t contest-dataset-runner-executor:0.2.0 -f docker/runner.Dockerfile - < `"$RunnerContext`""
        Assert-LastExit "runner 执行镜像构建失败。"
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $BackendContext, $RunnerContext
    }

    Write-Host "`n[5/5] 启动应用并等待就绪"
    docker compose up -d --no-build docker-api workspace-init backend
    Assert-LastExit "应用启动失败。"
    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
        try {
            Invoke-WebRequest -UseBasicParsing http://localhost:8000/health *> $null
            $Ready = $true
            break
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    if (-not $Ready) { throw "应用未在 60 秒内就绪。" }

    Write-Host "`n部署完成。"
    Write-Host "应用工作台：http://localhost:8000"
    Write-Host "API 文档：http://localhost:8000/docs"
}
finally {
    $ApiKey = $null
}
