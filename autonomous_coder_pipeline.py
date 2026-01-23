"""
Autonomous Coder Pipeline for Open WebUI

This pipeline enables autonomous coding tasks by:
1. Detecting coding task requests from user messages
2. Spinning up Proxmox VMs for isolated code execution
3. Running an agent that uses LLM to complete coding tasks
4. Streaming progress back to the chat

Installation:
1. Copy this file to your Open WebUI pipelines directory
2. Configure environment variables (see below)
3. Enable the pipeline in Open WebUI Settings → Admin → Pipelines

Environment Variables:
- PROXMOX_HOST: Proxmox server address (e.g., "proxmox.local:8006")
- PROXMOX_USER: API user (e.g., "root@pam" or "user@pve!token")
- PROXMOX_PASSWORD: Password or API token secret
- PROXMOX_NODE: Node name (default: "pve")
- AGENT_TEMPLATE_VMID: VM template ID for agents (default: 9000)
- OPENWEBUI_API_URL: Open WebUI API URL (default: "http://localhost:3000")
- OPENWEBUI_API_KEY: API key for Open WebUI (optional, for authentication)
"""

import os
import re
import json
import asyncio
import uuid
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, AsyncGenerator, Union
from dataclasses import dataclass, field, asdict
from enum import Enum
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("autonomous_coder")


