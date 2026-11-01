<#
.SYNOPSIS
Give one local account the ETW access that the live ETW tests need without elevation, show it, or undo it.

.DESCRIPTION
The ETW consumer of the recording step starts a system logger session with the kernel Process flag and enables
Microsoft-Windows-Kernel-Process and Microsoft-Windows-Kernel-File. Without elevation, an account may do that once it
holds three settings:

- membership of Performance Log Users (S-1-5-32-559), which takes effect at the account's next log-on;
- an allow entry with the mask 0x0081 on Microsoft-Windows-Kernel-File and on Microsoft-Windows-Kernel-Process;
- an allow entry with the mask 0x0FF7 on SystemTraceProvider.

The allow entries are stored in HKLM\SYSTEM\CurrentControlSet\Control\WMI\Security. Every action runs from an elevated
PowerShell, since reading and writing those stored descriptors needs administrator rights; the script never requests
elevation itself.

- inspect lists the stored entries of the three providers and of the default descriptor with their account and mask,
  and the account's membership. It changes nothing.
- setup records the prior state, once, and then gives the account the three settings. A repeated setup adds no
  duplicate entries and keeps the first record.
- restore writes the recorded state back, removes the membership only when the setup added it, and removes the record.
  Without a record it changes nothing.

The record lives in HKLM\SOFTWARE\SharedWorkflows\EtwAccess\<account SID>, outside every checkout, so a restore from
another checkout of the repository finds it.

.PARAMETER Action
inspect, setup or restore.

.PARAMETER Account
The local account to give the settings to, such as MACHINE\name; the account that runs the script by default.

.EXAMPLE
pwsh -NoProfile -File tests/etw_access.ps1 setup
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('inspect', 'setup', 'restore')]
    [string]$Action,

    [Parameter(Position = 1)]
    [string]$Account
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$SecurityPath = 'SYSTEM\CurrentControlSet\Control\WMI\Security'
$RecordPath = 'SOFTWARE\SharedWorkflows\EtwAccess'
$PerformanceLogUsers = 'S-1-5-32-559'
$DefaultDescriptor = '0811c1af-7a07-4a06-82ed-869455cdf713'
$Providers = [ordered]@{
    'edd08927-9cc4-4e65-b970-c2560fb5c289' = @{ Name = 'Microsoft-Windows-Kernel-File'; Mask = 0x0081 }
    '22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716' = @{ Name = 'Microsoft-Windows-Kernel-Process'; Mask = 0x0081 }
    '9e814aad-3204-11d2-9a82-006008a86939' = @{ Name = 'SystemTraceProvider'; Mask = 0x0FF7 }
}
$EventSecurityAddDACL = 2
$ErrorMemberInAlias = 1378
$ErrorMemberNotInAlias = 1377

