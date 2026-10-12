# AI Companion — Step 1: Model Gateway

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
ollama pull qwen3:8b
.\.venv\Scripts\python.exe -m companion
```

Ensure Ollama is running. `/chat` and `/coding` switch model roles. `/exit` exits.

Change `config/models.yaml` to use a different local Ollama model, after pulling it. Both roles initially use Qwen3 8B.

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The first version does not yet support tool calling, streaming, retries, GPU management, or UI. It is intentionally limited to a unified model call path.
