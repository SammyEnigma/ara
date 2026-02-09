# AGENTS.md — Guidance for AI Agents Using ARA MCP Tools

Humans can read this, too, but it is meant as high level context and instructions for LLMs.

## What is ARA?

ARA Records Ansible. It captures data from `ansible-playbook` runs (every
play, task, result, host and file) and stores it in a database (SQLite,
MySQL or PostgreSQL) through a Django REST API.

This MCP server gives you read-only access to that recorded data. You cannot
modify playbooks, trigger runs or change configuration through these tools.
You can only observe what has already happened.

## Project Philosophy

**Simplicity is a feature.** ARA exists to make Ansible easier to understand
and troubleshoot, not to add complexity. The MCP server follows the same
principle: it provides the minimum necessary tools for you to query the data
you need, nothing more.

**Read-only by design.** These tools are strictly for querying and analyzing
recorded playbook data. There are no write operations. This is intentional.
You are an observer looking at what Ansible did, not an actor changing what
it will do.

**Text over JSON.** Although ARA's API returns JSON, every tool in this MCP
server returns plain text formatted for readability and token efficiency. This
is deliberate: structured text is easier for you to reason about and cheaper
on context than raw JSON payloads.

**Token economy matters.** A major motivation for this MCP server is that
pasting raw `ansible-playbook` console output into a conversation is
expensive (often 60,000+ tokens for a single CI job). These tools return
the same information in a fraction of that cost (~2,000 tokens) because
you can ask for exactly what you need.

**Standalone and self-contained.** This MCP server is a single Python file
with minimal dependencies (`httpx` and `mcp`). It is intentionally not part
of the `ara` package and must be obtained from the source repository at
https://codeberg.org/ansible-community/ara/src/branch/master/contrib/mcp.

## Data Model

Understanding how ARA organizes data will help you query efficiently.

```
Playbook (one ansible-playbook invocation)
├── has many Tasks (each task definition in the playbook)
├── has many Hosts (each host targeted)
├── has many Results (one per task * host combination)
└── has many Files (playbooks, roles, templates recorded during the run)
```

Key relationships:

- A **playbook** is a single `ansible-playbook` execution. It has a status
  (completed, failed, running, expired), duration, and metadata about the
  Ansible and Python versions used, the controller (machine that ran it),
  and the user who ran it.
- A **task** belongs to a playbook. It has an action (the Ansible module used,
  e.g., `apt`, `copy`, `command`), a name, a file path and line number where
  it is defined.
- A **host** belongs to a playbook. It has aggregate result counts (ok,
  changed, failed, skipped, unreachable) and optionally Ansible facts
  (OS, kernel, Python version, etc.).
- A **result** is the outcome of a single task on a single host. It has a
  status (ok, failed, skipped, unreachable), optional stdout/stderr, return
  codes and error messages. This is where the actual failure details live.
- A **file** is a playbook, role, task file or template that was recorded.
  You can retrieve its content and look at specific lines.

Every object has a numeric ID. Tools accept either a numeric ID or a full
ARA URL (e.g., `https://demo.recordsansible.org/playbooks/4307.html`).

## How to Use the Tools

### Start broad, then drill down

The most effective pattern is top-down investigation:

1. **Start with the playbook.** Use `get_playbook` or `list_playbooks` to
   understand the overall status. Is it failed? How long did it take?
   How many tasks and hosts are involved?

2. **Narrow to failures.** If troubleshooting, use `troubleshoot_playbook`
   for an aggregated failure report, or `list_results` with
   `status=failed` to find the specific failures.

3. **Examine individual results.** Use `get_result` to see the full error
   message, stdout/stderr and the source code context around the failing
   task.

4. **Check the host.** Use `get_host` to see OS facts, Python version and
   other details that might explain environment-specific failures.

5. **Read the source.** Use `get_file` with a line number to see the exact
   playbook or role code around a failure.

### Use the wrapper tools first

For common workflows, prefer the wrapper tools over manual multi-step
queries:

- **`troubleshoot_playbook`** — When a playbook has failed. This single call
  aggregates all failures, their error messages, the source code context and
  relevant host facts. It is almost always the right starting point for
  debugging.

- **`troubleshoot_host`** — When a specific host is having problems across
  runs. This gives you a host-centric view of failures.

- **`analyze_performance`** — When a playbook is slow. This identifies the
  slowest tasks, breaks down time by Ansible module, compares performance
  across multiple runs of the same playbook, and suggests optimizations.

These wrapper tools make multiple API calls internally and return a
consolidated report. They save you (and the user) significant back-and-forth.

### Use foundation tools for targeted follow-up

After reviewing a wrapper tool's output, you may want more detail on a
specific task, result or file. That's what the foundation tools are for:

- `get_task` — To see a task's full definition and source code.
- `get_result` — To see the complete stdout/stderr of a specific failure.
- `get_file` with a `line` parameter — To see more source code context.
- `list_results` with filters — To check if a task failed on some hosts but
  not others (`status=failed`, `task=<id>`).
- `list_playbooks` with `path` filter — To find previous runs of the same
  playbook and compare outcomes.

### URLs in responses

Every tool response includes ARA web UI URLs for the objects it references.
When presenting findings to the user, include these URLs so they can inspect
the data directly in the ARA web interface if they want more detail.

## Common Investigation Patterns

### "Why did this playbook fail?"

```
troubleshoot_playbook(id=<playbook_id>)
```

