$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Test-HttpJson($Name, $Url) {
    try {
        $data = Invoke-RestMethod -Uri $Url -TimeoutSec 10
        [pscustomobject]@{
            Name = $Name
            Url = $Url
            Healthy = $true
            Detail = "ok"
            Data = $data
        }
    } catch {
        [pscustomobject]@{
            Name = $Name
            Url = $Url
            Healthy = $false
            Detail = $_.Exception.Message
            Data = $null
        }
    }
}

$checks = @()
$checks += Test-HttpJson "ACE-Step" "http://127.0.0.1:8001/health"
$checks += Test-HttpJson "AI Generator" "http://127.0.0.1:18080/health"

$ports = 8001, 18080
$portInfo = foreach ($port in $ports) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    [pscustomobject]@{
        Port = $port
        Listening = [bool]$conn
        ProcessId = if ($conn) { $conn.OwningProcess } else { "" }
    }
}

$gpu = ""
try {
    $gpu = nvidia-smi --query-gpu=name,memory.total,memory.free,driver_version --format=csv,noheader,nounits
} catch {
    $gpu = $_.Exception.Message
}

$disk = Get-PSDrive -Name D,F -ErrorAction SilentlyContinue | Select-Object Name,Free,Used

[pscustomobject]@{
    Time = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Checks = $checks | Select-Object Name,Url,Healthy,Detail
    Ports = $portInfo
    Gpu = $gpu
    Disk = $disk
} | ConvertTo-Json -Depth 8
