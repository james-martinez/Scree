#!/usr/bin/env python3
"""
Autonomous Coding Agent Runtime

This script runs inside the agent VM and performs the actual coding task.
It communicates with the LLM via Open WebUI's API (using the same model
selected by the user) and reports progress to the orchestrator.

Features:
- Git repository cloning and management
- File read/write/edit operations
- Shell command execution
- LLM-driven task planning and execution
- Progress logging for orchestrator streaming
"""

import os
import sys
import json
import time
import subprocess
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
import traceback

# Try to import openai client
try:
    from openai import OpenAI
except ImportError:
    print("Installing openai package...")
    subprocess.run([sys.executable, "-m", "pip", "install", "openai"], check=True)
    from openai import OpenAI


# ============================================================================
# Configuration
# ============================================================================

@dataclass
class AgentConfig:
    """Agent configuration loaded from task_config.json."""
    task_id: str
    repository_url: str
    branch: str
    task_description: str
    model: str  # The model selected in Open WebUI
    openwebui_api_url: str  # Open WebUI API URL
    openwebui_api_key: str  # Open WebUI API key (optional)
    workspace_dir: str = "/home/agent/workspace"
    max_iterations: int = 50
    max_file_size: int = 1024 * 1024  # 1MB
    command_timeout: int = 300  # 5 minutes
    
    @classmethod
    def load(cls, config_path: str = "/opt/agent/task_config.json") -> "AgentConfig":
        """Load configuration from JSON file."""
        with open(config_path, "r") as f:
            data = json.load(f)
        return cls(
            task_id=data["task_id"],
            repository_url=data["repository_url"],
            branch=data.get("branch", "main"),
            task_description=data["task_description"],
            model=data.get("model", "gpt-4"),  # Model from Open WebUI
            openwebui_api_url=data.get("openwebui_api_url", "http://localhost:3000"),
            openwebui_api_key=data.get("openwebui_api_key", ""),
            workspace_dir=data.get("workspace_dir", "/home/agent/workspace"),
            max_iterations=data.get("max_iterations", 50),
            max_file_size=data.get("max_file_size", 1024 * 1024),
            command_timeout=data.get("command_timeout", 300)
        )


# ============================================================================
# Logging
# ============================================================================

class ProgressLogger:
    """Logs progress to a file for the orchestrator to stream."""
    
    def __init__(self, log_path: str = "/opt/agent/progress.log"):
        self.log_path = log_path
        self._ensure_dir()
    
    def _ensure_dir(self):
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
    
    def _timestamp(self) -> str:
        return datetime.now().strftime("%H:%M:%S")
    
    def log(self, message: str, level: str = "INFO"):
        """Log a message with timestamp."""
        line = f"[{self._timestamp()}] [{level}] {message}"
        print(line)
        with open(self.log_path, "a") as f:
            f.write(line + "\n")
    
    def info(self, message: str):
        self.log(message, "INFO")
    
    def action(self, action: str, details: str = ""):
        """Log an action being taken."""
        msg = f"🔧 {action}"
        if details:
            msg += f": {details}"
        self.log(msg, "ACTION")
    
    def thinking(self, thought: str):
        """Log agent's thinking."""
        # Truncate long thoughts
        if len(thought) > 200:
            thought = thought[:200] + "..."
        self.log(f"💭 {thought}", "THINK")
    
    def success(self, message: str):
        self.log(f"✅ {message}", "SUCCESS")
    
    def error(self, message: str):
        self.log(f"❌ {message}", "ERROR")
    
    def complete(self, summary: str = ""):
        """Mark task as complete."""
        self.log(f"[TASK_COMPLETE] {summary}", "DONE")
    
    def fail(self, error: str):
        """Mark task as failed."""
        self.log(f"[TASK_FAILED] {error}", "FAIL")


# ============================================================================
# Tools
# ============================================================================

