<#
  ARS server inventory - READ-ONLY. Changes nothing on the machine.
  Run on each server (app server 192.168.150.249 and DB server HOPC866) from an
  elevated PowerShell (Run as Administrator) so disk-wear counters can be read:

      powershell -ExecutionPolicy Bypass -File .\server_inventory.ps1

  Best run while a listing / allocation job is active, so the 60-second
  performance sample shows real load. Writes <COMPUTERNAME>_inventory.txt
  next to this script; send that file back.
#>
$ErrorActionPreference = 'SilentlyContinue'
$out = Join-Path $PSScriptRoot ("{0}_inventory.txt" -f $env:COMPUTERNAME)
function Section($t) { "`r`n===== $t =====" }
function Gb($b) { [math]::Round($b / 1GB, 1) }

& {
  Section "Machine"
  $cs = Get-CimInstance Win32_ComputerSystem
  $bios = Get-CimInstance Win32_BIOS
  [pscustomobject]@{ Name = $cs.Name; Maker = $cs.Manufacturer; Model = $cs.Model; SystemSKU = $cs.SystemSKUNumber
    BIOS = $bios.SMBIOSBIOSVersion; BIOSDate = $bios.ReleaseDate; Hypervisor = $cs.HypervisorPresent } | Format-List

  Section "Operating system"
  $os = Get-CimInstance Win32_OperatingSystem
  [pscustomobject]@{ OS = $os.Caption; Version = $os.Version; Build = $os.BuildNumber
    Installed = $os.InstallDate; LastBoot = $os.LastBootUpTime } | Format-List

  Section "CPU"
  Get-CimInstance Win32_Processor | Select-Object Name, SocketDesignation, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed | Format-Table -AutoSize

  Section "Memory"
  $arr = Get-CimInstance Win32_PhysicalMemoryArray
  "Installed GB : $(Gb $cs.TotalPhysicalMemory)"
  "Free GB now  : $([math]::Round($os.FreePhysicalMemory / 1MB, 1))"
  "DIMM slots   : $(($arr | Measure-Object MemoryDevices -Sum).Sum)"
  "Max supported: $([math]::Round((($arr | Measure-Object MaxCapacityEx -Sum).Sum) / 1MB)) GB (as reported by firmware)"
  Get-CimInstance Win32_PhysicalMemory | Select-Object DeviceLocator, @{n='GB';e={$_.Capacity/1GB}}, Speed, ConfiguredClockSpeed, Manufacturer, PartNumber, TotalWidth, DataWidth | Format-Table -AutoSize
  "(TotalWidth 72 vs DataWidth 64 = ECC memory)"

  Section "Storage controllers"
  Get-CimInstance Win32_SCSIController | Select-Object Name, DriverName | Format-Table -AutoSize

  Section "Physical disks"
  Get-PhysicalDisk | Select-Object DeviceId, FriendlyName, MediaType, BusType, @{n='GB';e={Gb $_.Size}}, HealthStatus, FirmwareVersion | Format-Table -AutoSize

  Section "Disk wear / reliability (needs Administrator)"
  Get-PhysicalDisk | ForEach-Object {
    $r = $_ | Get-StorageReliabilityCounter
    [pscustomobject]@{ Disk = $_.FriendlyName; WearPct = $r.Wear; TempC = $r.Temperature; PowerOnHours = $r.PowerOnHours
      ReadErrors = $r.ReadErrorsTotal; WriteErrors = $r.WriteErrorsTotal; MaxLatencyMs = $r.FlushLatencyMax }
  } | Format-Table -AutoSize

  Section "Volumes"
  Get-Volume | Where-Object DriveLetter | Select-Object DriveLetter, FileSystemLabel, FileSystem, @{n='SizeGB';e={Gb $_.Size}}, @{n='FreeGB';e={Gb $_.SizeRemaining}}, HealthStatus | Format-Table -AutoSize
  Get-Partition | Where-Object DriveLetter | Select-Object DriveLetter, DiskNumber, @{n='GB';e={Gb $_.Size}} | Format-Table -AutoSize

  Section "Network adapters"
  Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object Name, InterfaceDescription, LinkSpeed | Format-Table -AutoSize

  Section "Virtualisation"
  "Hyper-V service (vmms): $((Get-Service vmms).Status)"
  "Running VMs: $((Get-VM | Measure-Object).Count)"
  Get-VM | Select-Object Name, State, ProcessorCount, @{n='RAMGB';e={Gb $_.MemoryAssigned}} | Format-Table -AutoSize

  Section "SQL Server instances"
  $inst = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL'
  if ($inst) {
    $inst.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' } | ForEach-Object {
      $setup = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\$($_.Value)\Setup"
      [pscustomobject]@{ Instance = $_.Name; Edition = $setup.Edition; Version = $setup.PatchLevel; DataRoot = $setup.SQLDataRoot }
    } | Format-Table -AutoSize
  } else { "No SQL Server instance found" }
  Get-Service | Where-Object { $_.Name -like 'MSSQL*' -or $_.Name -like 'SQLAgent*' } | Select-Object Name, Status, StartType | Format-Table -AutoSize

  Section "Web / app services"
  Get-Service | Where-Object { $_.Name -match '(?i)^(w3svc|nginx|uvicorn|nssm|pm2|cloudflared)' -or $_.DisplayName -match '(?i)\bARS\b|nginx|uvicorn|cloudflare' } | Select-Object Name, DisplayName, Status, StartType | Format-Table -AutoSize

  Section "Listening ports (web, API, SQL)"
  Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 80, 443, 1433, 3000, 5173, 8000, 8080, 8443 |
    Select-Object LocalAddress, LocalPort, @{n='Process';e={(Get-Process -Id $_.OwningProcess).Name}} | Sort-Object LocalPort -Unique | Format-Table -AutoSize

  Section "Top 15 processes by memory"
  Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 15 Name, Id, @{n='RAM_GB';e={Gb $_.WorkingSet64}}, @{n='CPU_s';e={[math]::Round($_.CPU)}} | Format-Table -AutoSize
  "python processes: $((Get-Process python* | Measure-Object).Count), total RAM GB: $(Gb ((Get-Process python* | Measure-Object WorkingSet64 -Sum).Sum))"

  Section "60-second performance sample (12 x 5 s)"
  $ctr = '\Processor(_Total)\% Processor Time', '\Memory\Available MBytes', '\Memory\Pages/sec',
         '\PhysicalDisk(*)\Avg. Disk sec/Read', '\PhysicalDisk(*)\Avg. Disk sec/Write',
         '\PhysicalDisk(*)\Current Disk Queue Length', '\PhysicalDisk(*)\Disk Bytes/sec',
         '\Network Interface(*)\Bytes Total/sec'
  $s = Get-Counter -Counter $ctr -SampleInterval 5 -MaxSamples 12
  $s.CounterSamples | Group-Object Path | ForEach-Object {
    $v = $_.Group.CookedValue
    [pscustomobject]@{ Counter = $_.Name -replace '^\\\\[^\\]+', ''
      Avg = [math]::Round(($v | Measure-Object -Average).Average, 4)
      Max = [math]::Round(($v | Measure-Object -Maximum).Maximum, 4) }
  } | Where-Object { $_.Max -gt 0 } | Format-Table -AutoSize -Wrap
  "(Disk sec/Read and sec/Write are in seconds: 0.005 = 5 ms)"
} *>&1 | Out-String -Width 220 | Set-Content -Path $out -Encoding UTF8

Write-Host "Saved: $out"
