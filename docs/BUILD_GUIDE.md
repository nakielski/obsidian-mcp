# Build Guide: From Zero to Sovereign MCP Server

> This guide documents the complete construction of the `obsidian-mcp` server.
> It is written so that anyone can rebuild it step by step.
> Originally created as a learning vehicle and architecture reference for
> [Sovereign AI](#-why-a-custom-mcp-server) backends.

---

## 🎯 Why a Custom MCP Server?

There are already Obsidian MCP servers (e.g. the official community plugin or the Local REST API).
So why build your own?

**The key architectural difference: headless capability.**

Existing solutions are tied to the Obsidian desktop app — they run *inside* the app
or communicate over a local HTTP port that the app exposes. This means:

1. The desktop app must be running.
2. They cannot be packaged into a Docker container.
3. They are not suitable for scalable backend or cloud deployments (e.g. STACKIT K8s).

This server is **filesystem-native**: it reads Markdown files directly, without app dependency.
That makes it ideal infrastructure for:
- **Agent backends**: Long-running LangGraph/CrewAI agents that need knowledge-base access.
- **Sovereign AI**: Deployment in European sovereign clouds (STACKIT).
- **CI/CD pipelines**: Automated documentation checks in GitHub Actions.

---

## 📐 Architecture in 60 Seconds

```
┌──────────────┐      MCP (stdio)      ┌──────────────────┐
│  MCP Client  │ ◄──────────────────► │  obsidian-mcp    │
│ (Claude,     │   tools + resources   │  server.py       │
│  Cursor ...) │                       │       │          │
└──────────────┘                       │       ▼          │
                                       │  vault.py        │
                                       │   (filesystem)   │
                                       │       │          │
                                       │       ▼          │
                                       │  Obsidian Vault  │
                                       │  (*.md + YAML)   │
                                       └──────────────────┘
```

- **`server.py`**: The MCP interface. Defines tools (actions) and resources (data).
- **`vault.py`**: The filesystem backend. Reads/writes Markdown files safely.
- **`examples/mock-vault/`**: A test vault so you can get started immediately.

---

## 🚀 Phase 1: Setup & First Run (15 minutes)

### Step 1.1: Install dependencies

```bash
# Clone the repository
git clone https://github.com/nakielski/obsidian-mcp.git
cd obsidian-mcp

# Virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install the package (editable mode)
pip install -e ".[dev]"
```

**What happens here?**
- `pyproject.toml` declares dependencies (`mcp`, `python-frontmatter`).
- The `-e` flag installs the package in "editable mode" — code changes take effect immediately.

### Step 1.2: Start the server (with mock vault)

```bash
# Starts the server with the test vault (examples/mock-vault/)
obsidian-mcp
```

**Expected output:**
```
[obsidian-mcp] No OBSIDIAN_VAULT_ROOT set, using mock vault.
```
*(The server now waits for stdio input from an MCP client. This is correct behavior.)*

### Step 1.3: Start the server with a real vault

```bash
export OBSIDIAN_VAULT_ROOT=/path/to/your/obsidian/vault
obsidian-mcp
```

---

## 🔌 Phase 2: Claude Desktop / Cursor Integration (10 minutes)

### For Claude Desktop:

1. Open the configuration file:
   - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

2. Add the server:

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "obsidian-mcp",
      "env": {
        "OBSIDIAN_VAULT_ROOT": "/path/to/your/vault"
      }
    }
  }
}
```

3. **Restart Claude Desktop.**
4. Test: Ask Claude *"Search my vault for Qdrant"* or *"Create a note in the Inbox folder"*.

### For Cursor:

Cursor supports MCP from version 0.42 onwards. Configuration in `~/.cursor/mcp.json` (same format as above).

---

## 🏗️ Phase 3: How the Code Works (Code Walkthrough)

### 3.1 The Vault Class (`vault.py`)

The heart of the project. Everything related to files lives here.

```python
class Vault:
    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).resolve()
        # ...
