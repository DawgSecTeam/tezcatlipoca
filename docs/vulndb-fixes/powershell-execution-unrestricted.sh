# LocalMachine execution policy + the two policy registry values that survive scope overrides.
#
# Deliberately no Set-ExecutionPolicy call. A nakon step is launched with
# `-ExecutionPolicy Bypass`, i.e. a Process-scope policy more specific than LocalMachine, and in
# that situation Set-ExecutionPolicy raises a *terminating* SecurityException ("the setting is
# overridden by a policy defined at a more specific scope"). Verified on 5.1.20348: -ErrorAction
# Ignore, -ErrorAction SilentlyContinue and try/catch ALL leave $Error.Count=1, and nakon's step
# rc is 1 whenever $Error is non-empty -- so the old one-line body failed every step and never
# reached the three registry writes. Verified too that `Set-ExecutionPolicy -Scope LocalMachine`
# writes exactly one value (reg export diff of HKLM\SOFTWARE\Microsoft\PowerShell is empty apart
# from ExecutionPolicy), so writing that value directly is the same plant with no throw.
$shellIds = 'HKLM:\SOFTWARE\Microsoft\PowerShell\1\ShellIds\Microsoft.PowerShell'
$policyKey = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell'

# -Force on every write keeps the second consecutive pass quiet: the value is already there, and
# a bare New-ItemProperty then fails with "The property already exists."
New-Item -Path $shellIds -Force | Out-Null
New-ItemProperty -Path $shellIds -Name 'ExecutionPolicy' -Value 'Unrestricted' -PropertyType String -Force | Out-Null
New-Item -Path $policyKey -Force | Out-Null
New-ItemProperty -Path $policyKey -Name 'EnableScripts' -Value 1 -PropertyType DWord -Force | Out-Null
