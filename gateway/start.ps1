# 加载 .env 并启动随办网关
Get-Content "$PSScriptRoot\.env" | ForEach-Object {
  if ($_ -match '^\s*([^#=\s][^=]*)=(.*)$') {
    [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), 'Process')
  }
}
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn app:app --host 0.0.0.0 --port 8800 --app-dir $PSScriptRoot