class Tool(ABC):
    """Base class for agent tools."""
    
    name: str
    description: str
    
    @abstractmethod
    def execute(self, **kwargs) -> str:
        """Execute the tool and return result."""
        pass
    
    @abstractmethod
    def get_schema(self) -> dict:
        """Get OpenAI function schema for this tool."""
        pass


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read the contents of a file"
    
    def __init__(self, config: AgentConfig, logger: ProgressLogger):
        self.config = config
        self.logger = logger
    
    def execute(self, path: str) -> str:
        self.logger.action("Reading file", path)
        
        full_path = self._resolve_path(path)
        if not full_path:
            return f"Error: Path '{path}' is outside workspace"
        
        if not os.path.exists(full_path):
            return f"Error: File '{path}' does not exist"
        
        if os.path.getsize(full_path) > self.config.max_file_size:
            return f"Error: File too large (max {self.config.max_file_size} bytes)"
        
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return content
        except Exception as e:
            return f"Error reading file: {e}"
    
    def _resolve_path(self, path: str) -> Optional[str]:
        """Resolve path and ensure it's within workspace."""
        workspace = os.path.realpath(self.config.workspace_dir)
        full_path = os.path.realpath(os.path.join(workspace, path))
        if full_path.startswith(workspace):
            return full_path
        return None
    
    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path to the file within the workspace"
                        }
                    },
                    "required": ["path"]
                }
            }
        }


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write content to a file (creates directories if needed)"
    
    def __init__(self, config: AgentConfig, logger: ProgressLogger):
        self.config = config
        self.logger = logger
    
    def execute(self, path: str, content: str) -> str:
        self.logger.action("Writing file", path)
        
        workspace = os.path.realpath(self.config.workspace_dir)
        full_path = os.path.realpath(os.path.join(workspace, path))
        
        if not full_path.startswith(workspace):
            return f"Error: Path '{path}' is outside workspace"
        
        try:
            # Create parent directories
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            
            return f"Successfully wrote {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error writing file: {e}"
    
    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path to the file within the workspace"
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to write to the file"
                        }
                    },
                    "required": ["path", "content"]
                }
            }
        }


class ListFilesTool(Tool):
    name = "list_files"
    description = "List files and directories in a path"
    
    def __init__(self, config: AgentConfig, logger: ProgressLogger):
        self.config = config
        self.logger = logger
    
    def execute(self, path: str = ".", recursive: bool = False) -> str:
        self.logger.action("Listing files", path)
        
        workspace = os.path.realpath(self.config.workspace_dir)
        full_path = os.path.realpath(os.path.join(workspace, path))
        
        if not full_path.startswith(workspace):
            return f"Error: Path '{path}' is outside workspace"
        
        if not os.path.exists(full_path):
            return f"Error: Path '{path}' does not exist"
        
        try:
            if recursive:
                files = []
                for root, dirs, filenames in os.walk(full_path):
                    # Skip hidden directories
                    dirs[:] = [d for d in dirs if not d.startswith('.')]
                    
                    for filename in filenames:
                        if not filename.startswith('.'):
                            rel_path = os.path.relpath(
                                os.path.join(root, filename),
                                workspace
                            )
                            files.append(rel_path)
                return "\n".join(sorted(files))
            else:
                entries = []
                for entry in os.listdir(full_path):
                    if entry.startswith('.'):
                        continue
                    entry_path = os.path.join(full_path, entry)
                    if os.path.isdir(entry_path):
                        entries.append(f"{entry}/")
                    else:
                        entries.append(entry)
                return "\n".join(sorted(entries))
        except Exception as e:
            return f"Error listing files: {e}"
    
    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path to list (default: current directory)",
                            "default": "."
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "List files recursively",
                            "default": False
                        }
                    },
                    "required": []
                }
            }
        }


class SearchFilesTool(Tool):
    name = "search_files"
    description = "Search for a pattern in files using grep"
    
    def __init__(self, config: AgentConfig, logger: ProgressLogger):
        self.config = config
        self.logger = logger
    
    def execute(self, pattern: str, path: str = ".", file_pattern: str = "*") -> str:
        self.logger.action("Searching files", f"'{pattern}' in {path}")
        
        workspace = os.path.realpath(self.config.workspace_dir)
        full_path = os.path.realpath(os.path.join(workspace, path))
        
        if not full_path.startswith(workspace):
            return f"Error: Path '{path}' is outside workspace"
        
        try:
            # Use grep for searching
            cmd = [
                "grep", "-r", "-n", "-I",
                "--include", file_pattern,
                pattern, full_path
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                # Make paths relative
                output = result.stdout
                output = output.replace(workspace + "/", "")
                return output or "No matches found"
            elif result.returncode == 1:
                return "No matches found"
            else:
                return f"Search error: {result.stderr}"
        except subprocess.TimeoutExpired:
            return "Error: Search timed out"
        except Exception as e:
            return f"Error searching: {e}"
    
    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Search pattern (regex)"
                        },
                        "path": {
                            "type": "string",
                            "description": "Directory to search in",
                            "default": "."
                        },
                        "file_pattern": {
                            "type": "string",
                            "description": "File glob pattern (e.g., '*.py')",
                            "default": "*"
                        }
                    },
                    "required": ["pattern"]
                }
            }
        }


