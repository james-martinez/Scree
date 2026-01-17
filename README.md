# Autonomous Coding Agent for Open WebUI

A self-hosted autonomous coding agent that runs inside Open WebUI. Similar to Google Jules or Cognition Devin, it can:

- Accept natural language coding tasks
- Spin up isolated Proxmox VMs for secure code execution
- Clone repositories and make code changes
- Use the same LLM model selected in Open WebUI for intelligent decision-making
- Stream progress back to the chat in real-time
- Create commits and push changes

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                       Open WebUI                            │
│  ┌─────────────────────────────────────────────────────┐   │
│  │           Autonomous Coder Pipeline                  │   │
│  │  • Detects coding tasks from chat                   │   │
│  │  • Manages Proxmox VM lifecycle                     │   │
│  │  • Streams progress to user                         │   │
│  │  • Uses SAME model selected in chat                 │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
          │                                    ▲
          │ Spawn VM                          │ LLM API calls
          ▼                                    │
┌─────────────────────────────────────────────────────────────┐
│                      Proxmox VE                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │  Agent VM   │  │  Agent VM   │  │  Agent VM   │        │
│  │  Task 001   │  │  Task 002   │  │  Task 003   │        │
│  │             │  │             │  │             │        │
│  │  • Clone    │  │  • Clone    │  │  • Clone    │        │
│  │  • Code     │  │  • Code     │  │  • Code     │   ─────┼──► Uses Open WebUI
│  │  • Test     │  │  • Test     │  │  • Test     │        │   API for LLM
│  │  • Commit   │  │  • Commit   │  │  • Commit   │        │
│  └─────────────┘  └─────────────┘  └─────────────┘        │
└─────────────────────────────────────────────────────────────┘
```

## Prerequisites

1. **Open WebUI** - Running instance with Pipeline support
2. **Proxmox VE** - With API access enabled
3. **Git credentials** - For repository access (if using private repos)

**Note:** The agent uses the same model you select in Open WebUI - fully native integration!

## Installation

### Step 1: Create Proxmox VM Template

1. Create a new VM in Proxmox with Ubuntu 22.04
2. SSH into the VM and run the setup script:

```bash
# Download and run the template setup script
curl -sSL https://raw.githubusercontent.com/your-repo/setup/create_agent_template.sh | sudo bash
```

Or manually copy and run `setup/create_agent_template.sh`.

3. Shutdown the VM:
```bash
sudo shutdown -h now
```

4. In Proxmox UI:
   - Right-click the VM
   - Select **Convert to template**
   - Note the **VMID** (e.g., 9000)

### Step 2: Configure Proxmox API Access

1. Create an API token in Proxmox:
   - Go to **Datacenter → Permissions → API Tokens**
   - Add a new token for your user
   - Note the **Token ID** and **Secret**

2. Or use password authentication (less secure):
   - Use your Proxmox username and password

### Step 3: Install the Pipeline in Open WebUI

1. Go to **Settings → Admin → Pipelines**
2. Click **Add Pipeline**
3. Copy the contents of `autonomous_coder_pipeline.py`
4. Configure the environment variables (see below)
5. Save and enable the pipeline

### Step 4: Configure Environment Variables

Set these environment variables in Open WebUI's pipeline configuration:

| Variable | Description | Example |
|----------|-------------|---------|
| `PROXMOX_HOST` | Proxmox server address | `192.168.1.100:8006` |
| `PROXMOX_USER` | API user | `root@pam` or `user@pve!token` |
| `PROXMOX_PASSWORD` | Password or API token secret | `your-password` |
| `PROXMOX_NODE` | Proxmox node name | `pve` |
| `AGENT_TEMPLATE_VMID` | VM template ID | `9000` |
| `OPENWEBUI_API_URL` | Open WebUI API URL | `http://localhost:3000` |
| `OPENWEBUI_API_KEY` | Open WebUI API key (optional) | `sk-...` |
| `MAX_TASK_DURATION` | Max task duration in seconds | `3600` |
| `VM_CORES` | vCPUs per agent VM | `2` |
| `VM_MEMORY` | Memory per agent VM (MB) | `4096` |

**Important:** The agent uses the same model you select in Open WebUI chat. No separate LLM configuration needed!

### Step 5: Copy Agent Runtime to Template

The agent runtime script (`agent/main.py`) needs to be available in the VM template:

```bash
# SSH into your template VM before converting to template
scp agent/main.py root@template-vm:/opt/agent/main.py
```