$principal = New-Object System.Security.Principal.WindowsPrincipal([System.Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    [Console]::Error.WriteLine("The $Action action reads the stored ETW security descriptors, which needs administrator rights: " +
        'start it from an elevated PowerShell. This script does not request elevation itself.')
    exit 1
}

Add-Type -Namespace SharedWorkflows -Name EtwAccessNative -MemberDefinition @'
[DllImport("advapi32.dll")]
public static extern uint EventAccessControl(ref Guid guid, uint operation, byte[] sid, uint rights, [MarshalAs(UnmanagedType.U1)] bool allow);

[DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
public static extern uint NetLocalGroupAddMembers(string server, string group, uint level, IntPtr[] members, uint count);

[DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
public static extern uint NetLocalGroupDelMembers(string server, string group, uint level, IntPtr[] members, uint count);

[DllImport("netapi32.dll", CharSet = CharSet.Unicode)]
public static extern uint NetLocalGroupGetMembers(string server, string group, uint level, out IntPtr buffer, int maximum,
    out uint read, out uint total, IntPtr resume);

[DllImport("netapi32.dll")]
public static extern uint NetApiBufferFree(IntPtr buffer);
'@

function Resolve-AccountSid {
    if ($Account) {
        return (New-Object System.Security.Principal.NTAccount($Account)).Translate([System.Security.Principal.SecurityIdentifier])
    }
    return [System.Security.Principal.WindowsIdentity]::GetCurrent().User
}

function Get-AccountName([System.Security.Principal.SecurityIdentifier]$Sid) {
    try { return $Sid.Translate([System.Security.Principal.NTAccount]).Value } catch { return $Sid.Value }
}

function Get-GroupName {
    # The local name of Performance Log Users depends on the system language.
    $name = (New-Object System.Security.Principal.SecurityIdentifier($PerformanceLogUsers)).Translate([System.Security.Principal.NTAccount]).Value
    return $name.Substring($name.IndexOf('\') + 1)
}

function Test-Member([System.Security.Principal.SecurityIdentifier]$Sid) {
    $buffer = [IntPtr]::Zero
    $read = [uint32]0
    $total = [uint32]0
    $status = [SharedWorkflows.EtwAccessNative]::NetLocalGroupGetMembers($null, (Get-GroupName), 0, [ref]$buffer, -1, [ref]$read, [ref]$total, [IntPtr]::Zero)
    if ($status -ne 0) { throw "NetLocalGroupGetMembers failed with $status" }
    try {
        for ($index = 0; $index -lt $read; $index++) {
            $member = New-Object System.Security.Principal.SecurityIdentifier([System.Runtime.InteropServices.Marshal]::ReadIntPtr($buffer, $index * [IntPtr]::Size))
            if ($member -eq $Sid) { return $true }
        }
        return $false
    } finally {
        [void][SharedWorkflows.EtwAccessNative]::NetApiBufferFree($buffer)
    }
}

function Set-Membership([System.Security.Principal.SecurityIdentifier]$Sid, [bool]$Member) {
    $bytes = New-Object byte[] $Sid.BinaryLength
    $Sid.GetBinaryForm($bytes, 0)
    $pointer = [System.Runtime.InteropServices.Marshal]::AllocHGlobal($bytes.Length)
    try {
        [System.Runtime.InteropServices.Marshal]::Copy($bytes, 0, $pointer, $bytes.Length)
        if ($Member) {
            $status = [SharedWorkflows.EtwAccessNative]::NetLocalGroupAddMembers($null, (Get-GroupName), 0, @($pointer), 1)
            if ($status -ne 0 -and $status -ne $ErrorMemberInAlias) { throw "NetLocalGroupAddMembers failed with $status" }
        } else {
            $status = [SharedWorkflows.EtwAccessNative]::NetLocalGroupDelMembers($null, (Get-GroupName), 0, @($pointer), 1)
            if ($status -ne 0 -and $status -ne $ErrorMemberNotInAlias) { throw "NetLocalGroupDelMembers failed with $status" }
        }
    } finally {
        [System.Runtime.InteropServices.Marshal]::FreeHGlobal($pointer)
    }
}

function Get-StoredDescriptor([string]$Guid) {
    $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($SecurityPath, $false)
    if ($null -eq $key) { return $null }
    try { return $key.GetValue($Guid, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) } finally { $key.Dispose() }
}

function Get-Entries([byte[]]$Descriptor) {
    if ($null -eq $Descriptor) { return @() }
    $raw = New-Object System.Security.AccessControl.RawSecurityDescriptor($Descriptor, 0)
    if ($null -eq $raw.DiscretionaryAcl) { return @() }
    return @($raw.DiscretionaryAcl | Where-Object { $_ -is [System.Security.AccessControl.CommonAce] })
}

function Test-Allowed([byte[]]$Descriptor, [System.Security.Principal.SecurityIdentifier]$Sid, [int]$Mask) {
    foreach ($entry in Get-Entries $Descriptor) {
        if ($entry.AceQualifier -eq [System.Security.AccessControl.AceQualifier]::AccessAllowed -and $entry.SecurityIdentifier -eq $Sid -and
            ($entry.AccessMask -band $Mask) -eq $Mask) {
            return $true
        }
    }
    return $false
}

function Write-Entries([string]$Name, [string]$Guid) {
    $descriptor = Get-StoredDescriptor $Guid
    Write-Output "$Name ($Guid):"
    if ($null -eq $descriptor) {
        Write-Output '  no stored descriptor'
        return
    }
    foreach ($entry in Get-Entries $descriptor) {
        $kind = if ($entry.AceQualifier -eq [System.Security.AccessControl.AceQualifier]::AccessAllowed) { 'allow' } else { $entry.AceQualifier.ToString().ToLowerInvariant() }
        Write-Output ('  {0,-6} 0x{1:X4}  {2}' -f $kind, $entry.AccessMask, (Get-AccountName $entry.SecurityIdentifier))
    }
}

function Open-Record([System.Security.Principal.SecurityIdentifier]$Sid, [bool]$Writable) {
    return [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey("$RecordPath\$($Sid.Value)", $Writable)
}

function Remove-Record([System.Security.Principal.SecurityIdentifier]$Sid) {
    $machine = [Microsoft.Win32.Registry]::LocalMachine
    $machine.DeleteSubKeyTree("$RecordPath\$($Sid.Value)", $false)
    # Remove the parent keys that the setup created once they hold no other record.
    foreach ($path in @($RecordPath, $RecordPath.Substring(0, $RecordPath.LastIndexOf('\')))) {
        $key = $machine.OpenSubKey($path, $false)
        if ($null -eq $key) { continue }
        $empty = $key.SubKeyCount -eq 0 -and $key.ValueCount -eq 0
        $key.Dispose()
        if ($empty) { $machine.DeleteSubKey($path, $false) }
    }
}

$sid = Resolve-AccountSid
$name = Get-AccountName $sid
$recordLocation = "HKLM\$RecordPath\$($sid.Value)"

switch ($Action) {
    'inspect' {
        foreach ($guid in $Providers.Keys) { Write-Entries $Providers[$guid].Name $guid }
        Write-Entries 'Default descriptor' $DefaultDescriptor
        $member = if (Test-Member $sid) { 'is' } else { 'is not' }
        Write-Output "$name ($($sid.Value)) $member a member of Performance Log Users."
        $record = Open-Record $sid $false
        if ($null -eq $record) {
            Write-Output "No setup record for $name exists at $recordLocation."
        } else {
            Write-Output "A setup record for $name exists at $recordLocation, written $($record.GetValue('Written'))."
            $record.Dispose()
        }
    }
    'setup' {
        $record = Open-Record $sid $false
        $written = $null
        if ($null -ne $record) {
            $written = $record.GetValue('Written')
            $record.Dispose()
        }
        if ($null -ne $written) {
            Write-Output "Keeping the setup record of $written at $recordLocation."
        } else {
            # The prior state is recorded before the first change, and Written last: a record without it comes from a
            # setup that stopped before any change, and is recorded anew.
            [Microsoft.Win32.Registry]::LocalMachine.DeleteSubKeyTree("$RecordPath\$($sid.Value)", $false)
            $record = [Microsoft.Win32.Registry]::LocalMachine.CreateSubKey("$RecordPath\$($sid.Value)")
            try {
                $absent = @()
                foreach ($guid in $Providers.Keys) {
                    $descriptor = Get-StoredDescriptor $guid
                    if ($null -eq $descriptor) { $absent += $guid }
                    else { $record.SetValue($guid, [byte[]]$descriptor, [Microsoft.Win32.RegistryValueKind]::Binary) }
                }
                $record.SetValue('Absent', [string[]]$absent, [Microsoft.Win32.RegistryValueKind]::MultiString)
                $record.SetValue('WasMember', [int](Test-Member $sid), [Microsoft.Win32.RegistryValueKind]::DWord)
                $record.SetValue('Account', $name, [Microsoft.Win32.RegistryValueKind]::String)
                $record.SetValue('Written', [DateTime]::Now.ToString('o'), [Microsoft.Win32.RegistryValueKind]::String)
            } finally {
                $record.Dispose()
            }
            Write-Output "Recorded the prior state at $recordLocation."
        }

        if (Test-Member $sid) {
            Write-Output "$name is already a member of Performance Log Users."
        } else {
            Set-Membership $sid $true
            Write-Output "Added $name to Performance Log Users."
        }

        $bytes = New-Object byte[] $sid.BinaryLength
        $sid.GetBinaryForm($bytes, 0)
        foreach ($guid in $Providers.Keys) {
            $provider = $Providers[$guid]
            if (Test-Allowed (Get-StoredDescriptor $guid) $sid $provider.Mask) {
                Write-Output ('{0} already allows {1} 0x{2:X4}.' -f $provider.Name, $name, $provider.Mask)
                continue
            }
            $id = [Guid]$guid
            $status = [SharedWorkflows.EtwAccessNative]::EventAccessControl([ref]$id, $EventSecurityAddDACL, $bytes, [uint32]$provider.Mask, $true)
            if ($status -ne 0) { throw "EventAccessControl failed with $status for $($provider.Name)" }
            Write-Output ('{0} now allows {1} 0x{2:X4}.' -f $provider.Name, $name, $provider.Mask)
        }
        Write-Output "The membership takes effect at the next log-on of $name; the allow entries take effect now."
    }
    'restore' {
        $record = Open-Record $sid $false
        if ($null -eq $record) {
            Write-Output "No setup record for $name exists at $recordLocation; nothing was changed."
            exit 0
        }
        $recordedAbsent = @($record.GetValue('Absent', @()))
        $unrecorded = @($Providers.Keys | Where-Object { $recordedAbsent -notcontains $_ -and $null -eq $record.GetValue($_) })
        if ($null -eq $record.GetValue('Written') -or $unrecorded.Count -gt 0) {
            $record.Dispose()
            [Console]::Error.WriteLine("The setup record at $recordLocation is incomplete; nothing was changed. A record without " +
                'its time of writing comes from a setup that stopped before any change, and a repeated setup records the state anew.')
            exit 1
        }
        try {
            $absent = @($record.GetValue('Absent'))
            $wasMember = [int]$record.GetValue('WasMember') -ne 0
            $security = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($SecurityPath, $true)
            try {
                foreach ($guid in $Providers.Keys) {
                    if ($absent -contains $guid) {
                        $security.DeleteValue($guid, $false)
                        Write-Output "Removed the stored descriptor of $($Providers[$guid].Name), which was absent before the setup."
                    } else {
                        $security.SetValue($guid, [byte[]]$record.GetValue($guid), [Microsoft.Win32.RegistryValueKind]::Binary)
                        Write-Output "Wrote back the recorded descriptor of $($Providers[$guid].Name)."
                    }
                }
            } finally {
                $security.Dispose()
            }
        } finally {
            $record.Dispose()
        }
        if ($wasMember) {
            Write-Output "$name was a member of Performance Log Users before the setup and stays one."
        } elseif (Test-Member $sid) {
            Set-Membership $sid $false
            Write-Output "Removed $name from Performance Log Users; this takes effect at the next log-on of $name."
        }
        Remove-Record $sid
        Write-Output "Removed the setup record at $recordLocation."
    }
}
