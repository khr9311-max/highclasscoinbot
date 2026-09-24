param(
    [ValidateSet('Prepare', 'StartPaper', 'StartLive', 'Status', 'Stop', 'NotifyStart', 'NotifyStatus', 'NotifyStop')]
    [string]$Action = 'Status',
    [ValidateSet('paper', 'live')]
    [string]$Mode = 'paper',
    [string]$Confirm = ''
)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    switch ($Action) {
        'Prepare'    { python -m btc_spot prepare --mode live }
        'StartPaper' { python -m btc_spot.pc start --mode paper }
        'StartLive'  {
            if ($Confirm -ne 'I_UNDERSTAND_LIVE_SPOT') {
                throw 'Pass -Confirm I_UNDERSTAND_LIVE_SPOT to start actual Spot trading.'
            }
            python -m btc_spot.pc start --mode live --confirm $Confirm
        }
        'Status' { python -m btc_spot.pc status --mode $Mode }
        'Stop'   { python -m btc_spot.pc stop --mode $Mode }
        'NotifyStart'  { python -m btc_spot.notify_pc start }
        'NotifyStatus' { python -m btc_spot.notify_pc status }
        'NotifyStop'   { python -m btc_spot.notify_pc stop }
    }
    if ($LASTEXITCODE -ne 0) { throw 'Spot command failed. Inspect the local status/log.' }
} finally {
    Pop-Location
}