class ExecuteCommandTool(Tool):
    name = "execute_command"
    description = "Execute a shell command"
    
    # Allowed commands for security
    ALLOWED_COMMANDS = {
        "npm", "yarn", "pnpm", "npx",
        "pip", "pip3", "python", "python3",
        "node",
        "go", "cargo", "rustc",
        "make", "cmake",
        "ls", "cat", "head", "tail", "grep", "find", "wc",
        "git",
        "curl", "wget",
        "jq", "yq",
        "echo", "printf", "test", "mkdir", "cp", "mv", "rm", "touch",
        "chmod", "pwd", "cd", "which", "env",
    }
    
    BLOCKED_PATTERNS = [
        r"rm\s+-rf\s+/",
        r">\s*/dev/",
        r"mkfs\.",
        r"dd\s+if=",
        r"curl.*\|\s*(ba)?sh",
        r"wget.*\|\s*(ba)?sh",
    ]
    
    def __init__(self, config: AgentConfig, logger: ProgressLogger):
        self.config = config
        self.logger = logger
    
    def execute(self, command: str) -> str:
        self.logger.action("Executing command", command[:100])
        
        # Security check
        is_safe, reason = self._validate_command(command)
        if not is_safe:
            return f"Error: Command blocked - {reason}"
        
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.config.command_timeout,
                cwd=self.config.workspace_dir,
                env={**os.environ, "HOME": "/home/agent"}
            )
            
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                if output:
                    output += "\n--- stderr ---\n"
                output += result.stderr
            
            if result.returncode != 0:
                output += f"\n(Exit code: {result.returncode})"
            
            return output or "(No output)"
            
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {self.config.command_timeout}s"
        except Exception as e:
            return f"Error executing command: {e}"
    
    def _validate_command(self, command: str) -> Tuple[bool, str]:
        """Validate command against security rules."""
        # Check blocked patterns
        for pattern in self.BLOCKED_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return False, f"Matches blocked pattern"
        
        # Extract base command
        try:
            parts = shlex.split(command)
            if not parts:
                return False, "Empty command"
            base_cmd = parts[0].split("/")[-1]
        except ValueError:
            # shlex couldn't parse, try simple split
            base_cmd = command.split()[0].split("/")[-1]
        
        if base_cmd not in self.ALLOWED_COMMANDS:
            return False, f"Command '{base_cmd}' not allowed"
        
        return True, "OK"
    
    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"{self.description}. Allowed commands: {', '.join(sorted(self.ALLOWED_COMMANDS))}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Shell command to execute"
                        }
                    },
                    "required": ["command"]
                }
            }
        }


class GitStatusTool(Tool):
    name = "git_status"
    description = "Check git status of the repository"
    
    def __init__(self, config: AgentConfig, logger: ProgressLogger):
        self.config = config
        self.logger = logger
    
    def execute(self) -> str:
        self.logger.action("Checking git status")
        
        try:
            result = subprocess.run(
                ["git", "status"],
                capture_output=True,
                text=True,
                cwd=self.config.workspace_dir
            )
            return result.stdout + result.stderr
        except Exception as e:
            return f"Error: {e}"
    
    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        }


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Show git diff of changes"
    
    def __init__(self, config: AgentConfig, logger: ProgressLogger):
        self.config = config
        self.logger = logger
    
    def execute(self, staged: bool = False) -> str:
        self.logger.action("Getting git diff", "staged" if staged else "unstaged")
        
        try:
            cmd = ["git", "diff"]
            if staged:
                cmd.append("--staged")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self.config.workspace_dir
            )
            return result.stdout or "(No changes)"
        except Exception as e:
            return f"Error: {e}"
    
    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "staged": {
                            "type": "boolean",
                            "description": "Show only staged changes",
                            "default": False
                        }
                    },
                    "required": []
                }
            }
        }


class GitCommitTool(Tool):
    name = "git_commit"
    description = "Stage all changes and create a commit"
    
    def __init__(self, config: AgentConfig, logger: ProgressLogger):
        self.config = config
        self.logger = logger
    
    def execute(self, message: str) -> str:
        self.logger.action("Creating git commit", message[:50])
        
        try:
            # Stage all changes
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.config.workspace_dir,
                check=True
            )
            
            # Commit
            result = subprocess.run(
                ["git", "commit", "-m", message],
                capture_output=True,
                text=True,
                cwd=self.config.workspace_dir
            )
            
            return result.stdout + result.stderr
        except subprocess.CalledProcessError as e:
            return f"Error: {e.stderr if hasattr(e, 'stderr') else str(e)}"
        except Exception as e:
            return f"Error: {e}"
    
    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "Commit message"
                        }
                    },
                    "required": ["message"]
                }
            }
        }