Or configure cloud-init to download it from your orchestrator.

## Usage

Once installed, simply chat with Open WebUI and request coding tasks:

### Basic Usage

```
Implement user authentication in https://github.com/user/my-app
```

### With Branch Specification

```
Add a REST API to https://github.com/user/api branch: develop
```

### Explicit Command

```
/code Implement dark mode toggle in https://github.com/user/webapp
```

### Example Interaction

```
You: Add JWT authentication to https://github.com/user/express-api

🚀 Autonomous Coding Task `abc123`

Repository: `https://github.com/user/express-api`
Branch: `main`
Model: `anthropic/claude-sonnet-4-5`

Task: Add JWT authentication

---

## ⏳ Provisioning Agent VM...

✅ Agent VM Ready
- Name: `agent-abc123`
- VMID: `10001`
- IP: `192.168.1.150`

---

## 📝 Agent Progress

`12:00:01` 🔧 Cloning repository
`12:00:05` ✅ Repository cloned successfully
`12:00:06` 🔧 Listing files
`12:00:07` 💭 This is an Express.js application...
`12:00:10` 🔧 Installing dependencies: npm install jsonwebtoken bcrypt
`12:00:30` 🔧 Creating file: src/middleware/auth.js
`12:00:35` 🔧 Creating file: src/controllers/authController.js
`12:00:40` 🔧 Modifying file: src/routes/index.js
...
`12:05:00` 🔧 Creating git commit: Add JWT authentication
`12:05:05` ✅ Task complete: Implemented JWT authentication

---

## ✅ Task Completed Successfully!

📊 Files Changed: 5
  - `src/middleware/auth.js`
  - `src/controllers/authController.js`
  - `src/routes/index.js`
  - `src/routes/auth.js`
  - `package.json`

📋 Summary: Added JWT authentication with login/register endpoints...

---

🧹 Cleaning up agent VM...
✅ VM destroyed successfully.
```

## Supported Repositories

- GitHub: `https://github.com/user/repo`
- GitLab: `https://gitlab.com/user/repo`
- Bitbucket: `https://bitbucket.org/user/repo`

## Security Considerations

### VM Isolation
- Each task runs in a separate VM
- VMs are destroyed after task completion
- Network access is limited to necessary services

### Command Restrictions
The agent can only execute allowed commands:
- Package managers: `npm`, `yarn`, `pip`, `cargo`, `go`
- Language runtimes: `python`, `node`, `go`
- File operations: `ls`, `cat`, `grep`, `find`
- Git operations: `git`

### Blocked Operations
- `rm -rf /`
- Pipe to shell: `curl ... | sh`
- Device access: `> /dev/...`

## Troubleshooting

### Pipeline not detecting coding tasks

Check that your message includes:
- A repository URL (github.com, gitlab.com, bitbucket.org)
- Action words: implement, add, create, fix, update, build

### VM creation fails

1. Verify Proxmox credentials
2. Check template VMID exists
3. Ensure sufficient resources on Proxmox host
4. Check Proxmox API is accessible from Open WebUI

### Agent not starting

1. Verify QEMU guest agent is installed in template
2. Check cloud-init configuration
3. Review `/opt/agent/progress.log` in the VM

### LLM errors

1. Verify Open WebUI is accessible from the agent VM
2. Check OPENWEBUI_API_URL is correct and reachable
3. Ensure the model exists in Open WebUI
4. If using API key auth, verify OPENWEBUI_API_KEY is set

## Development

### Project Structure

```
open_webui_pipeline/
├── autonomous_coder_pipeline.py  # Main Open WebUI pipeline
├── agent/
│   └── main.py                   # Agent runtime (runs in VM)
├── setup/
│   └── create_agent_template.sh  # VM template setup script
├── requirements.txt              # Python dependencies
└── README.md                     # This file
```

### Local Testing

1. Ensure Open WebUI is running and accessible

2. Create a test config file:
```bash
cat > /tmp/task_config.json << 'EOF'
{
  "task_id": "test001",
  "repository_url": "https://github.com/user/repo",
  "branch": "main",
  "task_description": "Add a hello world endpoint",
  "model": "gpt-4",
  "openwebui_api_url": "http://localhost:3000",
  "openwebui_api_key": ""
}
EOF
```

3. Test the agent locally (without VM):
```bash
cd open_webui_pipeline
python agent/main.py
```

## License

MIT License - See LICENSE file for details.

## Contributing

Contributions welcome! Please read CONTRIBUTING.md first.