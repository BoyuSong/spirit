import os
from pathlib import Path

from companion.collaboration.config import CollaborationConfig
from companion.core.agent import Agent
from companion.tools.approval import ApprovalManager
from companion.tools.files import DownloadArguments, PathArguments, ReadArguments, RestoreArguments, SystemFiles
from companion.tools.coding import CodingFiles, EditArguments, TestArguments, WriteArguments
from companion.tools.registry import Tool, ToolArguments, ToolRegistry
from companion.tools.system import get_system_status
from companion.tools.web import PageArguments, WebReader


def system_root(config: CollaborationConfig) -> Path:
    return Path(os.environ.get(config.system_workspace_env) or ".spirit-workspace").resolve()


def coding_root(config: CollaborationConfig) -> Path:
    return Path(os.environ.get(config.coding_workspace_env) or ".coding-workspace").resolve()


def build_agents(config, gateway, approvals=None):
    root = system_root(config)
    approvals = approvals or ApprovalManager(root / ".agent-internal" / "audit.jsonl")
    approvals.policies.update({role: spec.approval_mode for role, spec in config.agents.items()})
    files = SystemFiles(root, approvals, config.web_domains)
    coding = CodingFiles(coding_root(config), approvals)
    web = WebReader(config.web_domains)
    available = {
        "get_system_status": Tool("get_system_status", "Read current CPU and memory status", ToolArguments, get_system_status),
        "get_web_capabilities": Tool("get_web_capabilities", "Get website reading domains, formats and pagination limits", ToolArguments, web.capabilities),
        "read_webpage": Tool("read_webpage", "Read public HTTPS HTML/text, title, links and a text chunk; use next_offset to continue", PageArguments, web.read, timeout=30),
        "list_directory": Tool("list_directory", "List a directory inside the system workspace", PathArguments, files.list_directory),
        "get_system_workspace": Tool("get_system_workspace", "Get the system workspace path and configured execution policy", ToolArguments, files.workspace_info),
        "make_directory": Tool("make_directory", "Create a directory inside the system workspace", PathArguments, files.make_directory, timeout=360),
        "read_text_file": Tool("read_text_file", "Read UTF-8 text; use next_offset to continue large files", ReadArguments, files.read_text_file),
        "download_file": Tool("download_file", "Download a new file using the configured execution policy", DownloadArguments, files.download_file, timeout=360),
        "delete_file": Tool("delete_file", "Move one file to recoverable app trash using the configured execution policy", PathArguments, files.delete_file, timeout=360),
        "restore_file": Tool("restore_file", "Restore a deleted file by restore_id without overwriting a destination", RestoreArguments, files.restore_file, timeout=360),
        "list_recoverable_files": Tool("list_recoverable_files", "List recoverable filenames and restore IDs without exposing file contents", ToolArguments, files.list_recoverable_files),
        "get_coding_workspace": Tool("get_coding_workspace", "Get the project workspace, execution policy and pytest availability", ToolArguments, coding.workspace_info),
        "list_project_files": Tool("list_project_files", "List project files inside CODING_WORKSPACE", PathArguments, coding.list_directory),
        "read_project_file": Tool("read_project_file", "Read project text and SHA-256; use next_offset for continuation", ReadArguments, coding.read_project_file),
        "write_project_file": Tool("write_project_file", "Create or replace a project file; existing files require expected_sha256 from a read", WriteArguments, coding.write_project_file, timeout=360),
        "apply_text_edit": Tool("apply_text_edit", "Replace exactly one old_text match, guarded by expected_sha256", EditArguments, coding.apply_text_edit, timeout=360),
        "run_python_tests": Tool("run_python_tests", "Run pytest for a workspace path; executes project code with timeout and bounded output", TestArguments, coding.run_python_tests, timeout=370),
    }
    agents = {}
    for role, spec in config.agents.items():
        registry = ToolRegistry()
        for name in spec.tools:
            if name not in available:
                raise ValueError(f"Unknown tool in {role}: {name}")
            registry.register(available[name])
        if any(name != "web_search" for name in spec.builtin_tools):
            raise ValueError("Unsupported built-in tool")
        agents[role] = Agent(gateway, registry, builtin_tools=[{"type": name} for name in spec.builtin_tools],
                             max_tool_rounds=spec.max_tool_rounds, max_tool_calls=spec.max_tool_calls)
    return agents