class GitPushTool(Tool):
    name = "git_push"
    description = "Push commits to the remote repository"
    
    def __init__(self, config: AgentConfig, logger: ProgressLogger):
        self.config = config
        self.logger = logger
    
    def execute(self, branch: Optional[str] = None, force: bool = False) -> str:
        branch = branch or self.config.branch
        self.logger.action("Pushing to remote", branch)
        
        try:
            cmd = ["git", "push", "origin", branch]
            if force:
                cmd.insert(2, "-f")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self.config.workspace_dir
            )
            
            return result.stdout + result.stderr
        except Exception as e:
            return f"Error: {e}"
    
    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "branch": {
                            "type": "string",
                            "description": "Branch to push (default: task branch)"
                        },
                        "force": {
                            "type": "boolean",
                            "description": "Force push",
                            "default": False
                        }
                    },
                    "required": []
                }
            }
        }


class TaskCompleteTool(Tool):
    name = "task_complete"
    description = "Mark the task as complete with a summary"
    
    def __init__(self, config: AgentConfig, logger: ProgressLogger):
        self.config = config
        self.logger = logger
        self.completed = False
    
    def execute(self, summary: str, files_changed: List[str] = None) -> str:
        self.logger.success(f"Task complete: {summary}")
        self.completed = True
        
        # Save result
        result = {
            "success": True,
            "summary": summary,
            "files_changed": files_changed or []
        }
        
        with open("/opt/agent/result.json", "w") as f:
            json.dump(result, f, indent=2)
        
        self.logger.complete(summary)
        return "Task marked as complete"
    
    def get_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Summary of what was accomplished"
                        },
                        "files_changed": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of files that were modified"
                        }
                    },
                    "required": ["summary"]
                }
            }
        }


# ============================================================================
# Agent
# ============================================================================

