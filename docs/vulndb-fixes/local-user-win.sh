if (-not $USERNAME) { throw 'USERNAME is required' }
# nakon renders every var as a single-quoted string (gen/powershell.py render_step_ps1), so the
# '1' flags below arrive as [string]. Compare with -eq '1' -- the old `-eq 1` silently no-opped.
$wantNeverExpires = ("$PASSWORD_NEVER_EXPIRES" -eq '1')
$wantAdmin        = ("$ADMIN_ADD" -eq '1')
$existing = Get-LocalUser -Name $USERNAME -ErrorAction Ignore
if ($existing) {
    if ($PASSWORD) {
        $secure = ConvertTo-SecureString $PASSWORD -AsPlainText -Force
        Set-LocalUser -Name $USERNAME -Password $secure
    }
    $u = @{}
    if ($FULLNAME) { $u.FullName = $FULLNAME }
    if ($DESCRIPTION) { $u.Description = $DESCRIPTION }
    if ($u.Count) { Set-LocalUser -Name $USERNAME @u }
} else {
    $params = @{ Name = $USERNAME; AccountNeverExpires = $true }
    if ($FULLNAME) { $params.FullName = $FULLNAME }
    if ($DESCRIPTION) { $params.Description = $DESCRIPTION }
    if ($PASSWORD) {
        $secure = ConvertTo-SecureString $PASSWORD -AsPlainText -Force
        New-LocalUser @params -Password $secure | Out-Null
    } else {
        New-LocalUser @params -NoPassword | Out-Null
    }
}
# Re-assert the flags on both paths: an account created on an earlier pass must still end this
# pass with the property the caller asked for (the old body only set it on the create path and
# only when the string compared equal to the integer 1).
if ($wantNeverExpires) {
    Set-LocalUser -Name $USERNAME -PasswordNeverExpires $true
}
if ($wantAdmin) {
    Add-LocalGroupMember -Group 'Administrators' -Member $USERNAME -ErrorAction Ignore
}
