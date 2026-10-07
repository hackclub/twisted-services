<#
Tests every bot endpoint.

Usage (PowerShell):
  .\test_endpoints.ps1 \
    -BaseUrl http://127.0.0.1:8000 \
    -AuthBearerToken "..." \
    -ReviewerId U_REVIEWER \
    -UserIds U_USER1,U_USER2

Notes:
  - The server must already be running.
  - Slack Events test is skipped by default; enable with -TestSlackEvents.
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)]
  [string]$ReviewerId,

  [Parameter(Mandatory=$true)]
  [string[]]$UserIds,

  [string]$BaseUrl = $(if ($env:BASE_URL) { $env:BASE_URL } else { "http://127.0.0.1:8000" }),

  [Parameter(Mandatory=$true)]
  [string]$AuthBearerToken,

  [string]$SlackSigningSecret,

  [switch]$TestSlackEvents
)

if ($TestSlackEvents -and [string]::IsNullOrWhiteSpace($SlackSigningSecret)) {
  throw "Missing -SlackSigningSecret (required with -TestSlackEvents)"
}

$authToken = $AuthBearerToken
$signingSecret = $SlackSigningSecret

function Get-SlackSignature([string]$Timestamp, [string]$Body) {
  $base = "v0:$Timestamp`:$Body"
  $keyBytes = [Text.Encoding]::UTF8.GetBytes($signingSecret)
  $msgBytes = [Text.Encoding]::UTF8.GetBytes($base)
  $hmac = [Security.Cryptography.HMACSHA256]::new($keyBytes)
  try {
    $hashBytes = $hmac.ComputeHash($msgBytes)
  } finally {
    $hmac.Dispose()
  }
  $hex = ($hashBytes | ForEach-Object { $_.ToString("x2") }) -join ""
  return "v0=$hex"
}

function Assert-Status([int]$Got, [int]$Want, [string]$Label) {
  if ($Got -ne $Want) {
    throw "FAIL [$Label]: expected HTTP $Want, got $Got"
  }
  Write-Host "OK   [$Label] HTTP $Got"
}

try {
  # 1) GET /healthz
  $health = Invoke-WebRequest -UseBasicParsing -Method GET -Uri "$BaseUrl/healthz" -TimeoutSec 5
  Assert-Status -Got $health.StatusCode -Want 200 -Label "GET /healthz"

  # 2) POST /slack/events (signed url_verification)
  $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
  if ($TestSlackEvents) {
    $challenge = "test-challenge-$ts"
    $bodyObj = @{ type = "url_verification"; challenge = $challenge }
    $body = ($bodyObj | ConvertTo-Json -Compress)
    $sig = Get-SlackSignature -Timestamp $ts -Body $body

    $slackHeaders = @{
      "Content-Type" = "application/json"
      "X-Slack-Request-Timestamp" = $ts
      "X-Slack-Signature" = $sig
    }

    $slackResp = Invoke-WebRequest -UseBasicParsing -Method POST -Uri "$BaseUrl/slack/events" -Headers $slackHeaders -Body $body -TimeoutSec 10
    Assert-Status -Got $slackResp.StatusCode -Want 200 -Label "POST /slack/events url_verification"
    if ($slackResp.Content -notmatch [Regex]::Escape($challenge)) {
      throw "FAIL [POST /slack/events]: response did not contain challenge"
    }
  } else {
    Write-Host "SKIP [POST /slack/events] (default)"
  }

  $apiHeaders = @{
    "Authorization" = "Bearer $authToken"
    "Content-Type" = "application/json"
  }

  # 3) POST /ship per user
  foreach ($uid in $UserIds) {
    $payload = @{ user_id = $uid; project_name = "Test Project for $uid ($ts)"; project_link = "https://example.com/$uid/$ts" } | ConvertTo-Json -Compress
    $resp = Invoke-WebRequest -UseBasicParsing -Method POST -Uri "$BaseUrl/ship" -Headers $apiHeaders -Body $payload -TimeoutSec 20
    Assert-Status -Got $resp.StatusCode -Want 200 -Label "POST /ship ($uid)"
    $json = $resp.Content | ConvertFrom-Json
    if (-not $json.public -or -not $json.dm) { throw "Bad /ship shape" }
  }

  # 4) POST /review-accept and /review-reject per user
  foreach ($uid in $UserIds) {
    $pname = "Reviewable Project for $uid ($ts)"
    $plink = "https://example.com/review/$uid/$ts"

    $accept = @{ user_id=$uid; project_name=$pname; project_link=$plink; reviewer_id=$ReviewerId; feedback="Looks good ($ts)"; currencies="10 Gold" } | ConvertTo-Json -Compress
    $resp = Invoke-WebRequest -UseBasicParsing -Method POST -Uri "$BaseUrl/review-accept" -Headers $apiHeaders -Body $accept -TimeoutSec 30
    Assert-Status -Got $resp.StatusCode -Want 200 -Label "POST /review-accept ($uid)"

    $reject = @{ user_id=$uid; project_name=$pname; project_link=$plink; reviewer_id=$ReviewerId; feedback="Needs changes ($ts)" } | ConvertTo-Json -Compress
    $resp = Invoke-WebRequest -UseBasicParsing -Method POST -Uri "$BaseUrl/review-reject" -Headers $apiHeaders -Body $reject -TimeoutSec 30
    Assert-Status -Got $resp.StatusCode -Want 200 -Label "POST /review-reject ($uid)"
  }

  # 5) Fulfillment endpoints per user
  foreach ($uid in $UserIds) {
    $suffix = if ($uid.Length -ge 4) { $uid.Substring($uid.Length-4) } else { $uid }
    $oid = "$ts-$suffix"

    $base = @{ user_id=$uid; order_id=$oid; item_name="Test Item"; qty="1"; cost="5 potions" }

    $resp = Invoke-WebRequest -UseBasicParsing -Method POST -Uri "$BaseUrl/fulfill_pending" -Headers $apiHeaders -Body ($base | ConvertTo-Json -Compress) -TimeoutSec 30
    Assert-Status -Got $resp.StatusCode -Want 200 -Label "POST /fulfill_pending ($uid)"

    $resp = Invoke-WebRequest -UseBasicParsing -Method POST -Uri "$BaseUrl/fulfill_approved" -Headers $apiHeaders -Body ($base | ConvertTo-Json -Compress) -TimeoutSec 30
    Assert-Status -Got $resp.StatusCode -Want 200 -Label "POST /fulfill_approved ($uid)"

    $full = $base.Clone()
    $full.fulfilled_by = "Test Fulfillment"
    $full.tracking_details = "TRACK-$oid"
    $resp = Invoke-WebRequest -UseBasicParsing -Method POST -Uri "$BaseUrl/fulfill_fullfilled" -Headers $apiHeaders -Body ($full | ConvertTo-Json -Compress) -TimeoutSec 30
    Assert-Status -Got $resp.StatusCode -Want 200 -Label "POST /fulfill_fullfilled ($uid)"
  }

  # 6) POST /custom per user
  foreach ($uid in $UserIds) {
    $payload = @{ target_id=$uid; message="Hello <@$uid> (test message $ts)" } | ConvertTo-Json -Compress
    $resp = Invoke-WebRequest -UseBasicParsing -Method POST -Uri "$BaseUrl/custom" -Headers $apiHeaders -Body $payload -TimeoutSec 20
    Assert-Status -Got $resp.StatusCode -Want 200 -Label "POST /custom ($uid)"
  }

  Write-Host "All endpoint tests passed."
}
finally {
  # no-op
}
