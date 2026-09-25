# Run

```powershell
winget install --id Python.Python.3.12 -e --source winget
winget install --id astral-sh.uv -e --source winget
Set-Alias uv "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe"
cd C:\projects\hs-ai-service
uv sync
docker compose -f ..\hs-infrastructure\docker-infrastructure.yml up -d postgres-ai
uv run alembic upgrade head
```

# API

```powershell
cd C:\projects\hs-ai-service
uv run uvicorn homespace_ai.main:app --app-dir src --host 0.0.0.0 --port 8084 --reload
```

# Worker

```powershell
cd C:\projects\hs-ai-service
uv run python -m homespace_ai.knowledge.ingestion.worker
```