```

**Security:** The `_resolve()` method is critical — it prevents path-traversal attacks.
An LLM might try to read `../../../etc/passwd`. That is blocked:

```python
def _resolve(self, rel_path: str) -> Path:
    candidate = (self.root / rel_path).resolve()
    try:
        candidate.relative_to(self.root)  # Raises if outside vault
    except ValueError:
        raise PermissionError(f"Path escapes vault root")
```

### 3.2 The Tools (`server.py`)

Each tool is a function decorated with `@mcp.tool()`:

```python
@mcp.tool()
def search_notes(query: str) -> str:
    """Search the vault by full-text query."""
    hits = _get_vault().search(query)
    # ...
```

**How MCP interprets this:**
- The function name becomes the tool name (`search_notes`).
- The parameters become the input schema (`query: str`).
- The docstring becomes the description the LLM sees.
- The return value (str) is sent back to the LLM.

---

## 🔧 Phase 4: Adding Your Own Features (Extension Guide)

This is where it gets interesting. Here is how to add new tools.

### Step 4.1: Add a new method in `vault.py`

Add a new method to the `Vault` class. Example: *Count all tags in the vault*.

```python
# In vault.py, inside the Vault class:

def count_tags(self) -> dict[str, int]:
    """Return a frequency map of all tags in the vault."""
    freq = {}
    for note in self.list_notes():
        for tag in note.tags:
            freq[tag] = freq.get(tag, 0) + 1
    return dict(sorted(freq.items(), key=lambda x: x[1], reverse=True))
```

### Step 4.2: Expose the method as an MCP tool in `server.py`

```python
# In server.py:

@mcp.tool()
def count_tags() -> str:
    """Count the frequency of all tags across the vault."""
    freq = _get_vault().count_tags()
    return json.dumps(freq, ensure_ascii=False, indent=2)
```

### Step 4.3: Run tests

```bash
pytest tests/
```

### Step 4.4: Commit & push

```bash
git add src/obsidian_mcp/vault.py src/obsidian_mcp/server.py
git commit -m "feat: add count_tags tool"
git push
```

---

## 🏛️ Phase 5: Production Deployment (Sovereign Cloud)

### Step 5.1: Dockerization

```dockerfile
# Dockerfile (create in repo root)
FROM python:3.11-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e .

# Mount the vault as a volume
VOLUME ["/data/vault"]

ENV OBSIDIAN_VAULT_ROOT=/data/vault

# Runs in stdio mode — pair with an MCP client sidecar in production
CMD ["python", "-m", "obsidian_mcp.server"]
```

### Step 5.2: STACKIT Kubernetes Engine (SKE) Deployment

This is the transition from prototype to Sovereign AI architecture.

```yaml
# k8s-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: obsidian-mcp
  labels:
    app: obsidian-mcp
spec:
  replicas: 1
  selector:
    matchLabels:
      app: obsidian-mcp
  template:
    spec:
      containers:
      - name: obsidian-mcp
        image: <your-registry>/obsidian-mcp:latest
        env:
        - name: OBSIDIAN_VAULT_ROOT
          value: "/data/vault"
        volumeMounts:
        - name: vault-data
          mountPath: /data/vault
      volumes:
      - name: vault-data
        persistentVolumeClaim:
          claimName: vault-pvc
```

---

## 📚 What You Learned (Checklist)

After completing this guide, you can:
- [x] Explain what MCP is and how the client-server architecture works
- [x] Build an MCP server with Python and the official SDK
- [x] Prevent path-traversal security vulnerabilities
- [x] Define custom tools and expose them as MCP tools
- [x] Evaluate the security implications of filesystem access
- [x] Dockerize an MCP server and deploy it on Kubernetes
- [x] Explain the difference between app-bound and headless MCP servers

---

## 🔗 Next Steps in the Portfolio

- Add a RAG pipeline on top of this server — a natural next step.
- Build an autonomous agent that uses this server as its knowledge base.

---

*This project is part of the **Sovereign AI Portfolio** — designed for data locality and sovereignty, open source, headless-ready.*
