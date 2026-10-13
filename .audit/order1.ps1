#Requires -Version 7.0
# =====================================================================
# ORDER 1 — one-shot: snapshot, branch, baseline, manifest
# Run as: pwsh -NoProfile -File .audit\order1.ps1
# =====================================================================

$ErrorActionPreference = "Continue"

function Say  { param($m, $c = "White") Write-Host $m -ForegroundColor $c }
function Have { param($n) $null -ne (Get-Command $n -ErrorAction SilentlyContinue) }

# --- 1. locate repo root (walk up to nearest .git) ---
$repoRoot = (Get-Location).Path
while ($repoRoot -and -not (Test-Path (Join-Path $repoRoot ".git"))) {
    $parent = Split-Path $repoRoot -Parent
    if ($parent -eq $repoRoot -or -not $parent) { break }
    $repoRoot = $parent
}
if (-not (Test-Path (Join-Path $repoRoot ".git"))) {
    Say "No git repository found at or above $((Get-Location).Path)" "Red"
    exit 1
}
Set-Location $repoRoot
Say "Repo root: $repoRoot" "Cyan"

# --- 2. audit dir ---
$auditDir    = Join-Path $repoRoot ".audit"
$baselineDir = Join-Path $auditDir "baseline"
New-Item -ItemType Directory -Force -Path $baselineDir | Out-Null

# --- 3. record dirty state but do not bail ---
$dirty = @(git status --porcelain)
if ($dirty.Count -gt 0) {
    $dirty | Set-Content (Join-Path $auditDir "dirty-at-start.txt") -Encoding UTF8
    Say "Working tree is dirty ($($dirty.Count) entries) - recorded to .audit/dirty-at-start.txt" "Yellow"
    Say "Continuing; the audit branch will include these changes." "Yellow"
} else {
    Say "Working tree is clean." "Green"
}

# --- 4. create audit branch (handle collision) ---
$stamp  = Get-Date -Format "yyyyMMdd-HHmmss"
$branch = "audit/extract-patterns-$stamp"
git checkout -b $branch 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    $branch = "audit/extract-patterns-$stamp-$(Get-Random -Maximum 9999)"
    git checkout -b $branch 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Say "Could not create audit branch." "Red"
        exit 1
    }
}
Say "Branch: $branch" "Green"

# --- 5. stack detection ---
$hasGo     = (Test-Path "go.mod")          -and (Have go)
$hasNode   = (Test-Path "package.json")    -and (Have node)
$hasRust   = (Test-Path "Cargo.toml")      -and (Have cargo)
$hasPython = ((Test-Path "pyproject.toml") -or (Test-Path "requirements.txt")) -and (Have python)
$hasDocker = Test-Path "Dockerfile"

$stack = [ordered]@{
    go     = $hasGo
    node   = $hasNode
    rust   = $hasRust
    python = $hasPython
    docker = $hasDocker
}

Say ""
Say "Stack detected:" "Cyan"
$stack.GetEnumerator() | ForEach-Object {
    $mark = if ($_.Value) { "x" } else { " " }
    Say ("  [{0}] {1}" -f $mark, $_.Key)
}

# --- 6. baseline runs ---
Say ""
Say "Running baseline (this may take a while)..." "Cyan"

if ($hasGo) {
    $goDir = Split-Path (Resolve-Path "go.mod") -Parent
    Push-Location $goDir
    try {
        Say "  go build ./..." "DarkGray"
        go build ./... 2>&1 | Out-File (Join-Path $baselineDir "go-build.log") -Encoding UTF8
        Say "  go test ./..." "DarkGray"
        go test ./...  2>&1 | Out-File (Join-Path $baselineDir "go-test.log")  -Encoding UTF8
        Say "  go vet ./..." "DarkGray"
        go vet ./...   2>&1 | Out-File (Join-Path $baselineDir "go-vet.log")   -Encoding UTF8
    } finally { Pop-Location }
}

if ($hasNode) {
    $nodeDir = Split-Path (Resolve-Path "package.json") -Parent
    Push-Location $nodeDir
    try {
        if (Test-Path "pnpm-lock.yaml") {
            Say "  pnpm install + test" "DarkGray"
            pnpm install --frozen-lockfile 2>&1 | Out-File (Join-Path $baselineDir "node-install.log") -Encoding UTF8
            pnpm -s test                   2>&1 | Out-File (Join-Path $baselineDir "node-test.log")    -Encoding UTF8
        } elseif (Test-Path "yarn.lock") {
            Say "  yarn install + test" "DarkGray"
            yarn install --frozen-lockfile 2>&1 | Out-File (Join-Path $baselineDir "node-install.log") -Encoding UTF8
            yarn -s test                   2>&1 | Out-File (Join-Path $baselineDir "node-test.log")    -Encoding UTF8
        } else {
            Say "  npm ci + test" "DarkGray"
            npm ci            2>&1 | Out-File (Join-Path $baselineDir "node-install.log") -Encoding UTF8
            npm test --silent 2>&1 | Out-File (Join-Path $baselineDir "node-test.log")    -Encoding UTF8
        }
    } finally { Pop-Location }
}

if ($hasRust) {
    Say "  cargo test --all" "DarkGray"
    cargo test --all 2>&1 | Out-File (Join-Path $baselineDir "rust-test.log") -Encoding UTF8
}

if ($hasPython) {
    Say "  pytest -q" "DarkGray"
    pytest -q 2>&1 | Out-File (Join-Path $baselineDir "py-test.log") -Encoding UTF8
}

# --- 7. manifest ---
$manifest = [ordered]@{
    repo_root       = $repoRoot
    branch          = $branch
    stamp           = $stamp
    stack           = $stack
    baseline_dir    = $baselineDir
    dirty_at_start  = ($dirty.Count -gt 0)
    dirty_entries   = $dirty.Count
    generated_at    = (Get-Date).ToUniversalTime().ToString("o")
    pattern_targets = @(
        "registry.table_driven",
        "errors.actionable_taxonomy",
        "api.envelope_unwrap",
        "fs.atomic_staged_write",
        "fs.embedded_integrity",
        "db.migration_advisory_lock",
        "tenant.namespace_isolation",
        "llm.chain_ast",
        "llm.summarization_preservation",
        "agent.supervisor_loop_guard",
        "sandbox.policy_preflight",
        "release.post_update_verification"
    )
}
$manifestPath = Join-Path $auditDir "manifest.json"
$manifest | ConvertTo-Json -Depth 5 | Set-Content $manifestPath -Encoding UTF8

Say ""
Say "ORDER 1 COMPLETE" "Green"
Say "  repo     : $repoRoot"
Say "  branch   : $branch"
Say "  baseline : $baselineDir"
Say "  manifest : $manifestPath"
Say ""
Say "Next: run Claude Code and paste ORDER 2." "Cyan"
