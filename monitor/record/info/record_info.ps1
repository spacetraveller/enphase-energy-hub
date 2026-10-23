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

function Get-ChildText {
    param(
        [xml]$Document,
        [string]$XPath
    )

    $node = $Document.SelectSingleNode($XPath)
    if ($null -eq $node) {
        return ""
    }
    return $node.InnerText.Trim()
}

$envPath = Resolve-Path -LiteralPath $EnvFile
$envValues = Read-EnvFile -Path $envPath

$hostName = $envValues["ENPHASE_HOST"].TrimEnd("/")
$scheme = $envValues["ENPHASE_INFO_SCHEME"]
if (-not $scheme) {
    $scheme = "http"
}
$path = $envValues["ENPHASE_INFO_PATH"]
if (-not $path) {
    $path = "/info"
}
if (-not $path.StartsWith("/")) {
    $path = "/$path"
}

$url = "${scheme}://${hostName}${path}"
$capturedAt = [DateTimeOffset]::UtcNow
$response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 10
if ($response.Content -is [byte[]]) {
    $content = [System.Text.Encoding]::UTF8.GetString($response.Content)
} else {
    $content = [string]$response.Content
}
[xml]$xml = $content

$packages = @()
$xml.SelectNodes("/envoy_info/package") | ForEach-Object {
    $packages += [ordered]@{
        name = $_.name
        part_number = $_.pn
        version = $_.version
        build = $_.build
    }
}

$snapshot = [ordered]@{
    captured_at = $capturedAt.ToString("o")
    source_url = $url
    status_code = [int]$response.StatusCode
    info = [ordered]@{
        envoy_time = Get-ChildText -Document $xml -XPath "/envoy_info/time"
        device = [ordered]@{
            serial_number = Get-ChildText -Document $xml -XPath "/envoy_info/device/sn"
            part_number = Get-ChildText -Document $xml -XPath "/envoy_info/device/pn"
            software = Get-ChildText -Document $xml -XPath "/envoy_info/device/software"
            euaid = Get-ChildText -Document $xml -XPath "/envoy_info/device/euaid"
            seqnum = Get-ChildText -Document $xml -XPath "/envoy_info/device/seqnum"
            api_version = Get-ChildText -Document $xml -XPath "/envoy_info/device/apiver"
            integrated_meter = Get-ChildText -Document $xml -XPath "/envoy_info/device/imeter"
        }
        web_tokens = Get-ChildText -Document $xml -XPath "/envoy_info/web-tokens"
        packages = $packages
        build_info = [ordered]@{
            build_id = Get-ChildText -Document $xml -XPath "/envoy_info/build_info/build_id"
            build_time_gmt = Get-ChildText -Document $xml -XPath "/envoy_info/build_info/build_time_gmt"
            release_stage = Get-ChildText -Document $xml -XPath "/envoy_info/build_info/release_stage"
            release_version = Get-ChildText -Document $xml -XPath "/envoy_info/build_info/release_ver"
        }
    }
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
