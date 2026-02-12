# ARA MCP Server — Review

## What it is

This is a standalone MCP (Model Context Protocol) server that gives LLMs read-only access to data recorded by ARA (ARA Records Ansible). ARA captures everything that happens during `ansible-playbook` runs — every play, task, result, host, and file — and stores it in a database through a Django REST API. This MCP server sits in front of that API and lets an agent query the recorded data through structured tool calls.

The key problem it solves is token economy. Pasting raw `ansible-playbook` console output into a conversation typically costs 60,000+ tokens for a single CI job. Through these tools, an agent gets the same information in ~2,000 tokens because it can ask for exactly what it needs — just the failures, just the slow tasks, just the host facts.

## Architecture

Single-file Python server (`httpx` + `mcp`, ~1,600 lines) with three layers:

### Foundation tools (10 tools)

`get_*` and `list_*` for the five ARA data types — playbooks, tasks, hosts, results, and files. These map directly to the REST API endpoints. List tools support filtering (by status, playbook, host, action, etc.) and ordering. Every tool returns plain text with ARA web UI URLs for cross-referencing.

### Wrapper tools (3 tools)

These orchestrate multiple API calls internally and return consolidated reports:

- **`troubleshoot_playbook`** — The primary debugging entry point. Fetches the playbook, finds all failed results, pulls the full error details, extracts code snippets around failing lines, and surfaces host facts (OS, Python version, memory, network, etc.) for every affected host. Excludes `ignore_errors` failures by default with a flag to include them.
- **`troubleshoot_host`** — Host-centric failure view across runs. Shows failures and unreachable results with error messages, useful for diagnosing host-specific issues.
- **`analyze_performance`** — Identifies the slowest tasks, breaks down time by Ansible module, compares performance across multiple runs of the same playbook (by path), flags inconsistent tasks across controllers, and suggests concrete optimizations (forks, fact caching, pipelining, etc.).

### Formatting layer

All output is deliberately plain text, not JSON. This is cheaper on tokens and easier for models to reason about. Every object includes its ARA web UI URL so the user can click through for more detail.

## How it connects to ARA

The `ARAClient` class manages an async httpx client pointed at the ARA API server (configured via `ARA_API_SERVER`, defaulting to `http://127.0.0.1:8000`). It supports optional basic auth via `ARA_API_USERNAME` / `ARA_API_PASSWORD`. The client identifies itself with a `User-Agent: ara-contrib-mcp/<version>` header, with the version string defined once as a `SERVER_VERSION` constant shared between the User-Agent and the MCP server registration.

All tools accept either numeric IDs or full ARA web UI URLs (e.g., `https://demo.recordsansible.org/playbooks/4307.html`), with origin validation to ensure URLs match the configured server.

## Error handling

The HTTP layer translates specific failure modes into descriptive Python exceptions: `ConnectionError` when the server is unreachable (with the configured URL and a hint to check `ARA_API_SERVER`), `TimeoutError` for slow responses, `PermissionError` for 401/403 with guidance about credentials, `LookupError` for 404s, and `RuntimeError` for 500+ server errors. The tool router catches each type separately and returns a clear, actionable message rather than a raw traceback or a generic "API error: 404".

## Concurrency

The wrapper tools are the most API-call-intensive part of the server. A `batch_get()` method on the client uses `asyncio.gather` with `return_exceptions=True` to fetch multiple resources concurrently without one failure aborting the batch.

- **`troubleshoot_playbook`**: fetches all failed results concurrently, then pre-fetches all referenced files and hosts in a single concurrent batch. A playbook with 10 failures across 3 hosts touching 4 files goes from ~17 sequential round-trips to ~4.
- **`troubleshoot_host`**: fetches failed and unreachable result lists concurrently, then batch-fetches the full result details.
- **`analyze_performance`**: the cross-run analysis was the biggest bottleneck — 5 playbooks × 25 results could mean 130+ sequential calls. Now it's 5 concurrent list calls plus one batch of up to 125 result fetches. The current-playbook section similarly batches all result detail fetches.

## Host facts

A shared `format_host_facts()` helper formats host facts consistently across all three places that display them (`get_host`, `troubleshoot_playbook`, `troubleshoot_host`). The output mirrors the ARA web dashboard's three sections:

- **System & Environment**: OS, kernel, Python (from structured `ansible_python.version` with fallback to `ansible_python_version`), user/shell, virtualization type/role, SELinux status/mode, package manager, uptime (human-readable), and load averages.
- **Processor, Memory & Storage**: CPU model, cores/threads, RAM usage with percentages, swap (only shown if configured), and disk mounts with usage percentages and free space.
- **Network**: hostname, FQDN, default IPv4/IPv6 with interface and gateway, and DNS nameservers.

## Code snippets

Failure reports and task details include source code context extracted from ARA's recorded files. Snippets show 10 lines of context on each side of the target line (21 lines total), with the relevant line marked by `>>>`. This gives enough surrounding context to understand the task block structure — the rescue block, the conditionals, the variable references.

## What the AGENTS.md provides

The companion `AGENTS.md` is written for LLMs and teaches the top-down investigation pattern: start with the playbook, narrow to failures, examine individual results, check the host, read the source. It documents the data model relationships, explains when to use wrapper tools vs. foundation tools, covers common investigation patterns ("why did this fail?", "is this host broken?", "this is too slow", "did this work before?"), and lists important gotchas (IDs are per-run, pagination, `ignore_errors`, facts may be empty, duration format).

## Observations for future iterations

These aren't blockers for a v1 — just things worth noting:

- The `msg` field in result content can be a dict (e.g., `msg` containing the full facts output as a nested object). The formatter does `f"Message: {msg}"` which will print a Python dict repr. Worth handling with truncation or a summary for non-string messages.
- The TODO.md has well-specified designs for `compare_playbooks` (diff two runs of the same playbook with file-level unified diffs and regression correlation), `scan_sensitive_output` (flag credentials leaked in task output), `compare_hosts` (find outlier hosts in multi-host runs), `summarize_changes` (what did the playbook actually modify), and `get_help` (expose AGENTS.md as a tool call). These are natural next additions.
- The `get_task` handler still does a sequential file fetch for code context — not worth batching for a single-resource call, but worth noting for consistency.