This gives you everything you need in one call. Read the failure list,
check the error messages and code context. If you need more detail on a
specific failure, follow up with `get_result`.

### "Is this host broken?"

```
troubleshoot_host(id=<host_id>)
```

Check if the failures are consistent (same task every time) or intermittent.
Look at the host facts — is it a different OS version? Missing Python?

### "This playbook is too slow"

```
analyze_performance(id=<playbook_id>)
```

Look at the slowest tasks and the module breakdown. Common culprits:
slow fact gathering (consider `gather_subset: min`), sequential package
installs (batch them), low fork count (raise `forks` in ansible.cfg).

### "Did this work before?"

```
list_playbooks(path=<playbook_path>, order="-started", limit=10)
```

Find recent runs of the same playbook. Compare statuses and durations.
If a previously-passing playbook now fails, focus on what changed between
the last success and the first failure.

### "What changed on this host?"

```
get_host(id=<host_id>)
```

Check the Ansible facts for OS version, kernel, Python version, package
manager. Compare with a working host if one exists.

## Things to Keep in Mind

- **IDs are per-playbook-run.** A host ID in ARA is not a global host
  identifier — it represents that host in the context of a specific
  playbook execution. The same physical host will have different ARA host
  IDs across different playbook runs.

- **Pagination exists.** List tools return paginated results. If a playbook
  has hundreds of tasks or results, you may not see all of them in one call.
  Use the `limit` parameter and filter aggressively (by status, by task, by
  host) rather than trying to fetch everything.

- **`ignore_errors` matters.** Some Ansible tasks are expected to fail and
  have `ignore_errors: true`. The `troubleshoot_playbook` tool excludes
  these by default. If a user says something like "there are errors but the
  playbook shows as completed," try re-running with `include_ignored=true`.

- **Facts may not be available.** If a playbook uses `gather_facts: false`,
  host facts will be empty. This is normal and doesn't indicate a problem.

- **Duration strings are HH:MM:SS.** The `duration` field from ARA is a
  string like `0:01:23.456789`. The wrapper tools parse these for you, but
  if you're working with raw foundation tool output, keep the format in mind.

- **The controller is the machine that ran Ansible**, not the target host.
  This is useful for distinguishing CI runners, bastion hosts or different
  developer workstations.

## API Fixtures

Sample API responses are available in the `contrib/mcp/fixtures/` directory:

```
fixtures/
├── playbooks.txt   # GET /api/v1/playbooks (list)
├── playbook.txt    # GET /api/v1/playbooks/1 (detail)
├── tasks.txt       # GET /api/v1/tasks (list)
├── task.txt        # GET /api/v1/tasks/1 (detail)
├── hosts.txt       # GET /api/v1/hosts (list)
├── host.txt        # GET /api/v1/hosts/1 (detail)
├── results.txt     # GET /api/v1/results (list)
├── result.txt      # GET /api/v1/results/1 (detail)
├── files.txt       # GET /api/v1/files (list)
└── file.txt        # GET /api/v1/files/1 (detail)
```

Each file contains the raw JSON response from the ARA API. Use these to
understand the shape of the data that the MCP tools work with — what fields
are available, how relationships are represented (nested objects vs. bare
IDs), and what the values look like in practice.

These fixtures were captured from https://demo.recordsansible.org. They are
`.txt` files rather than `.json` because each one has a comment on the first
line identifying the API endpoint it came from. This comment is not valid JSON
but is useful context for both humans and models reading the file.

If the fixtures need to be refreshed, re-run the corresponding API calls:

```bash
# List endpoints
for resource in playbooks tasks hosts results files; do
  echo "# /api/v1/${resource}" > fixtures/${resource}.txt
  curl -s "https://demo.recordsansible.org/api/v1/${resource}?limit=5" | python3 -m json.tool >> fixtures/${resource}.txt
done

# Detail endpoints (use IDs matching the current fixtures)
echo "# /api/v1/playbooks/4448" > fixtures/playbook.txt
curl -s https://demo.recordsansible.org/api/v1/playbooks/4448 | python3 -m json.tool >> fixtures/playbook.txt

echo "# /api/v1/task/653231" > fixtures/task.txt
curl -s https://demo.recordsansible.org/api/v1/tasks/653231 | python3 -m json.tool >> fixtures/task.txt

echo "# /api/v1/hosts/13076" > fixtures/host.txt
curl -s https://demo.recordsansible.org/api/v1/hosts/13076 | python3 -m json.tool >> fixtures/host.txt

echo "# /api/v1/results/995551" > fixtures/result.txt
curl -s https://demo.recordsansible.org/api/v1/results/995551 | python3 -m json.tool >> fixtures/result.txt

echo "# /api/v1/files/1" > fixtures/file.txt
curl -s https://demo.recordsansible.org/api/v1/files/1 | python3 -m json.tool >> fixtures/file.txt
```

## What You Should Not Do

- **Don't fetch everything.** Resist the urge to call `list_results` with no
  filters on a playbook with thousands of results. Filter by status, task or
  host first.

- **Don't guess at IDs.** IDs are returned by list and get tools. Use them
  from previous tool responses rather than assuming sequential numbering.

- **Don't confuse ARA data with live system state.** ARA records what happened
  during a playbook run. The current state of a host may have changed since
  the run was recorded.

- **Don't recommend changes to this MCP server.** If something seems wrong
  with the tool responses, report it as a potential issue rather than
  suggesting code modifications. Simplicity is a feature and changes should
  be deliberate.
