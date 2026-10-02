# Start the kiosk web app unauthenticated on 0.0.0.0 as a hidden background process
# (no service wrapper, no auth, no rate limit) plus an operator how-to on C:\.
if (-not $APP_DIR) { $APP_DIR = 'C:\KaminoAI\app' }
if (-not $PORT)    { $PORT = '80' }
$py = Join-Path $APP_DIR '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) { $py = (Get-Command python -ErrorAction Ignore).Source }
# Prerequisite gap, made explicit: with no interpreter the old body handed $null to
# Start-Process and died on "Cannot validate argument on parameter 'FilePath'", which reads like
# a script bug rather than "the app tree another config was supposed to plant is not here".
if (-not $py -or -not (Test-Path -LiteralPath $py)) {
    throw ("no python interpreter for the kiosk app: neither '$APP_DIR\.venv\Scripts\python.exe' " +
           "nor 'python' on PATH exists. Plant the kiosk app tree (or set APP_DIR) before this configuration.")
}
Start-Process -FilePath $py `
    -ArgumentList '-m','uvicorn','main:app','--host','0.0.0.0','--port',$PORT `
    -WorkingDirectory $APP_DIR -WindowStyle Hidden | Out-Null
Set-Content -Path 'C:\How to Start your AI Droid.txt' -Encoding ascii -Value @(
    'How to activate your droid:',
    'Open Powershell and :',
    'lms server start',
    'lms load llama-3.2-3b-instruct',
    'cd ' + $APP_DIR,
    '.\.venv\Scripts\Activate.ps1',
    'python -m uvicorn main:app --host 0.0.0.0 --port ' + $PORT
)
# Startup is a loose Start-Process, not a service: it dies on logout/reboot and has
# no restart policy, so the scored service is fragile by design. It also loses the port to
# anything already bound on it (IIS/http.sys) and says nothing when it does -- that silence is
# part of the planted weakness, so the liveness note below only warns.
$deadline = (Get-Date).AddSeconds(8)
$bound = $false
while ((Get-Date) -lt $deadline) {
    if (Get-NetTCPConnection -State Listen -LocalPort $PORT -ErrorAction Ignore) { $bound = $true; break }
    Start-Sleep -Milliseconds 500
}
if (-not $bound) {
    Write-Warning "kiosk: nothing is listening on port $PORT yet; check for a conflicting listener (IIS/http.sys) or a startup error in the app."
}
