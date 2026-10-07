# Build the three linux/arm64 images locally and push them to the ECR repositories created by 02-artifacts.
#   usage: scripts\build_and_push_images.ps1 -EnvName poc -Tag v1 [-Region us-east-1]
#   then:  python -m cloudformation.deploy images --env poc --image-tag v1
param([Parameter(Mandatory)][string]$EnvName, [Parameter(Mandatory)][string]$Tag, [string]$Region = "us-east-1")
$ErrorActionPreference = "Stop"
$account = aws sts get-caller-identity --query Account --output text
$registry = "$account.dkr.ecr.$Region.amazonaws.com"
$prefix = "portfolio-copilot-$EnvName"
Set-Location (Join-Path $PSScriptRoot "..")
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $registry
if ($LASTEXITCODE -ne 0) { throw "docker login failed" }

$images = @(
  @{ Repo = "quant-mcp";      Module = "src.quant_mcp.app";      Port = 8000 },
  @{ Repo = "market-analyst"; Module = "src.market_analyst.app"; Port = 9000 },
  @{ Repo = "orchestrator";   Module = "src.orchestrator.agent"; Port = 8080 }
)
foreach ($i in $images) {
  $uri = "$registry/$prefix/$($i.Repo):$Tag"
  Write-Host ">> $uri"
  docker buildx build --platform linux/arm64 --provenance=false -f docker/Dockerfile `
    --build-arg "APP_MODULE=$($i.Module)" --build-arg "APP_PORT=$($i.Port)" --build-arg "SERVICE_NAME=$($i.Repo)" `
    -t $uri --push .
  if ($LASTEXITCODE -ne 0) { throw "build failed for $($i.Repo)" }
}
Write-Host "pushed :$Tag to $registry/$prefix/{quant-mcp,market-analyst,orchestrator}"
