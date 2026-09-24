# Binance-only entry point; settings are loaded from binance_coinm_v1/.env.
# Usage: .\run_binance.ps1 [run|check|status|gate|backtest|validate] [options]
$ErrorActionPreference = 'Stop'
$botArguments = @($args)
if ($botArguments.Count -eq 0) {
    $botArguments = @('run')
}
Push-Location -LiteralPath $PSScriptRoot
try {
    & python -m binance_coinm_v1 @botArguments
    $botExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $botExitCode
