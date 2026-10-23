param(
    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"

function Read-EnvFile {
    param([string]$Path)

    $values = @{}
    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }

        $name, $value = $line.Split("=", 2)
        $values[$name.Trim()] = $value.Trim()
    }
    return $values
}

function Get-Preview {
    param([string]$Content)

    if (-not $Content) {
        return ""
    }

    $normalized = $Content -replace "\s+", " "
    if ($normalized.Length -le 500) {
        return $normalized
    }
    return $normalized.Substring(0, 500)
}

function Get-JsonShape {
    param([string]$Content)

    try {
        $parsed = $Content | ConvertFrom-Json -ErrorAction Stop
        if ($parsed -is [array]) {
            if ($parsed.Count -eq 0) {
                return "array[0]"
            }
            $first = $parsed[0]
            $keys = ($first.PSObject.Properties | Select-Object -ExpandProperty Name) -join ", "
            return "array[$($parsed.Count)] first keys: $keys"
        }

        $keys = ($parsed.PSObject.Properties | Select-Object -ExpandProperty Name) -join ", "
        return "object keys: $keys"
    } catch {
        return ""
    }
}

# Allow self-signed Envoy certificates for this process when Windows PowerShell can negotiate TLS.
Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class TrustAllCertsPolicy : ICertificatePolicy {
    public bool CheckValidationResult(
        ServicePoint srvPoint, X509Certificate certificate,
        WebRequest request, int certificateProblem) {
        return true;
    }
}
"@
[System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustAllCertsPolicy
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$envPath = Resolve-Path -LiteralPath $EnvFile
$envValues = Read-EnvFile -Path $envPath
$hostName = $envValues["ENPHASE_HOST"].TrimEnd("/")
$token = $envValues["ENPHASE_ACCESS_TOKEN"]

if (-not $token) {
    throw "ENPHASE_ACCESS_TOKEN is empty in $envPath"
}

$timeout = 10
$headers = @{
    Accept = "application/json"
    Authorization = "Bearer $token"
}

$endpoints = @(
    "/auth/check_jwt",
    "/home",
    "/home.json",
    "/info",
    "/production",
    "/production.json",
    "/production.json?details=1",
    "/api/v1/production",
    "/api/v1/production/inverters",
    "/inventory.json",
    "/ivp/meters",
    "/ivp/meters/readings",
    "/ivp/pdm/device_data",
    "/ivp/ensemble/inventory",
    "/ivp/ensemble/status",
    "/ivp/ensemble/secctrl",
    "/ivp/ensemble/power",
    "/ivp/ensemble/relay",
    "/ivp/ensemble/dry_contacts",
    "/ivp/ss/dry_contact_settings",
    "/ivp/ss/gen_config",
    "/ivp/ss/gen_mode",
    "/ivp/ss/gen_schedule",
    "/uvp/ss/pel_settings",
    "/admin/lib/tariff"
)

$capturedAt = [DateTimeOffset]::UtcNow
$results = @()

foreach ($endpoint in $endpoints) {
    foreach ($scheme in @("http", "https")) {
        $url = "${scheme}://${hostName}${endpoint}"
        $result = [ordered]@{
            endpoint = $endpoint
            scheme = $scheme
            url = $url
            accessible = $false
            status_code = $null
            content_type = ""
            json_shape = ""
            preview = ""
            error = ""
        }

        try {
            $response = Invoke-WebRequest `
                -Uri $url `
                -Headers $headers `
                -UseBasicParsing `
                -MaximumRedirection 0 `
                -TimeoutSec $timeout `
                -ErrorAction Stop

            $content = if ($response.Content -is [byte[]]) {
                [System.Text.Encoding]::UTF8.GetString($response.Content)
            } else {
                [string]$response.Content
            }

            $result.status_code = [int]$response.StatusCode
            $result.content_type = [string]$response.Headers["Content-Type"]
            $result.accessible = ([int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 300)
            $result.json_shape = Get-JsonShape -Content $content
            $result.preview = Get-Preview -Content $content
        } catch {
            $response = $_.Exception.Response
            if ($null -ne $response) {
                $result.status_code = [int]$response.StatusCode
                $result.content_type = [string]$response.Headers["Content-Type"]
            }
            $result.error = $_.Exception.Message
        }

        $results += $result
        $status = if ($null -eq $result.status_code) { "ERR" } else { $result.status_code }
        Write-Host "$scheme $endpoint -> $status"
    }
}

$snapshot = [ordered]@{
    captured_at = $capturedAt.ToString("o")
    host = $hostName
    auth_header_used = "Bearer <ENPHASE_ACCESS_TOKEN>"
    results = $results
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$outputDir = Join-Path $scriptDir "snapshots"
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

$fileName = "$($capturedAt.ToString("yyyyMMddTHHmmssZ")).json"
$outputPath = Join-Path $outputDir $fileName
$latestPath = Join-Path $outputDir "latest.json"
$json = $snapshot | ConvertTo-Json -Depth 8

Set-Content -LiteralPath $outputPath -Value $json -Encoding UTF8
Set-Content -LiteralPath $latestPath -Value $json -Encoding UTF8

Write-Host "Wrote $outputPath"
Write-Host "Updated $latestPath"