class TaskStatus(Enum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class CodingTask:
    """Represents an autonomous coding task."""
    id: str
    status: TaskStatus
    repository_url: str
    branch: str
    task_description: str
    model: str
    vmid: Optional[int] = None
    vm_ip: Optional[str] = None
    vm_name: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[Dict] = None
    progress_log: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data


class ProxmoxManager:
    """Manages Proxmox VM lifecycle for coding agents."""
    
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        node: str = "pve",
        template_vmid: int = 9000,
        verify_ssl: bool = False
    ):
        self.host = host
        self.user = user
        self.password = password
        self.node = node
        self.template_vmid = template_vmid
        self.verify_ssl = verify_ssl
        self._proxmox = None
        self._vm_pool_start = 10000
    
    def _get_client(self):
        """Lazy-load Proxmox API client."""
        if self._proxmox is None:
            try:
                import proxmoxer
                self._proxmox = proxmoxer.ProxmoxAPI(
                    self.host,
                    user=self.user,
                    password=self.password,
                    verify_ssl=self.verify_ssl
                )
                logger.info(f"Connected to Proxmox at {self.host}")
            except ImportError:
                raise ImportError(
                    "proxmoxer package required. Install with: pip install proxmoxer"
                )
            except Exception as e:
                logger.error(f"Failed to connect to Proxmox: {e}")
                raise
        return self._proxmox
    
    def _get_next_vmid(self) -> int:
        """Find next available VMID."""
        proxmox = self._get_client()
        vms = proxmox.cluster.resources.get(type="vm")
        used_ids = {vm["vmid"] for vm in vms}
        vmid = self._vm_pool_start
        while vmid in used_ids:
            vmid += 1
        return vmid
    
    async def create_agent_vm(
        self,
        task_id: str,
        task_config: Dict,
        cores: int = 2,
        memory: int = 4096
    ) -> Dict:
        """Clone template and start an agent VM for a task."""
        proxmox = self._get_client()
        vmid = self._get_next_vmid()
        vm_name = f"agent-{task_id[:8]}"
        
        logger.info(f"Creating agent VM {vm_name} (VMID: {vmid}) from template {self.template_vmid}")
        
        try:
            # Clone the template
            clone_result = proxmox.nodes(self.node).qemu(self.template_vmid).clone.post(
                newid=vmid,
                name=vm_name,
                full=1,
                target=self.node
            )
            logger.info(f"Clone task started: {clone_result}")
            
            # Wait for clone to complete
            await self._wait_for_task(clone_result)
            
            # Configure the VM
            proxmox.nodes(self.node).qemu(vmid).config.put(
                cores=cores,
                memory=memory,
                tags=f"agent,task-{task_id}"
            )
            
            # Start the VM
            proxmox.nodes(self.node).qemu(vmid).status.start.post()
            logger.info(f"Started VM {vmid}")
            
            # Wait for VM to get IP
            ip_address = await self._wait_for_ip(vmid, timeout=120)
            logger.info(f"VM {vmid} got IP: {ip_address}")
            
            return {
                "vmid": vmid,
                "name": vm_name,
                "ip_address": ip_address,
                "task_id": task_id
            }
            
        except Exception as e:
            logger.error(f"Failed to create agent VM: {e}")
            # Cleanup on failure
            try:
                await self.destroy_vm(vmid)
            except Exception:
                pass
            raise
    
    async def _wait_for_task(self, upid: str, timeout: int = 300):
        """Wait for a Proxmox task to complete."""
        proxmox = self._get_client()
        start = time.time()
        
        while time.time() - start < timeout:
            try:
                status = proxmox.nodes(self.node).tasks(upid).status.get()
                if status.get("status") == "stopped":
                    if status.get("exitstatus") == "OK":
                        return
                    else:
                        raise Exception(f"Task failed: {status.get('exitstatus')}")
            except Exception as e:
                if "does not exist" not in str(e):
                    raise
            await asyncio.sleep(2)
        
        raise TimeoutError(f"Task {upid} timed out after {timeout}s")
    
    async def _wait_for_ip(self, vmid: int, timeout: int = 120) -> Optional[str]:
        """Wait for VM to get an IP address via QEMU guest agent."""
        proxmox = self._get_client()
        start = time.time()
        
        while time.time() - start < timeout:
            try:
                agent_info = proxmox.nodes(self.node).qemu(vmid).agent.get("network-get-interfaces")
                for iface in agent_info.get("result", []):
                    if iface.get("name") not in ("lo", "localhost"):
                        for ip_info in iface.get("ip-addresses", []):
                            if ip_info.get("ip-address-type") == "ipv4":
                                ip = ip_info.get("ip-address")
                                if ip and not ip.startswith("127."):
                                    return ip
            except Exception as e:
                # Guest agent might not be ready yet
                logger.debug(f"Waiting for guest agent: {e}")
            
            await asyncio.sleep(5)
        
        logger.warning(f"VM {vmid} did not get IP within {timeout}s")
        return None
    
    async def exec_command(self, vmid: int, command: str) -> Dict:
        """Execute a command in the VM via guest agent."""
        proxmox = self._get_client()
        
        try:
            result = proxmox.nodes(self.node).qemu(vmid).agent.exec.post(
                command=command
            )
            
            # Wait for command to complete
            pid = result.get("pid")
            if pid:
                await asyncio.sleep(1)
                status = proxmox.nodes(self.node).qemu(vmid).agent("exec-status").get(pid=pid)
                return {
                    "exitcode": status.get("exitcode"),
                    "stdout": status.get("out-data", ""),
                    "stderr": status.get("err-data", "")
                }
            return result
        except Exception as e:
            logger.error(f"Failed to exec command in VM {vmid}: {e}")
            raise
    
    async def destroy_vm(self, vmid: int, force: bool = True):
        """Stop and delete a VM."""
        proxmox = self._get_client()
        
        try:
            # Stop the VM
            try:
                proxmox.nodes(self.node).qemu(vmid).status.stop.post()
                await asyncio.sleep(5)
            except Exception as e:
                if force:
                    try:
                        proxmox.nodes(self.node).qemu(vmid).status.stop.post(forceStop=1)
                        await asyncio.sleep(3)
                    except Exception:
                        pass
            
            # Delete the VM
            proxmox.nodes(self.node).qemu(vmid).delete()
            logger.info(f"Destroyed VM {vmid}")
            
        except Exception as e:
            logger.error(f"Failed to destroy VM {vmid}: {e}")
            raise
    
    async def get_vm_status(self, vmid: int) -> Dict:
        """Get current VM status."""
        proxmox = self._get_client()
        return proxmox.nodes(self.node).qemu(vmid).status.current.get()


