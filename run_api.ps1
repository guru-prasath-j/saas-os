Set-Location $PSScriptRoot
python -m uvicorn amy.app:app --host 127.0.0.1 --port 8848 --reload
