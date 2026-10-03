# Exposes RPC over HTTPS (RpcProxy) virtual directories on the default web site.
# depends_on "IIS HTTP" is a real prerequisite: without WebAdministration/New-WebApplication this
# body can do nothing, so say so instead of dying on a command-not-found (the old body also
# installed 'RSAT-Rpc-Proxy', which is not a feature on any Server 2012+ SKU, and the silent
# failure meant the real role was never installed).
$ProgressPreference = 'SilentlyContinue'
Import-Module WebAdministration -ErrorAction Stop

$iisFeature = Get-WindowsFeature -Name Web-Server -ErrorAction SilentlyContinue
if (-not (Get-Service W3SVC -ErrorAction SilentlyContinue)) {
    if ($iisFeature -and $iisFeature.InstallState -ne 'Installed') {
        throw "IIS (Web-Server) is not installed on this box and it is not installed automatically here; add the 'IIS HTTP' dependency before this configuration."
    }
    throw "W3SVC is absent; IIS (Web-Server) is not present on this box. Add the 'IIS HTTP' dependency before this configuration."
}

# RPC over HTTP must be a server role, not the RSAT client feature the old body asked for.
$rpcFeature = Get-WindowsFeature -Name RPC-over-HTTP-Proxy -ErrorAction SilentlyContinue
if ($rpcFeature -and $rpcFeature.InstallState -ne 'Installed') {
    Install-WindowsFeature -Name RPC-over-HTTP-Proxy -WarningAction SilentlyContinue | Out-Null
}

$pool = 'DefaultAppPool'
foreach ($v in @('Rpc','RpcWithCert')) {
    # -Force makes the pool assignment authoritative on a re-run, and | Out-Null keeps the
    # cmdlet's result object out of the step's stdout stream.
    if (-not (Test-Path "IIS:\Sites\Default Web Site\$v")) {
        New-WebApplication -Site 'Default Web Site' -Name $v `
            -PhysicalPath "$env:windir\System32\RpcProxy" -ApplicationPool $pool -Force | Out-Null
    } else {
        Set-ItemProperty "IIS:\Sites\Default Web Site\$v" -Name applicationPool -Value $pool -ErrorAction SilentlyContinue | Out-Null
    }
}

# The role install writes the RpcProxy activation keys itself
# (HKLM:\SOFTWARE\Microsoft\Rpc\RpcProxy Enabled=1, ValidPorts=...), but ValidPorts is what
# actually makes the proxied endpoints reachable and it is easy to end up with an empty one
# (the role only fills it while it installs). Re-assert both so a re-run cannot leave a shell
# that 401s/empty-proxies instead of the intended RPC tunnel.
$rpcProxyKey = 'HKLM:\SOFTWARE\Microsoft\Rpc\RpcProxy'
New-Item -Path $rpcProxyKey -Force | Out-Null
New-ItemProperty -Path $rpcProxyKey -Name 'Enabled' -Value 1 -PropertyType DWord -Force | Out-Null
if (-not (Get-ItemProperty -Path $rpcProxyKey -Name 'ValidPorts' -ErrorAction Ignore).ValidPorts) {
    $validPorts = "$env:COMPUTERNAME" + ':593;' + "$env:COMPUTERNAME" + ':49152-65535'
    New-ItemProperty -Path $rpcProxyKey -Name 'ValidPorts' -Value $validPorts -PropertyType String -Force | Out-Null
}

Restart-Service W3SVC -Force -ErrorAction SilentlyContinue
# RpcProxy on a DC web site turns it into an authenticated tunnel into inner-ring RPC endpoints
# (MS-RPCH / Outlook-Anywhere-style exposure).