class Pipe:
    """
    Open WebUI Pipe for Autonomous Coding Tasks.
    
    Detects coding task requests, provisions VMs, and streams progress.
    """
    
    class Valves:
        """Pipeline configuration - editable in Open WebUI UI."""
        def __init__(self):
            self.PROXMOX_HOST = os.getenv("PROXMOX_HOST", "proxmox.local:8006")
            self.PROXMOX_USER = os.getenv("PROXMOX_USER", "root@pam")
            self.PROXMOX_PASSWORD = os.getenv("PROXMOX_PASSWORD", "")
            self.PROXMOX_NODE = os.getenv("PROXMOX_NODE", "pve")
            self.AGENT_TEMPLATE_VMID = int(os.getenv("AGENT_TEMPLATE_VMID", "9000"))
            # Open WebUI API for the agent to use the same model
            self.OPENWEBUI_API_URL = os.getenv("OPENWEBUI_API_URL", "http://localhost:3000")
            self.OPENWEBUI_API_KEY = os.getenv("OPENWEBUI_API_KEY", "")
            self.MAX_TASK_DURATION = int(os.getenv("MAX_TASK_DURATION", "3600"))  # 1 hour
            self.VM_CORES = int(os.getenv("VM_CORES", "2"))
            self.VM_MEMORY = int(os.getenv("VM_MEMORY", "4096"))
    
    def __init__(self):
        self.name = "Autonomous Coder"
        self.valves = self.Valves()
        self.tasks: Dict[str, CodingTask] = {}
        self.proxmox: Optional[ProxmoxManager] = None
        
        # Patterns to detect coding task requests
        self.task_triggers = [
            r"\b(implement|create|build|develop|code|write)\b.*\b(feature|function|class|module|api|endpoint)\b",
            r"\b(add|fix|update|refactor|modify)\b.*\b(code|file|function|bug|issue)\b",
            r"\bhttps?://(?:github|gitlab|bitbucket)\.[a-z]+/[\w\-\.]+/[\w\-\.]+",
            r"^/code\s+",
            r"^/implement\s+",
            r"^/build\s+",
        ]
    
    async def on_startup(self):
        """Initialize pipeline on startup."""
        logger.info(f"[{self.name}] Pipeline starting...")
        logger.info(f"[{self.name}] Proxmox: {self.valves.PROXMOX_HOST}")
        logger.info(f"[{self.name}] Template VMID: {self.valves.AGENT_TEMPLATE_VMID}")
        logger.info(f"[{self.name}] Open WebUI API: {self.valves.OPENWEBUI_API_URL}")
        
        # Initialize Proxmox manager
        if self.valves.PROXMOX_PASSWORD:
            self.proxmox = ProxmoxManager(
                host=self.valves.PROXMOX_HOST,
                user=self.valves.PROXMOX_USER,
                password=self.valves.PROXMOX_PASSWORD,
                node=self.valves.PROXMOX_NODE,
                template_vmid=self.valves.AGENT_TEMPLATE_VMID
            )
    
    async def on_shutdown(self):
        """Cleanup on shutdown."""
        logger.info(f"[{self.name}] Pipeline shutting down...")
        
        # Cleanup any running tasks
        for task_id, task in list(self.tasks.items()):
            if task.vmid and task.status in (TaskStatus.RUNNING, TaskStatus.PROVISIONING):
                try:
                    await self.proxmox.destroy_vm(task.vmid)
                    logger.info(f"Cleaned up VM for task {task_id}")
                except Exception as e:
                    logger.error(f"Failed to cleanup VM for task {task_id}: {e}")
    
    def _is_coding_task(self, message: str) -> bool:
        """Detect if the message is requesting a coding task."""
        message_lower = message.lower()
        
        for pattern in self.task_triggers:
            if re.search(pattern, message_lower, re.IGNORECASE):
                return True
        
        # Check for explicit repository URLs
        if re.search(r'https?://(?:github|gitlab|bitbucket)\.[a-z]+/', message):
            # Must also have some action words
            action_words = ["implement", "add", "create", "fix", "update", "build", "modify", "change"]
            if any(word in message_lower for word in action_words):
                return True
        
        return False
    
    def _extract_repo_info(self, message: str) -> Optional[Dict]:
        """Extract repository URL and branch from the message."""
        # Look for repository URLs
        url_pattern = r'(https?://(?:github\.com|gitlab\.com|bitbucket\.org)/[\w\-\.]+/[\w\-\.]+)(?:\.git)?'
        urls = re.findall(url_pattern, message)
        
        if not urls:
            return None
        
        # Look for branch mentions
        branch_patterns = [
            r'branch[:\s]+["\']?([a-zA-Z0-9_\-/]+)["\']?',
            r'on\s+(?:the\s+)?["\']?([a-zA-Z0-9_\-/]+)["\']?\s+branch',
        ]
        
        branch = "main"
        for pattern in branch_patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                branch = match.group(1)
                break
        
        return {
            "url": urls[0],
            "branch": branch
        }
    
    def _extract_task_description(self, message: str, repo_url: str) -> str:
        """Extract the task description, removing the repo URL."""
        # Remove the URL from the message to get cleaner task description
        description = re.sub(r'https?://\S+', '', message).strip()
        
        # Remove common prefixes
        prefixes = ["/code", "/implement", "/build", "please", "can you", "could you"]
        for prefix in prefixes:
            if description.lower().startswith(prefix):
                description = description[len(prefix):].strip()
        
        return description or message
    
    async def pipe(
        self,
        body: dict,
        __user__: dict = None,
        __event_emitter__=None
    ) -> Union[str, AsyncGenerator[str, None]]:
        """
        Main pipeline entry point.
        
        Args:
            body: Request body containing messages and model info
            __user__: User information
            __event_emitter__: Event emitter for streaming responses
        
        Returns:
            Response string or async generator for streaming
        """
        messages = body.get("messages", [])
        if not messages:
            return "No messages provided."
        
        user_message = messages[-1].get("content", "")
        
        # Check if this is a coding task request
        if not self._is_coding_task(user_message):
            # Not a coding task - return None to let Open WebUI handle normally
            return None
        
        # This is a coding task - handle it
        return self._handle_coding_task(body, user_message, __user__, __event_emitter__)
    
    async def _handle_coding_task(
        self,
        body: dict,
        user_message: str,
        user: Optional[dict],
        event_emitter
    ) -> AsyncGenerator[str, None]:
        """Handle a detected coding task request."""
        
        # Extract repository information
        repo_info = self._extract_repo_info(user_message)
        
        if not repo_info:
            yield self._format_no_repo_message()
            return
        
        # Check if Proxmox is configured
        if not self.proxmox:
            yield """⚠️ **Proxmox not configured**

Please configure the following environment variables:
- `PROXMOX_HOST`: Your Proxmox server address
- `PROXMOX_USER`: API user (e.g., root@pam)
- `PROXMOX_PASSWORD`: API password or token
- `AGENT_TEMPLATE_VMID`: VM template ID for agents
- `OPENWEBUI_API_URL`: Open WebUI API URL (for agent LLM access)
"""
            return
        
        # Create task
        task_id = str(uuid.uuid4())[:8]
        task_description = self._extract_task_description(user_message, repo_info["url"])
        model = body.get("model", self.valves.DEFAULT_MODEL)
        
        task = CodingTask(
            id=task_id,
            status=TaskStatus.PENDING,
            repository_url=repo_info["url"],
            branch=repo_info["branch"],
            task_description=task_description,
            model=model
        )
        self.tasks[task_id] = task
        
        # Start header
        yield f"""# 🚀 Autonomous Coding Task `{task_id}`

**Repository:** `{repo_info['url']}`
**Branch:** `{repo_info['branch']}`
**Model:** `{model}`

**Task:** {task_description}

---

"""
        
        try:
            # Provision VM
            task.status = TaskStatus.PROVISIONING
            yield "## ⏳ Provisioning Agent VM...\n\n"
            
            vm_info = await self.proxmox.create_agent_vm(
                task_id=task_id,
                task_config={
                    "repository": repo_info,
                    "task_description": task_description,
                    "openwebui_api_url": self.valves.OPENWEBUI_API_URL,
                    "openwebui_api_key": self.valves.OPENWEBUI_API_KEY,
                    "model": model  # Use the model selected in Open WebUI
                },
                cores=self.valves.VM_CORES,
                memory=self.valves.VM_MEMORY
            )
            
            task.vmid = vm_info["vmid"]
            task.vm_ip = vm_info["ip_address"]
            task.vm_name = vm_info["name"]
            task.started_at = datetime.utcnow().isoformat()
            
            yield f"""✅ **Agent VM Ready**
- Name: `{vm_info['name']}`
- VMID: `{vm_info['vmid']}`
- IP: `{vm_info['ip_address'] or 'pending...'}`

---

## 📝 Agent Progress

"""
            
            # Inject and start the agent
            task.status = TaskStatus.RUNNING
            await self._inject_and_start_agent(task)
            
            # Stream progress
            async for progress in self._stream_agent_progress(task):
                yield progress
            
            # Get result
            result = await self._get_task_result(task)
            task.result = result
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.utcnow().isoformat()
            
            # Format result
            yield self._format_task_result(task, result)
            
        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            yield "\n\n⚠️ **Task cancelled**\n"
            raise
            
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            logger.error(f"Task {task_id} failed: {e}", exc_info=True)
            yield f"\n\n❌ **Task Failed**\n\nError: {str(e)}\n"
            
        finally:
            # Cleanup VM
            if task.vmid:
                yield "\n---\n\n🧹 Cleaning up agent VM...\n"
                try:
                    await self.proxmox.destroy_vm(task.vmid)
                    yield "✅ VM destroyed successfully.\n"
                except Exception as e:
                    yield f"⚠️ Failed to cleanup VM: {e}\n"
    
    async def _inject_and_start_agent(self, task: CodingTask):
        """Inject task configuration and start the agent in the VM."""
        if not task.vmid:
            raise ValueError("No VMID for task")
        
        # Create task config file
        # The agent will use Open WebUI's API with the same model selected by the user
        config = {
            "task_id": task.id,
            "repository_url": task.repository_url,
            "branch": task.branch,
            "task_description": task.task_description,
            "model": task.model,  # Same model user selected in Open WebUI
            "openwebui_api_url": self.valves.OPENWEBUI_API_URL,
            "openwebui_api_key": self.valves.OPENWEBUI_API_KEY
        }
        config_json = json.dumps(config).replace("'", "'\\''")
        
        # Write config to VM
        await self.proxmox.exec_command(
            task.vmid,
            f"echo '{config_json}' > /opt/agent/task_config.json"
        )
        
        # Start the agent (in background)
        await self.proxmox.exec_command(
            task.vmid,
            "cd /opt/agent && nohup python3 main.py > /opt/agent/output.log 2>&1 &"
        )
        
        logger.info(f"Agent started in VM {task.vmid}")
    
    async def _stream_agent_progress(self, task: CodingTask) -> AsyncGenerator[str, None]:
        """Stream progress updates from the agent VM."""
        if not task.vmid:
            return
        
        last_line_count = 0
        check_interval = 3  # seconds
        max_duration = self.valves.MAX_TASK_DURATION
        start_time = time.time()
        
        while True:
            # Check timeout
            if time.time() - start_time > max_duration:
                yield f"\n⏰ **Task timeout** (exceeded {max_duration}s)\n"
                break
            
            try:
                # Read progress log from VM
                result = await self.proxmox.exec_command(
                    task.vmid,
                    "cat /opt/agent/progress.log 2>/dev/null || echo ''"
                )
                
                log_content = result.get("stdout", "")
                lines = log_content.strip().split("\n") if log_content.strip() else []
                
                # Yield new lines
                if len(lines) > last_line_count:
                    for line in lines[last_line_count:]:
                        if line.strip():
                            # Format progress line
                            formatted = self._format_progress_line(line)
                            yield formatted
                            task.progress_log.append(line)
                    last_line_count = len(lines)
                
                # Check if task is complete
                if "[TASK_COMPLETE]" in log_content:
                    break
                if "[TASK_FAILED]" in log_content:
                    # Extract error
                    error_match = re.search(r'\[TASK_FAILED\]\s*(.*)', log_content)
                    if error_match:
                        task.error = error_match.group(1)
                    break
                
            except Exception as e:
                logger.warning(f"Failed to read progress for task {task.id}: {e}")
            
            await asyncio.sleep(check_interval)
    
    def _format_progress_line(self, line: str) -> str:
        """Format a progress line for display."""
        # Parse timestamp if present
        timestamp_match = re.match(r'\[(\d{2}:\d{2}:\d{2})\]', line)
        if timestamp_match:
            ts = timestamp_match.group(1)
            content = line[len(timestamp_match.group(0)):].strip()
            return f"`{ts}` {content}\n"
        return f"  {line}\n"
    
    async def _get_task_result(self, task: CodingTask) -> Dict:
        """Get the final result from the agent."""
        if not task.vmid:
            return {"success": False, "error": "No VM"}
        
        try:
            result = await self.proxmox.exec_command(
                task.vmid,
                "cat /opt/agent/result.json 2>/dev/null || echo '{}'"
            )
            
            result_json = result.get("stdout", "{}")
            return json.loads(result_json)
        except Exception as e:
            logger.error(f"Failed to get result for task {task.id}: {e}")
            return {"success": False, "error": str(e)}
    
    def _format_task_result(self, task: CodingTask, result: Dict) -> str:
        """Format the final task result for display."""
        output = "\n---\n\n"
        
        if result.get("success"):
            output += "## ✅ Task Completed Successfully!\n\n"
            
            if result.get("pr_url"):
                output += f"🔗 **Pull Request:** {result['pr_url']}\n"
            if result.get("branch_name"):
                output += f"🌿 **Branch:** `{result['branch_name']}`\n"
            
            files_changed = result.get("files_changed", [])
            if files_changed:
                output += f"\n📊 **Files Changed:** {len(files_changed)}\n\n"
                for f in files_changed[:10]:  # Show first 10
                    output += f"  - `{f}`\n"
                if len(files_changed) > 10:
                    output += f"  - ... and {len(files_changed) - 10} more\n"
            
            if result.get("summary"):
                output += f"\n📋 **Summary:**\n{result['summary']}\n"
        else:
            output += "## ❌ Task Failed\n\n"
            error = result.get("error") or task.error or "Unknown error"
            output += f"**Error:** {error}\n"
        
        return output
    
    def _format_no_repo_message(self) -> str:
        """Format the message shown when no repository URL is detected."""
        return """## 🔍 Repository Required

I detected a coding task request, but I need a repository URL to work on.

**Please provide a GitHub/GitLab repository URL:**

```
Implement user authentication in https://github.com/user/repo
```

**Or specify a branch:**

```
Add a REST API to https://github.com/user/repo branch: develop
```

**Supported formats:**
- `https://github.com/user/repo`
- `https://gitlab.com/user/repo`
- `https://bitbucket.org/user/repo`

**Example tasks:**
- "Add JWT authentication to https://github.com/user/api"
- "Fix the login bug in https://github.com/user/webapp branch: bugfix"
- "/code implement dark mode in https://github.com/user/frontend"
"""


# Export for Open WebUI
pipe = Pipe()