class CodingAgent:
    """Autonomous coding agent that uses LLM (via Open WebUI) to complete tasks."""
    
    SYSTEM_PROMPT = """You are an autonomous coding agent. Your task is to complete coding objectives by reading, understanding, and modifying codebases.

## Guidelines

1. **Understand First**: Before making changes, explore the codebase structure and understand the existing patterns.

2. **Work Incrementally**: Make small, focused changes. Test frequently.

3. **Follow Conventions**: Match the existing code style and patterns in the repository.

4. **Be Thorough**: Consider edge cases, error handling, and testing.

5. **Document Your Work**: Add comments where appropriate and update documentation if needed.

## Process

1. Analyze the repository structure
2. Understand the existing code and patterns
3. Plan your implementation
4. Make changes incrementally
5. Test your changes
6. Commit with clear messages
7. Mark task complete when done

## Available Tools

You have tools for file operations (read, write, list, search), command execution, and git operations. Use them systematically to accomplish the task.

When you're done, use the task_complete tool to mark the task as finished.
"""
    
    def __init__(self, config: AgentConfig, logger: ProgressLogger):
        self.config = config
        self.logger = logger
        
        # Initialize LLM client using Open WebUI's API
        # Open WebUI provides an OpenAI-compatible API at /api/v1
        api_base = config.openwebui_api_url.rstrip('/')
        if not api_base.endswith('/api'):
            api_base = f"{api_base}/api"
        
        self.client = OpenAI(
            base_url=f"{api_base}/v1",
            api_key=config.openwebui_api_key or "not-required"
        )
        
        self.logger.info(f"Using Open WebUI API at {api_base}")
        self.logger.info(f"Using model: {config.model}")
        
        # Initialize tools
        self.tools = self._init_tools()
        self.tool_map = {tool.name: tool for tool in self.tools}
        
        # Conversation history
        self.messages: List[Dict] = []
        
        # Task completion flag
        self.task_completed = False
    
    def _init_tools(self) -> List[Tool]:
        """Initialize all available tools."""
        return [
            ReadFileTool(self.config, self.logger),
            WriteFileTool(self.config, self.logger),
            ListFilesTool(self.config, self.logger),
            SearchFilesTool(self.config, self.logger),
            ExecuteCommandTool(self.config, self.logger),
            GitStatusTool(self.config, self.logger),
            GitDiffTool(self.config, self.logger),
            GitCommitTool(self.config, self.logger),
            GitPushTool(self.config, self.logger),
            TaskCompleteTool(self.config, self.logger),
        ]
    
    def clone_repository(self):
        """Clone the repository to the workspace."""
        self.logger.info(f"Cloning repository: {self.config.repository_url}")
        
        # Ensure workspace is empty
        if os.path.exists(self.config.workspace_dir):
            subprocess.run(["rm", "-rf", self.config.workspace_dir], check=True)
        os.makedirs(self.config.workspace_dir, exist_ok=True)
        
        # Clone
        result = subprocess.run(
            ["git", "clone", "-b", self.config.branch, "--single-branch",
             self.config.repository_url, self.config.workspace_dir],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to clone repository: {result.stderr}")
        
        # Configure git
        subprocess.run(
            ["git", "config", "user.email", "agent@autonomous-coder.local"],
            cwd=self.config.workspace_dir
        )
        subprocess.run(
            ["git", "config", "user.name", "Autonomous Coding Agent"],
            cwd=self.config.workspace_dir
        )
        
        self.logger.success("Repository cloned successfully")
    
    def run(self):
        """Main agent loop."""
        self.logger.info(f"Starting task: {self.config.task_description}")
        
        # Clone repository
        try:
            self.clone_repository()
        except Exception as e:
            self.logger.fail(f"Failed to clone repository: {e}")
            return
        
        # Initialize conversation
        self.messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"""## Task

{self.config.task_description}

## Repository

URL: {self.config.repository_url}
Branch: {self.config.branch}
Working Directory: {self.config.workspace_dir}

Please begin by exploring the repository structure to understand the codebase, then implement the requested changes.
"""}
        ]
        
        # Run agent loop
        for iteration in range(self.config.max_iterations):
            self.logger.info(f"Iteration {iteration + 1}/{self.config.max_iterations}")
            
            try:
                # Get LLM response
                response = self._get_completion()
                
                # Handle response
                if not response:
                    self.logger.error("Empty response from LLM")
                    continue
                
                # Check for tool calls
                message = response.choices[0].message
                
                if message.tool_calls:
                    # Execute tool calls
                    self.messages.append(message.model_dump())
                    
                    for tool_call in message.tool_calls:
                        result = self._execute_tool(tool_call)
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result
                        })
                        
                        # Check if task was completed
                        if tool_call.function.name == "task_complete":
                            self.task_completed = True
                            return
                else:
                    # Just text response - add to history
                    if message.content:
                        self.logger.thinking(message.content[:200])
                        self.messages.append({
                            "role": "assistant",
                            "content": message.content
                        })
                
                # Check stop conditions
                if self.task_completed:
                    return
                
            except Exception as e:
                self.logger.error(f"Error in iteration {iteration + 1}: {e}")
                traceback.print_exc()
                
                # Add error to conversation for recovery
                self.messages.append({
                    "role": "user",
                    "content": f"An error occurred: {e}. Please try a different approach."
                })
        
        # Max iterations reached
        self.logger.fail("Max iterations reached without completing task")
    
    def _get_completion(self):
        """Get completion from LLM."""
        return self.client.chat.completions.create(
            model=self.config.model,
            messages=self.messages,
            tools=[tool.get_schema() for tool in self.tools],
            tool_choice="auto",
            max_tokens=4096
        )
    
    def _execute_tool(self, tool_call) -> str:
        """Execute a tool call and return the result."""
        name = tool_call.function.name
        
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            return f"Error: Invalid JSON arguments: {tool_call.function.arguments}"
        
        tool = self.tool_map.get(name)
        if not tool:
            return f"Error: Unknown tool '{name}'"
        
        try:
            result = tool.execute(**args)
            # Truncate very long results
            if len(result) > 10000:
                result = result[:10000] + "\n\n... (truncated)"
            return result
        except Exception as e:
            return f"Error executing {name}: {e}"


# ============================================================================
# Main
# ============================================================================

def main():
    """Main entry point."""
    logger = ProgressLogger()
    
    try:
        logger.info("Loading configuration...")
        config = AgentConfig.load()
        
        logger.info(f"Task ID: {config.task_id}")
        logger.info(f"Repository: {config.repository_url}")
        logger.info(f"Branch: {config.branch}")
        logger.info(f"Model: {config.model} (via Open WebUI)")
        logger.info(f"Open WebUI API: {config.openwebui_api_url}")
        
        agent = CodingAgent(config, logger)
        agent.run()
        
        if not agent.task_completed:
            logger.fail("Task did not complete successfully")
        
    except Exception as e:
        logger.fail(str(e))
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()