#!/usr/bin/env python3
# Copyright (c) 2026 The ARA Records Ansible authors
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""
Standalone ARA Records Ansible MCP Server.
Provides read-only access to ara's API for recorded playbook data and metrics.
Simplicity is a feature.
Find the included README.rst and AGENTS.md for more information.
"""

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ara-mcp")

SERVER_VERSION = "1.0.0"


# URL Building
def get_web_base() -> str:
    """Get the web UI base URL from the API server."""
    return os.getenv("ARA_API_SERVER", "http://127.0.0.1:8000").rstrip("/")


def playbook_url(pk: int) -> str:
    return f"{get_web_base()}/playbooks/{pk}.html"


def task_url(pk: int) -> str:
    return f"{get_web_base()}/tasks/{pk}.html"


def host_url(pk: int) -> str:
    return f"{get_web_base()}/hosts/{pk}.html"


def result_url(pk: int) -> str:
    return f"{get_web_base()}/results/{pk}.html"


def file_url(pk: int, line: int = None) -> str:
    url = f"{get_web_base()}/files/{pk}.html"
    if line:
        url += f"#line-{line}"
    return url


class ARAClient:
    """Async HTTP client for the ARA REST API (read-only)."""

    def __init__(
        self,
        endpoint: str = os.getenv("ARA_API_SERVER", "http://127.0.0.1:8000"),
        timeout: int = 30,
    ):
        self.base_url = endpoint.rstrip("/")
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _get_auth(self) -> Optional[httpx.BasicAuth]:
        username = os.getenv("ARA_API_USERNAME")
        password = os.getenv("ARA_API_PASSWORD")
        if username and password:
            return httpx.BasicAuth(username, password)
        return None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                auth=self._get_auth(),
                headers={
                    "Accept": "application/json",
                    "User-Agent": f"ara-contrib-mcp/{SERVER_VERSION}",
                },
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _request(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        client = await self._get_client()
        url = f"{self.base_url}{path}"
        try:
            resp = await client.get(
                url,
                params={k: v for k, v in (params or {}).items() if v is not None},
            )
        except httpx.ConnectError:
            raise ConnectionError(
                f"Could not connect to ARA server at {self.base_url}. "
                "Is the server running? Check ARA_API_SERVER."
            )
        except httpx.TimeoutException:
            raise TimeoutError(
                f"Request to {url} timed out after {self.timeout}s. "
                "The server may be overloaded or the timeout too low."
            )
        except httpx.RequestError as e:
            raise ConnectionError(f"Network error contacting ARA server: {e}")

        if resp.status_code == 401:
            raise PermissionError(
                "Authentication failed (HTTP 401). "
                "Check ARA_API_USERNAME and ARA_API_PASSWORD."
            )
        if resp.status_code == 403:
            raise PermissionError(
                "Access denied (HTTP 403). "
                "The configured credentials may lack permission for this resource."
            )
        if resp.status_code == 404:
            raise LookupError(f"Not found: {path} (HTTP 404). The resource may not exist.")
        if resp.status_code >= 500:
            raise RuntimeError(
                f"ARA server error (HTTP {resp.status_code}) for {path}. "
                "This is a server-side issue, not a query problem."
            )
        resp.raise_for_status()
        return resp.json()

    async def list(self, resource: str, **params) -> Dict[str, Any]:
        """GET /api/v1/<resource> with optional filters."""
        return await self._request(f"/api/v1/{resource}", params)

    async def get(self, resource: str, pk: int) -> Dict[str, Any]:
        """GET /api/v1/<resource>/<pk>."""
        return await self._request(f"/api/v1/{resource}/{pk}")

    async def batch_get(self, resource: str, pks: List[int]) -> Dict[int, Dict[str, Any]]:
        """Fetch multiple resources concurrently. Returns {pk: data} dict."""
        if not pks:
            return {}

        async def _fetch_one(pk: int) -> tuple[int, Dict[str, Any]]:
            data = await self.get(resource, pk)
            return (pk, data)

        results = await asyncio.gather(
            *[_fetch_one(pk) for pk in pks],
            return_exceptions=True,
        )
        out = {}
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Batch fetch error for {resource}: {result}")
                continue
            pk, data = result
            out[pk] = data
        return out


# URL Parsing
def validate_url_origin(url: str) -> None:
    """
    Validate that a URL's origin matches ARA_API_SERVER.
    Raises ValueError if origins don't match.
    """
    parsed = urlparse(url)
    # Only validate if it looks like a full URL (has scheme and netloc)
    if not parsed.scheme or not parsed.netloc:
        return

    configured = urlparse(get_web_base())
    url_origin = f"{parsed.scheme}://{parsed.netloc}"
    configured_origin = f"{configured.scheme}://{configured.netloc}"

    if url_origin.lower() != configured_origin.lower():
        raise ValueError(
            f"URL origin '{url_origin}' does not match configured "
            f"ARA_API_SERVER '{configured_origin}'. "
            "Please use a URL from the configured server or change ARA_API_SERVER."
        )


def parse_ara_url(url_or_id: str) -> tuple[str, int]:
    """
    Parse an ARA URL or numeric ID into (resource_type, id).

    Handles:
      - "4307" with known resource context
      - "https://demo.recordsansible.org/playbooks/4307.html"
      - "https://demo.recordsansible.org/playbooks/4307.html?status=failed#results"

    Returns (resource_type, id) tuple.
    Raises ValueError if URL cannot be parsed or origin doesn't match ARA_API_SERVER.
    """
    url_or_id = url_or_id.strip()

    # Pure numeric ID - cannot determine resource type
    if url_or_id.isdigit():
        raise ValueError(
            f"Numeric ID '{url_or_id}' provided without resource context. "
            "Please provide a full ARA URL or use the appropriate tool."
        )

    # Validate origin before parsing
    validate_url_origin(url_or_id)

    # Parse as URL
    parsed = urlparse(url_or_id)
    path = parsed.path.rstrip("/")

    # Pattern: /<resource>/<id>.html or /<resource>/<id>
    match = re.match(r"^/(\w+)/(\d+)(?:\.html)?$", path)
    if match:
        resource = match.group(1)
        resource_id = int(match.group(2))
        return (resource, resource_id)

    raise ValueError(f"Could not parse ARA URL: {url_or_id}")


def extract_id(url_or_id) -> int:
    """Extract numeric ID from a URL, numeric string, or integer."""
    # Handle integer input directly
    if isinstance(url_or_id, int):
        return url_or_id

    url_or_id = str(url_or_id).strip()
    if url_or_id.isdigit():
        return int(url_or_id)

    # parse_ara_url validates origin and extracts ID
    _, resource_id = parse_ara_url(url_or_id)
    return resource_id


# Formatting Helpers
def format_duration(duration: Optional[str]) -> str:
    """Format duration for display."""
    return duration if duration else "N/A"


def format_status(status: str) -> str:
    """Format status with text indicator."""
    indicators = {
        "completed": "[OK]",
        "failed": "[FAILED]",
        "running": "[RUNNING]",
        "expired": "[EXPIRED]",
        "ok": "[OK]",
        "skipped": "[SKIPPED]",
        "unreachable": "[UNREACHABLE]",
        "changed": "[CHANGED]",
    }
    return indicators.get(status, f"[{status.upper()}]")


def format_timestamp(ts: Optional[str]) -> str:
    """Format ISO timestamp for display."""
    if not ts:
        return "N/A"
    # Return as-is but truncate microseconds for readability
    return ts.replace("Z", " UTC").split(".")[0] if ts else "N/A"


def extract_code_snippet(content: str, line_number: int, context: int = 5) -> str:
    """Extract code snippet around a specific line number."""
    if not content:
        return "  (no content available)"

    lines = content.split("\n")
    total = len(lines)
    target = line_number - 1  # 0-indexed

    if target < 0 or target >= total:
        return f"  (line {line_number} not found, file has {total} lines)"

    start = max(0, target - context)
    end = min(total, target + context + 1)

    snippet = []
    for i in range(start, end):
        marker = ">>>" if i == target else "   "
        snippet.append(f"  {marker} {i + 1:4d} | {lines[i]}")

    return "\n".join(snippet)


def parse_duration_seconds(duration: Optional[str]) -> float:
    """Parse duration string to seconds."""
    if not duration:
        return 0.0
    try:
        parts = duration.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        pass
    return 0.0


def get_nested(data: Any, key: str, default: Any = None) -> Any:
    """Get value from dict or nested dict by key, handling both dict and scalar."""
    if isinstance(data, dict):
        return data.get(key, default)
    return default


def format_host_facts(facts: Dict[str, Any], indent: str = "  ") -> List[str]:
    """
    Format host facts for text output, mirroring the ARA web dashboard layout.
    Returns a list of lines. Empty list if no facts are available.
    """
    if not facts or not isinstance(facts, dict):
        return [f"{indent}Facts: Not available"]

    lines = []

    # --- System & Environment ---
    system_lines = []

    distro = facts.get("ansible_distribution", "")
    version = facts.get("ansible_distribution_version", "")
    if distro:
        system_lines.append(f"{indent}OS:       {distro} {version}".rstrip())

    kernel = facts.get("ansible_kernel")
    if kernel:
        system_lines.append(f"{indent}Kernel:   {kernel}")

    # Python version: prefer structured ansible_python.version, fall back to ansible_python_version
    python_info = facts.get("ansible_python", {})
    if isinstance(python_info, dict):
        pv = python_info.get("version", {})
        if isinstance(pv, dict) and "major" in pv:
            system_lines.append(f"{indent}Python:   {pv['major']}.{pv['minor']}.{pv['micro']}")
        elif facts.get("ansible_python_version"):
            system_lines.append(f"{indent}Python:   {facts['ansible_python_version']}")
    elif facts.get("ansible_python_version"):
        system_lines.append(f"{indent}Python:   {facts['ansible_python_version']}")

    user_id = facts.get("ansible_user_id")
    if user_id:
        shell = facts.get("ansible_user_shell", "")
        system_lines.append(f"{indent}User:     {user_id}" + (f" ({shell})" if shell else ""))

    virt_type = facts.get("ansible_virtualization_type")
    if virt_type:
        virt_role = facts.get("ansible_virtualization_role", "")
        system_lines.append(f"{indent}Virt:     {virt_type}" + (f" ({virt_role})" if virt_role else ""))

    selinux = facts.get("ansible_selinux", {})
    if isinstance(selinux, dict) and selinux.get("status"):
        mode = selinux.get("mode", "")
        system_lines.append(f"{indent}SELinux:  {selinux['status']}" + (f" ({mode})" if mode else ""))

    pkg_mgr = facts.get("ansible_pkg_mgr")
    if pkg_mgr:
        system_lines.append(f"{indent}Pkg Mgr:  {pkg_mgr}")

    uptime = facts.get("ansible_uptime_seconds")
    if uptime:
        try:
            secs = int(uptime)
            days, rem = divmod(secs, 86400)
            hours, rem = divmod(rem, 3600)
            mins = rem // 60
            parts = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            parts.append(f"{mins}m")
            system_lines.append(f"{indent}Uptime:   {' '.join(parts)}")
        except (ValueError, TypeError):
            pass

    load = facts.get("ansible_loadavg", {})
    if isinstance(load, dict) and load:
        vals = []
        for key in ("1m", "5m", "15m"):
            v = load.get(key)
            if v is not None:
                vals.append(f"{key}={v:.2f}")
        if vals:
            system_lines.append(f"{indent}Load:     {', '.join(vals)}")

    if system_lines:
        lines.append(f"{indent}System & Environment:")
        lines.extend(system_lines)

    # --- Processor & Memory ---
    hw_lines = []

    processor = facts.get("ansible_processor", [])
    if isinstance(processor, list) and len(processor) > 2:
        hw_lines.append(f"{indent}CPU:      {processor[2]}")

    cores = facts.get("ansible_processor_cores")
    nproc = facts.get("ansible_processor_nproc")
    if cores or nproc:
        parts = []
        if cores:
            parts.append(f"{cores} cores")
        if nproc:
            parts.append(f"{nproc} threads")
        hw_lines.append(f"{indent}          {', '.join(parts)}")

    mem = facts.get("ansible_memory_mb", {})
    real = mem.get("real", {}) if isinstance(mem, dict) else {}
    if isinstance(real, dict) and real.get("total"):
        used = real.get("used", 0)
        total = real["total"]
        pct = (used / total * 100) if total else 0
        hw_lines.append(f"{indent}RAM:      {used} / {total} MB ({pct:.0f}% used)")

    swap = mem.get("swap", {}) if isinstance(mem, dict) else {}
    if isinstance(swap, dict) and swap.get("total") and swap["total"] > 0:
        used = swap.get("used", 0)
        total = swap["total"]
        pct = (used / total * 100) if total else 0
        hw_lines.append(f"{indent}Swap:     {used} / {total} MB ({pct:.0f}% used)")

    mounts = facts.get("ansible_mounts", [])
    if isinstance(mounts, list):
        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            size_total = mount.get("size_total", 0)
            if not size_total:
                continue
            size_avail = mount.get("size_available", 0)
            used_pct = ((size_total - size_avail) / size_total * 100) if size_total else 0
            avail_gb = size_avail / (1024 ** 3)
            mount_point = mount.get("mount", "?")
            fstype = mount.get("fstype", "")
            hw_lines.append(f"{indent}Disk:     {mount_point} ({fstype}) {used_pct:.0f}% used, {avail_gb:.1f} GB free")

    if hw_lines:
        lines.append(f"{indent}Processor, Memory & Storage:")
        lines.extend(hw_lines)

    # --- Network ---
    net_lines = []

    hostname = facts.get("ansible_hostname")
    if hostname:
        net_lines.append(f"{indent}Hostname: {hostname}")

    fqdn = facts.get("ansible_fqdn")
    if fqdn and fqdn != hostname:
        net_lines.append(f"{indent}FQDN:     {fqdn}")

    ipv4 = facts.get("ansible_default_ipv4", {})
    if isinstance(ipv4, dict) and ipv4.get("address"):
        addr = ipv4["address"]
        iface = ipv4.get("interface", "")
        gw = ipv4.get("gateway", "")
        parts = [f"{addr}"]
        if iface:
            parts[0] += f" ({iface})"
        if gw:
            parts.append(f"gw {gw}")
        net_lines.append(f"{indent}IPv4:     {', '.join(parts)}")

    ipv6 = facts.get("ansible_default_ipv6", {})
    if isinstance(ipv6, dict) and ipv6.get("address"):
        addr = ipv6["address"]
        prefix = ipv6.get("prefix", "")
        iface = ipv6.get("interface", "")
        val = f"{addr}/{prefix}" if prefix else addr
        if iface:
            val += f" ({iface})"
        net_lines.append(f"{indent}IPv6:     {val}")

    dns = facts.get("ansible_dns", {})
    if isinstance(dns, dict):
        nameservers = dns.get("nameservers", [])
        if nameservers:
            net_lines.append(f"{indent}DNS:      {', '.join(str(ns) for ns in nameservers)}")

    if net_lines:
        lines.append(f"{indent}Network:")
        lines.extend(net_lines)

    return lines if lines else [f"{indent}Facts: Not available"]


# Initialize Client and Server
ara = ARAClient()
server = Server("ara-records-ansible")


# Tool Definitions
@server.list_tools()
async def handle_list_tools() -> List[Tool]:
    """List available MCP tools."""
    return [
        # === Foundation: Playbooks ===
        Tool(
            name="get_playbook",
            description="Get details for a specific playbook by ID or ARA URL",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Playbook ID or URL (e.g., '4307' or 'https://demo.recordsansible.org/playbooks/4307.html')",
                    }
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="list_playbooks",
            description="List playbooks with optional filtering",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["completed", "running", "failed", "expired"],
                    },
                    "name": {"type": "string", "description": "Filter by name (partial match)"},
                    "path": {"type": "string", "description": "Filter by path (partial match)"},
                    "label": {"type": "string", "description": "Filter by label"},
                    "controller": {"type": "string"},
                    "ansible_version": {"type": "string"},
                    "order": {
                        "type": "string",
                        "default": "-started",
                        "description": "Order by field (prefix with - for descending)",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
            },
        ),
        # === Foundation: Tasks ===
        Tool(
            name="get_task",
            description="Get details for a specific task by ID",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string", "description": "Task ID"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="list_tasks",
            description="List tasks with optional filtering",
            inputSchema={
                "type": "object",
                "properties": {
                    "playbook": {"type": "integer", "description": "Filter by playbook ID"},
                    "status": {
                        "type": "string",
                        "enum": ["completed", "running", "failed", "expired"],
                    },
                    "name": {"type": "string"},
                    "action": {"type": "string", "description": "Filter by module (e.g., 'command')"},
                    "order": {"type": "string", "default": "lineno"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        # === Foundation: Hosts ===
        Tool(
            name="get_host",
            description="Get details for a specific host by ID or ARA URL",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Host ID or ARA URL"}
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="list_hosts",
            description="List hosts with optional filtering",
            inputSchema={
                "type": "object",
                "properties": {
                    "playbook": {"type": "integer", "description": "Filter by playbook ID"},
                    "name": {"type": "string", "description": "Filter by hostname"},
                    "order": {"type": "string", "default": "-updated"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        # === Foundation: Results ===
        Tool(
            name="get_result",
            description="Get details for a specific result by ID or ARA URL",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Result ID or ARA URL"}
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="list_results",
            description="List results with optional filtering",
            inputSchema={
                "type": "object",
                "properties": {
                    "playbook": {"type": "integer", "description": "Filter by playbook ID"},
                    "task": {"type": "integer", "description": "Filter by task ID"},
                    "host": {"type": "integer", "description": "Filter by host ID"},
                    "status": {
                        "type": "string",
                        "enum": ["ok", "failed", "skipped", "unreachable"],
                    },
                    "changed": {"type": "boolean"},
                    "ignore_errors": {"type": "boolean"},
                    "order": {"type": "string", "default": "-started"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        # === Foundation: Files ===
        Tool(
            name="get_file",
            description="Get file content by ID, optionally showing snippet around a line",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "File ID"},
                    "line": {"type": "integer", "description": "Show snippet around this line"},
                    "context": {
                        "type": "integer",
                        "default": 5,
                        "description": "Lines of context",
                    },
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="list_files",
            description="List files with optional filtering",
            inputSchema={
                "type": "object",
                "properties": {
                    "playbook": {"type": "integer", "description": "Filter by playbook ID"},
                    "path": {"type": "string", "description": "Filter by path (partial match)"},
                    "order": {"type": "string", "default": "-updated"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        # === Wrapper: Troubleshooting ===
        Tool(
            name="troubleshoot_playbook",
            description="Analyze a failed playbook: find failures, show code context, surface relevant host facts",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Playbook ID or ARA URL"},
                    "include_ignored": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include failures with ignore_errors=true",
                    },
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="troubleshoot_host",
            description="Analyze failures for a specific host across playbook runs",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Host ID or ARA URL"}
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="analyze_performance",
            description="Analyze playbook performance: identify slowest tasks and optimization opportunities",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Playbook ID or ARA URL"},
                    "top_n": {
                        "type": "integer",
                        "default": 10,
                        "description": "Number of slowest tasks to show",
                    },
                },
                "required": ["id"],
            },
        ),
    ]


# Tool Router
@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """Route tool calls to appropriate handlers."""
    handlers = {
        "get_playbook": handle_get_playbook,
        "list_playbooks": handle_list_playbooks,
        "get_task": handle_get_task,
        "list_tasks": handle_list_tasks,
        "get_host": handle_get_host,
        "list_hosts": handle_list_hosts,
        "get_result": handle_get_result,
        "list_results": handle_list_results,
        "get_file": handle_get_file,
        "list_files": handle_list_files,
        "troubleshoot_playbook": handle_troubleshoot_playbook,
        "troubleshoot_host": handle_troubleshoot_host,
        "analyze_performance": handle_analyze_performance,
    }

    handler = handlers.get(name)
    if not handler:
        available = ", ".join(sorted(handlers.keys()))
        return [TextContent(type="text", text=f"Unknown tool: {name}\nAvailable tools: {available}")]

    try:
        return await handler(arguments)
    except LookupError as e:
        return [TextContent(type="text", text=f"Not found: {e}")]
    except ConnectionError as e:
        return [TextContent(type="text", text=f"Connection error: {e}")]
    except TimeoutError as e:
        return [TextContent(type="text", text=f"Timeout: {e}")]
    except PermissionError as e:
        return [TextContent(type="text", text=f"Authentication/authorization error: {e}")]
    except ValueError as e:
        return [TextContent(type="text", text=f"Invalid input: {e}")]
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        return [TextContent(
            type="text",
            text=f"API error (HTTP {status}): The ARA server returned an unexpected error.\n"
                 f"URL: {e.request.url}",
        )]
    except RuntimeError as e:
        return [TextContent(type="text", text=f"Server error: {e}")]
    except Exception as e:
        logger.exception(f"Unexpected error in {name}")
        return [TextContent(
            type="text",
            text=f"Unexpected error in {name}: {type(e).__name__}: {e}\n"
                 "This may be a bug. Please report it.",
        )]


# Foundation Handlers: Playbooks
async def handle_get_playbook(args: Dict[str, Any]) -> List[TextContent]:
    pk = extract_id(args["id"])
    data = await ara.get("playbooks", pk)

    items = data.get("items", {})
    labels = [lb.get("name", "") for lb in data.get("labels", [])]

    lines = [
        f"Playbook {data['id']}: {data.get('name') or 'Unnamed'}",
        "=" * 60,
        playbook_url(pk),
        "",
        f"Status:     {format_status(data.get('status', 'unknown'))}",
        f"Path:       {data.get('path', 'N/A')}",
        f"Controller: {data.get('controller', 'N/A')}",
        f"User:       {data.get('user', 'N/A')}",
        "",
        f"Started:    {format_timestamp(data.get('started'))}",
        f"Ended:      {format_timestamp(data.get('ended'))}",
        f"Duration:   {format_duration(data.get('duration'))}",
        "",
        f"Ansible:    {data.get('ansible_version', 'N/A')}",
        f"Python:     {data.get('python_version', 'N/A')}",
        "",
        "Statistics:",
        f"  Plays:   {items.get('plays', 0)}",
        f"  Tasks:   {items.get('tasks', 0)}",
        f"  Results: {items.get('results', 0)}",
        f"  Hosts:   {items.get('hosts', 0)}",
        f"  Files:   {items.get('files', 0)}",
    ]

    if labels:
        lines.append("")
        lines.append(f"Labels: {', '.join(labels)}")

    return [TextContent(type="text", text="\n".join(lines))]


async def handle_list_playbooks(args: Dict[str, Any]) -> List[TextContent]:
    params = {k: v for k, v in args.items() if v is not None}
    params.setdefault("limit", 25)
    params.setdefault("order", "-started")

    data = await ara.list("playbooks", **params)
    results = data.get("results", [])
    count = data.get("count", 0)

    if not results:
        return [TextContent(type="text", text="No playbooks found.")]

    lines = [f"Playbooks ({len(results)} of {count})", "=" * 60]

    for p in results:
        status = format_status(p.get("status", "unknown"))
        name = p.get("name") or "Unnamed"
        items = p.get("items", {})
        lines.append("")
        lines.append(f"{status} #{p['id']}: {name}")
        lines.append(f"  {playbook_url(p['id'])}")
        lines.append(f"  Path:     {p.get('path', 'N/A')}")
        lines.append(f"  Duration: {format_duration(p.get('duration'))} | Tasks: {items.get('tasks', 0)}")
        lines.append(f"  Started:  {format_timestamp(p.get('started'))}")

    return [TextContent(type="text", text="\n".join(lines))]


# Foundation Handlers: Tasks
async def handle_get_task(args: Dict[str, Any]) -> List[TextContent]:
    pk = extract_id(args["id"])
    data = await ara.get("tasks", pk)

    file_info = data.get("file", {})
    file_id = get_nested(file_info, "id", file_info if isinstance(file_info, int) else None)
    lineno = data.get("lineno")

    snippet = ""
    file_link = ""
    if file_id and lineno:
        try:
            file_data = await ara.get("files", file_id)
            content = file_data.get("content", "")
            file_link = file_url(file_id, lineno)
            if content:
                snippet = f"\nCode (line {lineno}):\n{file_link}\n{extract_code_snippet(content, lineno)}"
        except Exception:
            pass

    playbook_id = get_nested(data.get("playbook"), "id", data.get("playbook"))
    play_id = get_nested(data.get("play"), "id", data.get("play"))

    lines = [
        f"Task {data['id']}: {data.get('name') or 'Unnamed'}",
        "=" * 60,
        task_url(pk),
        "",
        f"Status:   {format_status(data.get('status', 'unknown'))}",
        f"Action:   {data.get('action', 'N/A')}",
        f"Path:     {data.get('path', 'N/A')}",
        f"Line:     {lineno or 'N/A'}",
        f"Handler:  {data.get('handler', False)}",
        "",
        f"Started:  {format_timestamp(data.get('started'))}",
        f"Ended:    {format_timestamp(data.get('ended'))}",
        f"Duration: {format_duration(data.get('duration'))}",
        "",
        f"Playbook: {playbook_id} ({playbook_url(playbook_id) if playbook_id else 'N/A'})",
        f"Play:     {play_id}",
    ]

    if snippet:
        lines.append(snippet)

    return [TextContent(type="text", text="\n".join(lines))]


async def handle_list_tasks(args: Dict[str, Any]) -> List[TextContent]:
    params = {k: v for k, v in args.items() if v is not None}
    params.setdefault("limit", 50)
    params.setdefault("order", "lineno")

    data = await ara.list("tasks", **params)
    results = data.get("results", [])
    count = data.get("count", 0)

    if not results:
        return [TextContent(type="text", text="No tasks found.")]

    lines = [f"Tasks ({len(results)} of {count})", "=" * 60]

    for t in results:
        status = format_status(t.get("status", "unknown"))
        lines.append(f"{status} #{t['id']} L{t.get('lineno', '?')}: {t.get('name') or 'Unnamed'}")
        lines.append(f"    {t.get('action', 'N/A')} | {format_duration(t.get('duration'))}")
        lines.append(f"    {task_url(t['id'])}")

    return [TextContent(type="text", text="\n".join(lines))]


# Foundation Handlers: Hosts
async def handle_get_host(args: Dict[str, Any]) -> List[TextContent]:
    pk = extract_id(args["id"])
    data = await ara.get("hosts", pk)

    playbook_id = get_nested(data.get("playbook"), "id", data.get("playbook"))

    lines = [
        f"Host {data['id']}: {data.get('name', 'Unknown')}",
        "=" * 60,
        host_url(pk),
        "",
        "Results:",
        f"  OK:          {data.get('ok', 0)}",
        f"  Changed:     {data.get('changed', 0)}",
        f"  Failed:      {data.get('failed', 0)}",
        f"  Skipped:     {data.get('skipped', 0)}",
        f"  Unreachable: {data.get('unreachable', 0)}",
        "",
        f"Playbook: {playbook_id} ({playbook_url(playbook_id) if playbook_id else 'N/A'})",
    ]

    # Host facts
    facts = data.get("facts", {})
    lines.append("")
    lines.extend(format_host_facts(facts))

    return [TextContent(type="text", text="\n".join(lines))]


async def handle_list_hosts(args: Dict[str, Any]) -> List[TextContent]:
    params = {k: v for k, v in args.items() if v is not None}
    params.setdefault("limit", 50)
    params.setdefault("order", "-updated")

    data = await ara.list("hosts", **params)
    results = data.get("results", [])
    count = data.get("count", 0)

    if not results:
        return [TextContent(type="text", text="No hosts found.")]

    lines = [f"Hosts ({len(results)} of {count})", "=" * 60]

    for h in results:
        failed = h.get("failed", 0)
        unreachable = h.get("unreachable", 0)
        status = "[FAILED]" if failed > 0 else "[UNREACHABLE]" if unreachable > 0 else "[OK]"

        lines.append(f"{status} #{h['id']}: {h.get('name', 'Unknown')}")
        lines.append(f"    OK: {h.get('ok', 0)} | Changed: {h.get('changed', 0)} | Failed: {failed}")
        lines.append(f"    {host_url(h['id'])}")

    return [TextContent(type="text", text="\n".join(lines))]


# Foundation Handlers: Results
async def handle_get_result(args: Dict[str, Any]) -> List[TextContent]:
    pk = extract_id(args["id"])
    data = await ara.get("results", pk)

    task_info = data.get("task", {})
    host_info = data.get("host", {})
    content = data.get("content", {})

    task_id = get_nested(task_info, "id")
    task_name = get_nested(task_info, "name", "N/A")
    task_action = get_nested(task_info, "action", "N/A")
    task_lineno = get_nested(task_info, "lineno")
    task_file = get_nested(task_info, "file")
    host_id = get_nested(host_info, "id")
    host_name = get_nested(host_info, "name", "N/A")

    lines = [
        f"Result {data['id']}",
        "=" * 60,
        result_url(pk),
        "",
        f"Status:        {format_status(data.get('status', 'unknown'))}",
        f"Changed:       {data.get('changed', False)}",
        f"Ignore Errors: {data.get('ignore_errors', False)}",
        "",
        f"Host:   {host_name} ({host_url(host_id) if host_id else 'N/A'})",
        f"Task:   {task_name} ({task_url(task_id) if task_id else 'N/A'})",
        f"Action: {task_action}",
        "",
        f"Started:  {format_timestamp(data.get('started'))}",
        f"Duration: {format_duration(data.get('duration'))}",
    ]

    # Content: error messages, stdout, stderr
    if content and isinstance(content, dict):
        msg = content.get("msg")
        stderr = content.get("stderr")
        stdout = content.get("stdout")
        rc = content.get("rc")

        if msg:
            lines.append("")
            lines.append(f"Message: {msg}")
        if rc is not None:
            lines.append(f"Return Code: {rc}")
        if stderr:
            lines.append("")
            lines.append("Stderr:")
            lines.append(f"  {stderr[:500]}{'...' if len(stderr) > 500 else ''}")
        if stdout and not stderr:
            lines.append("")
            lines.append("Stdout:")
            lines.append(f"  {stdout[:500]}{'...' if len(stdout) > 500 else ''}")

    # Code snippet
    if task_file and task_lineno:
        file_id = get_nested(task_file, "id", task_file if isinstance(task_file, int) else None)
        if file_id:
            try:
                file_data = await ara.get("files", file_id)
                file_content = file_data.get("content", "")
                if file_content:
                    lines.append("")
                    lines.append(f"Code (line {task_lineno}):")
                    lines.append(file_url(file_id, task_lineno))
                    lines.append(extract_code_snippet(file_content, task_lineno))
            except Exception:
                pass

    return [TextContent(type="text", text="\n".join(lines))]


async def handle_list_results(args: Dict[str, Any]) -> List[TextContent]:
    params = {k: v for k, v in args.items() if v is not None}
    params.setdefault("limit", 50)
    params.setdefault("order", "-started")

    data = await ara.list("results", **params)
    results = data.get("results", [])
    count = data.get("count", 0)

    if not results:
        return [TextContent(type="text", text="No results found.")]

    lines = [f"Results ({len(results)} of {count})", "=" * 60]

    for r in results:
        status = format_status(r.get("status", "unknown"))
        flags = []
        if r.get("changed"):
            flags.append("changed")
        if r.get("ignore_errors"):
            flags.append("ignored")
        flag_str = f" ({', '.join(flags)})" if flags else ""

        lines.append(f"{status} #{r['id']}: Task {r.get('task', '?')} on Host {r.get('host', '?')}{flag_str}")
        lines.append(f"    {result_url(r['id'])}")

    return [TextContent(type="text", text="\n".join(lines))]


# Foundation Handlers: Files
async def handle_get_file(args: Dict[str, Any]) -> List[TextContent]:
    pk = extract_id(args["id"])
    data = await ara.get("files", pk)

    content = data.get("content", "")
    line = args.get("line")
    context = args.get("context", 5)

    lines = [
        f"File {data['id']}",
        "=" * 60,
        file_url(pk, line),
        "",
        f"Path: {data.get('path', 'N/A')}",
        f"SHA1: {data.get('sha1', 'N/A')}",
    ]

    if content:
        total_lines = len(content.split("\n"))
        lines.append(f"Lines: {total_lines}")

        if line:
            lines.append("")
            lines.append(f"Snippet (around line {line}):")
            lines.append(extract_code_snippet(content, line, context))
        else:
            preview = "\n".join(f"  {i+1:4d} | {ln}" for i, ln in enumerate(content.split("\n")[:20]))
            lines.append("")
            lines.append("Preview:")
            lines.append(preview)
            if total_lines > 20:
                lines.append(f"  ... ({total_lines - 20} more lines)")

    return [TextContent(type="text", text="\n".join(lines))]


async def handle_list_files(args: Dict[str, Any]) -> List[TextContent]:
    params = {k: v for k, v in args.items() if v is not None}
    params.setdefault("limit", 50)
    params.setdefault("order", "-updated")

    data = await ara.list("files", **params)
    results = data.get("results", [])
    count = data.get("count", 0)

    if not results:
        return [TextContent(type="text", text="No files found.")]

    lines = [f"Files ({len(results)} of {count})", "=" * 60]

    for f in results:
        lines.append(f"#{f['id']}: {f.get('path', 'N/A')}")
        lines.append(f"    {file_url(f['id'])}")

    return [TextContent(type="text", text="\n".join(lines))]


# Wrapper Handlers: Troubleshooting
async def handle_troubleshoot_playbook(args: Dict[str, Any]) -> List[TextContent]:
    """Analyze failures in a playbook with code context and host facts."""
    pk = extract_id(args["id"])
    include_ignored = args.get("include_ignored", False)

    # Get playbook details
    playbook = await ara.get("playbooks", pk)

    lines = [
        f"Troubleshooting Playbook {pk}",
        "=" * 60,
        playbook_url(pk),
        "",
        f"Name:   {playbook.get('name') or 'Unnamed'}",
        f"Status: {format_status(playbook.get('status', 'unknown'))}",
        f"Path:   {playbook.get('path', 'N/A')}",
        "",
    ]

    # Get failed results
    params = {"playbook": pk, "status": "failed", "limit": 100}
    if not include_ignored:
        params["ignore_errors"] = False

    results_data = await ara.list("results", **params)
    failed_results = results_data.get("results", [])

    if not failed_results:
        lines.append("No failures found.")
        if not include_ignored:
            lines.append("(Use include_ignored=true to see failures with ignore_errors=true)")
        return [TextContent(type="text", text="\n".join(lines))]

    lines.append(f"Found {len(failed_results)} failure(s):")

    # Fetch all full results concurrently
    result_ids = [r["id"] for r in failed_results]
    full_results = await ara.batch_get("results", result_ids)

    # Collect host IDs from fetched results
    host_ids = set()
    for full_result in full_results.values():
        host_info = full_result.get("host", {})
        host_id = get_nested(host_info, "id")
        if host_id:
            host_ids.add(host_id)

    # Pre-fetch all files and hosts concurrently
    file_ids = set()
    for full_result in full_results.values():
        task_info = full_result.get("task", {})
        task_file = get_nested(task_info, "file")
        task_lineno = get_nested(task_info, "lineno")
        if task_file and task_lineno:
            file_id = get_nested(task_file, "id", task_file if isinstance(task_file, int) else None)
            if file_id:
                file_ids.add(file_id)

    file_cache, host_cache = await asyncio.gather(
        ara.batch_get("files", list(file_ids)),
        ara.batch_get("hosts", list(host_ids)),
    )

    # Display each failure
    for i, result in enumerate(failed_results, 1):
        result_id = result["id"]
        full_result = full_results[result_id]

        task_info = full_result.get("task", {})
        host_info = full_result.get("host", {})
        content = full_result.get("content", {})

        task_name = get_nested(task_info, "name", "Unknown")
        task_action = get_nested(task_info, "action", "unknown")
        task_lineno = get_nested(task_info, "lineno")
        task_file = get_nested(task_info, "file")
        host_id = get_nested(host_info, "id")
        host_name = get_nested(host_info, "name", "Unknown")

        lines.append("")
        lines.append(f"--- Failure {i}: Result #{result_id} ---")
        lines.append(result_url(result_id))
        lines.append(f"Task:   {task_name}")
        lines.append(f"Action: {task_action}")
        lines.append(f"Host:   {host_name} ({host_url(host_id) if host_id else 'N/A'})")

        if full_result.get("ignore_errors"):
            lines.append("Note:   ignore_errors=true (intentionally ignored)")

        # Error details
        if content and isinstance(content, dict):
            msg = content.get("msg")
            stderr = content.get("stderr")
            rc = content.get("rc")

            if msg:
                lines.append(f"Error: {msg}")
            if rc is not None:
                lines.append(f"Return code: {rc}")
            if stderr:
                stderr_preview = stderr[:500] + "..." if len(stderr) > 500 else stderr
                lines.append(f"Stderr: {stderr_preview}")

        # Code snippet
        if task_file and task_lineno:
            file_id = get_nested(task_file, "id", task_file if isinstance(task_file, int) else None)
            if file_id and file_id in file_cache:
                file_data = file_cache[file_id]
                file_content = file_data.get("content", "")
                file_path = file_data.get("path", "Unknown")
                if file_content:
                    lines.append("")
                    lines.append(f"Code ({file_path}, line {task_lineno}):")
                    lines.append(file_url(file_id, task_lineno))
                    lines.append(extract_code_snippet(file_content, task_lineno))

    # Host information
    if host_ids:
        lines.append("")
        lines.append("--- Host Information ---")

        for host_id in host_ids:
            if host_id not in host_cache:
                continue
            host_data = host_cache[host_id]
            facts = host_data.get("facts", {})

            lines.append(f"\nHost: {host_data.get('name', 'Unknown')} ({host_url(host_id)})")
            lines.extend(format_host_facts(facts))

    # Summary
    lines.append("")
    lines.append("--- Summary ---")

    task_failures = {}
    for result_id, full_result in full_results.items():
        task_info = full_result.get("task", {})
        task_name = get_nested(task_info, "name", "Unknown")
        task_failures[task_name] = task_failures.get(task_name, 0) + 1

    lines.append(f"Total failures: {len(failed_results)}")
    if len(task_failures) > 1:
        lines.append("By task:")
        for task_name, cnt in sorted(task_failures.items(), key=lambda x: -x[1]):
            lines.append(f"  - {task_name}: {cnt}")

    lines.append("")
    lines.append("If you would like me to analyze a specific failure in more detail")
    lines.append("or suggest potential fixes, please let me know.")

    return [TextContent(type="text", text="\n".join(lines))]


async def handle_troubleshoot_host(args: Dict[str, Any]) -> List[TextContent]:
    """Analyze failures for a specific host."""
    pk = extract_id(args["id"])

    host = await ara.get("hosts", pk)

    lines = [
        f"Troubleshooting Host {pk}: {host.get('name', 'Unknown')}",
        "=" * 60,
        host_url(pk),
        "",
        f"Failed: {host.get('failed', 0)} | Unreachable: {host.get('unreachable', 0)}",
        "",
    ]

    # Key facts
    facts = host.get("facts", {})
    lines.extend(format_host_facts(facts))
    lines.append("")

    # Get failed and unreachable results concurrently
    failed_data, unreachable_data = await asyncio.gather(
        ara.list("results", host=pk, status="failed", limit=50),
        ara.list("results", host=pk, status="unreachable", limit=50),
    )

    all_failures = failed_data.get("results", []) + unreachable_data.get("results", [])

    if not all_failures:
        lines.append("No failures or unreachable results found.")
        return [TextContent(type="text", text="\n".join(lines))]

    lines.append(f"Found {len(all_failures)} failure(s):")

    # Batch fetch full results for all failures (up to 10)
    display_failures = all_failures[:10]
    full_results = await ara.batch_get("results", [r["id"] for r in display_failures])

    for i, result in enumerate(display_failures, 1):
        full_result = full_results.get(result["id"])
        if not full_result:
            lines.append(f"\n{i}. Result #{result['id']}: could not fetch details")
            continue

        task_info = full_result.get("task", {})
        content = full_result.get("content", {})

        task_name = get_nested(task_info, "name", "Unknown")
        task_action = get_nested(task_info, "action", "unknown")
        status = full_result.get("status", "unknown")

        lines.append("")
        lines.append(f"{i}. [{status.upper()}] Result #{result['id']}")
        lines.append(f"   {result_url(result['id'])}")
        lines.append(f"   Task: {task_name}")
        lines.append(f"   Action: {task_action}")

        if content and isinstance(content, dict):
            msg = content.get("msg", "")
            if msg:
                lines.append(f"   Error: {msg[:150]}{'...' if len(msg) > 150 else ''}")

    if len(all_failures) > 10:
        lines.append("")
        lines.append(f"... and {len(all_failures) - 10} more")

    return [TextContent(type="text", text="\n".join(lines))]


async def handle_analyze_performance(args: Dict[str, Any]) -> List[TextContent]:
    """Analyze playbook performance across multiple runs to identify patterns."""
    pk = extract_id(args["id"])
    top_n = args.get("top_n", 10)

    playbook = await ara.get("playbooks", pk)
    playbook_path = playbook.get("path", "")

    lines = [
        f"Performance Analysis: Playbook {pk}",
        "=" * 60,
        playbook_url(pk),
        "",
        f"Name:     {playbook.get('name') or 'Unnamed'}",
        f"Duration: {format_duration(playbook.get('duration'))}",
        f"Tasks:    {playbook.get('items', {}).get('tasks', 0)}",
        "",
    ]

    # Find other runs of the same playbook by path
    related_playbooks = []
    related_count = 0
    if playbook_path:
        related_data = await ara.list("playbooks", path=playbook_path, order="-created", limit=5)
        related_playbooks = related_data.get("results", [])
        related_count = related_data.get("count", 0)
        logger.info(f"Path search '{playbook_path}' found {related_count} total, {len(related_playbooks)} returned")

    lines.append(f"Searched for runs with path: {playbook_path}")
    lines.append(f"Found: {related_count} total runs ({len(related_playbooks)} analyzed)")
    lines.append("")

    # Cross-run analysis - collect task times from ALL related playbooks
    task_times_aggregate = {}  # task_name -> [(playbook_id, duration, controller)]
    all_run_durations = []  # (playbook_id, duration, controller, status)

    if len(related_playbooks) > 1:
        lines.append(f"--- Cross-Run Analysis ({len(related_playbooks)} recent runs) ---")
        lines.append("")

        # Fetch top results for all related playbooks concurrently
        rp_results_map = {}

        async def _fetch_rp_results(rp):
            rp_id = rp["id"]
            rp_results = await ara.list("results", playbook=rp_id, order="-duration", limit=25)
            return (rp_id, rp_results)

        rp_fetches = await asyncio.gather(
            *[_fetch_rp_results(rp) for rp in related_playbooks],
            return_exceptions=True,
        )

        for rp, fetch_result in zip(related_playbooks, rp_fetches):
            rp_id = rp["id"]
            duration_sec = parse_duration_seconds(rp.get("duration"))
            controller = rp.get("controller", "unknown")
            status = rp.get("status", "unknown")
            all_run_durations.append((rp_id, duration_sec, controller, status))

            if isinstance(fetch_result, Exception):
                logger.warning(f"Could not fetch results for playbook {rp_id}: {fetch_result}")
                continue
            _, rp_results = fetch_result
            rp_results_map[rp_id] = rp_results.get("results", [])

        # Batch fetch all full results across all related playbooks
        all_result_ids = []
        result_to_playbook = {}
        for rp_id, results in rp_results_map.items():
            for res in results:
                all_result_ids.append(res["id"])
                result_to_playbook[res["id"]] = rp_id

        full_rp_results = await ara.batch_get("results", all_result_ids)

        for res_id, full_res in full_rp_results.items():
            rp_id = result_to_playbook[res_id]
            controller = next(
                (rp.get("controller", "unknown") for rp in related_playbooks if rp["id"] == rp_id),
                "unknown",
            )
            task_info = full_res.get("task", {})
            task_name = get_nested(task_info, "name", "Unknown")
            task_duration = parse_duration_seconds(full_res.get("duration"))

            if task_name not in task_times_aggregate:
                task_times_aggregate[task_name] = []
            task_times_aggregate[task_name].append((rp_id, task_duration, controller))

        # Show run history
        lines.append("Run history:")
        for run_id, dur, ctrl, status in all_run_durations:
            status_ind = format_status(status)
            lines.append(f"  {status_ind} #{run_id}: {dur:.1f}s on {ctrl}")
            lines.append(f"      {playbook_url(run_id)}")

        # Analyze by controller
        durations_by_controller = {}
        for run_id, dur, ctrl, _ in all_run_durations:
            if ctrl not in durations_by_controller:
                durations_by_controller[ctrl] = []
            durations_by_controller[ctrl].append(dur)

        if len(durations_by_controller) > 1:
            lines.append("")
            lines.append("Duration by controller:")
            for ctrl, durs in sorted(durations_by_controller.items()):
                avg = sum(durs) / len(durs) if durs else 0
                lines.append(f"  {ctrl}: {avg:.1f}s avg ({len(durs)} runs)")

        # Find tasks with high variance across runs
        lines.append("")
        lines.append("Task consistency across runs:")
        inconsistent_tasks = []
        for task_name, measurements in task_times_aggregate.items():
            if len(measurements) >= 2:
                durations = [m[1] for m in measurements]
                avg = sum(durations) / len(durations)
                max_dur = max(durations)
                min_dur = min(durations)
                if avg > 1 and (max_dur - min_dur) / avg > 0.5:  # >50% variance, min 1s avg
                    inconsistent_tasks.append((task_name, min_dur, max_dur, avg, measurements))

        if inconsistent_tasks:
            inconsistent_tasks.sort(key=lambda x: x[3], reverse=True)  # Sort by avg duration
            for task_name, min_d, max_d, avg_d, measurements in inconsistent_tasks[:5]:
                lines.append(f"  {task_name}:")
                lines.append(f"    Range: {min_d:.1f}s - {max_d:.1f}s (avg: {avg_d:.1f}s)")
                slowest = max(measurements, key=lambda x: x[1])
                lines.append(f"    Slowest on: {slowest[2]} (playbook #{slowest[0]})")
        else:
            lines.append("  All tasks show consistent performance across runs.")

        lines.append("")
    elif related_count <= 1:
        lines.append("Note: Only 1 run found for this playbook path.")
        lines.append("Cannot establish patterns across multiple runs.")
        lines.append("")
    else:
        lines.append("Note: Could not search for related runs (no path available).")
        lines.append("")

    # Get results for the current playbook ordered by duration
    results_data = await ara.list("results", playbook=pk, order="-duration", limit=100)
    results = results_data.get("results", [])

    if not results:
        lines.append("No results found.")
        return [TextContent(type="text", text="\n".join(lines))]

    lines.append(f"--- Slowest Tasks (Playbook #{pk}) ---")

    # Batch fetch all full results
    all_full_results = await ara.batch_get("results", [r["id"] for r in results])

    # Track by task+host to avoid duplicates
    seen = set()
    shown = 0
    task_times = {}  # action -> {count, total_time}

    for result in results:
        full_result = all_full_results.get(result["id"])
        if not full_result:
            continue

        task_info = full_result.get("task", {})
        host_info = full_result.get("host", {})

        task_id = get_nested(task_info, "id", result.get("task"))
        task_name = get_nested(task_info, "name", "Unknown")
        task_action = get_nested(task_info, "action", "unknown")
        host_name = get_nested(host_info, "name", "Unknown")
        duration = full_result.get("duration", "N/A")

        # Track time by action
        duration_sec = parse_duration_seconds(duration)
        if task_action not in task_times:
            task_times[task_action] = {"count": 0, "total": 0.0}
        task_times[task_action]["count"] += 1
        task_times[task_action]["total"] += duration_sec

        # Skip if we've already shown enough or seen this task+host combo
        if shown >= top_n:
            continue

        key = (task_id, host_name)
        if key in seen:
            continue
        seen.add(key)

        shown += 1
        lines.append("")
        lines.append(f"{shown}. {duration}")
        lines.append(f"   Task: {task_name}")
        lines.append(f"   Action: {task_action}")
        lines.append(f"   Host: {host_name}")
        lines.append(f"   {result_url(result['id'])}")

    # Time breakdown by module
    lines.append("")
    lines.append("--- Time by Module ---")

    sorted_actions = sorted(task_times.items(), key=lambda x: -x[1]["total"])
    for action, stats in sorted_actions[:8]:
        avg = stats["total"] / stats["count"] if stats["count"] > 0 else 0
        lines.append(f"  {action}: {stats['count']} calls, {stats['total']:.1f}s total, {avg:.2f}s avg")

    # Optimization tips
    lines.append("")
    lines.append("--- Optimization Tips ---")

    # Check forks setting
    arguments = playbook.get("arguments", {})
    forks = arguments.get("forks", 5)

    tips = []

    if forks and forks < 10:
        tips.append(f"- Current forks: {forks}. Consider raising to 25-50 for parallel execution")

    slow_modules = {a for a, s in task_times.items() if s["total"] > 30}
    if "setup" in slow_modules or "gather_facts" in slow_modules:
        tips.append("- Fact gathering is slow. Consider:")
        tips.append("    - 'gather_facts: false' if facts aren't needed")
        tips.append("    - 'gather_subset: min' or specific subsets")
        tips.append("    - Enable fact caching: 'fact_caching = jsonfile' in ansible.cfg")

    if any(m in slow_modules for m in ["package", "yum", "apt", "dnf"]):
        tips.append("- Batch package installations into single tasks")

    if any(m in slow_modules for m in ["command", "shell", "raw"]):
        tips.append("- Long-running commands may benefit from async/poll")

    if "copy" in slow_modules or "template" in slow_modules:
        tips.append("- For many files, consider synchronize or archive modules")

    if not tips:
        tips.append("- Use 'free' strategy for independent tasks")
        tips.append("- Enable pipelining in ansible.cfg for SSH performance")

    lines.extend(tips)

    return [TextContent(type="text", text="\n".join(lines))]


# Main Entry Point
async def main():
    init_options = InitializationOptions(
        server_name="ara-records-ansible",
        server_version=SERVER_VERSION,
        capabilities={},
    )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
