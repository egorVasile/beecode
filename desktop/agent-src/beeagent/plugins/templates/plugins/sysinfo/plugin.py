import os
import platform
import shutil
import subprocess
import sys

from beeagent.tools.base import BaseTool, ToolResult


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            return (out.stdout or "").strip().splitlines()[0]
    except Exception:
        pass
    return "?"


class SysInfoTool(BaseTool):
    name = "sysinfo"
    description = "Show system info: OS, CPU, RAM, disk usage, Python version, git/node versions."
    parameters = {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> ToolResult:
        try:
            total, used, free = shutil.disk_usage(os.getcwd())
            disk = f"{free // (2**30)} GiB free of {total // (2**30)} GiB"
        except Exception:
            disk = "?"
        lines = [
            f"OS:        {platform.system()} {platform.release()} ({platform.machine()})",
            f"Python:    {sys.version.split()[0]} ({sys.executable})",
            f"CPU:       {os.cpu_count()} cores",
            f"Disk:      {disk}",
            f"git:       {_run(['git', '--version'])}",
            f"node:      {_run(['node', '--version'])}",
        ]
        return ToolResult(output="\n".join(lines), error=False)

    def is_safe(self) -> bool:
        return True


TOOLS = [SysInfoTool()]
