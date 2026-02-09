# TODO

Ideas for future development.

## scan_sensitive_output

### Use case

A user wants to know if any recorded output from a playbook run contains
secrets, credentials or other sensitive data that should not have been logged.
This can happen when a task is missing `no_log: true`, when secrets are passed
inline to `shell`/`command` modules, or when a module unexpectedly dumps
credentials in its return data.

Manually reviewing hundreds of results is tedious. This tool iterates through
a playbook's results and flags anything that looks like it shouldn't be there.

### Implementation

1. Fetch the playbook to confirm it exists and get basic metadata.

2. Fetch all results for the playbook (`list results, playbook=<id>`).
   Paginate if necessary — a playbook can have thousands of results.

3. For each result, fetch the full result data (`get results/<id>`) and
   inspect the `content` dict. Check the following fields:
   - `stdout`
   - `stderr`
   - `msg`
   - `cmd` (for shell/command tasks — the actual command string)
   - Any other string values in content (some modules return arbitrary keys)

4. Match against a set of patterns. Two categories:

   **Structural patterns** (regex):
   - AWS keys: `AKIA[0-9A-Z]{16}`
   - Private keys: `BEGIN (RSA|DSA|EC|OPENSSH) PRIVATE KEY`
   - Generic secrets: `password\s*[:=]`, `token\s*[:=]`, `secret\s*[:=]`
   - Connection strings with embedded credentials: `://[^:]+:[^@]+@`
   - Hexadecimal tokens of suspicious length (e.g., 40+ hex chars)
   - Common variable names in command strings: `--password`, `-p <arg>`

   **Contextual checks** (based on task metadata):
   - Tasks using `shell`, `command`, `raw` where the `cmd` field contains
     any of the structural patterns above (secrets passed as CLI arguments)
   - Tasks where `no_log` is `false` (explicit) and the action is a module
     known to handle credentials (`user`, `uri`, `mysql_user`, `postgresql_user`,
     `vault_write`, etc.)

5. Also check recorded files (`list files, playbook=<id>`) for variables
   files or templates that contain patterns. This is a lighter check — just
   flag files whose content matches credential patterns, not a deep analysis.

6. Report findings grouped by severity:
   - **High**: Actual credential-like strings found in result output
   - **Medium**: Tasks using credential-related modules without `no_log`
   - **Info**: `shell`/`command` tasks with arguments that look like they
     might contain secrets but can't be confirmed from the recorded data

7. For each finding, include the result ID, task name, host, the matched
   pattern (but not the actual secret — truncate/redact the match) and a
   URL to the result in the ARA web UI.

### Parameters

- `id` (required): Playbook ID or ARA URL
- `include_files` (optional, default false): Also scan recorded file contents
- `limit` (optional, default 500): Max results to scan (safety valve for
  very large playbooks)

### Notes

- The tool should redact matched values in its own output. If it finds
  `password=hunter2` it should report `password=****` — we don't want
  to relay the secret back through the model's context.
- False positives are acceptable and expected. The point is to surface
  things worth reviewing, not to be a definitive security scanner.
- Long hex strings will be noisy. Consider only flagging them when they
  appear in fields that wouldn't normally contain hex data.


## compare_playbooks

### Use case

A user wants to understand what changed between two runs of the same playbook.
The most common scenario: "this worked yesterday and it's broken today."
Rather than manually querying both runs and cross-referencing results, this
tool does the comparison in one call.

It can also be used proactively — comparing a staging run to a production run,
or a run before a change to a run after.

### Implementation

1. Accept two playbook IDs. Designate them "before" and "after" (or "A" and
   "B"). If only one ID is provided, automatically find the previous run of
   the same playbook by looking up its path and fetching the most recent
   earlier run (`list playbooks, path=<path>, order=-started, limit=2`).

2. Fetch both playbooks to get their metadata. Report:
   - Status comparison (e.g., completed → failed)
   - Duration comparison (and delta)
   - Ansible/Python version differences (if any)
   - Controller differences (different CI runner? different user?)

3. **File comparison (code changes).** This is the cheapest and often most
   informative part of the comparison. ARA deduplicates file content by
   SHA1 hash — if the same file is recorded across 20 playbook runs and
   hasn't changed, it is stored once. This means comparing files between
   two runs is a hash comparison, not a content comparison.

   Implementation:

   a. Fetch all files for both playbooks:
      `list files, playbook=<A_id>` and `list files, playbook=<B_id>`.
      Each file has a `path` and a `sha1`.

   b. Build a lookup by path for each playbook:
      `{path: {id, sha1}}` for A and B.

   c. Compare:
      - **Same path, same SHA1**: file unchanged. Skip it.
      - **Same path, different SHA1**: file was modified between runs.
        Fetch content for both versions (`get files/<A_id>` and
        `get files/<B_id>`) and produce a unified diff.
      - **Path only in B**: new file (added since the previous run).
      - **Path only in A**: removed file (no longer part of the playbook).

   d. Report changed files first (with diffs), then new files, then
      removed files. For diffs, use Python's `difflib.unified_diff` to
      produce a standard unified diff format that models understand well.

   e. Keep diffs concise. If a file diff is very large (e.g., a generated
      file or a large vars file), truncate and note the total number of
      changed lines. Prioritize diffs of task files and role files over
      vars files and templates.

   Because most runs of the same playbook will have identical files, this
   step is usually fast: the common case is "0 files changed" and no
   content needs to be fetched at all.

   When files *have* changed, the diff is the most valuable part of the
   comparison — it directly answers "what code change caused this
   regression?" without the user having to dig through version control.

4. **Result comparison.** Fetch results for both playbooks. Build lookup
   maps keyed by `(task_name, task_action, host_name)` — this is the
   natural join key since task IDs are different across runs but task
   names and host names should be stable.

5. Classify each task+host pair into one of:
   - **Regression**: was ok/changed in A, now failed in B
   - **Fixed**: was failed in A, now ok/changed in B
   - **New failure**: task+host exists only in B and is failed
   - **Gone**: task+host existed in A but not in B (task removed or host
     no longer targeted)
   - **Status changed**: any other status transition (e.g., ok → skipped)
   - **Timing change**: same status but duration changed by more than a
     configurable threshold (e.g., 2x slower or faster)

6. **Correlate file changes with regressions.** If a task that regressed
   in step 5 is defined in a file that changed in step 3, highlight this
   connection explicitly: "Task 'Install packages' regressed on host
   web-01. The file it is defined in (roles/app/tasks/main.yml) was
   modified between runs — see diff above." This is the high-value
   insight that ties code changes to outcome changes.

7. Report in order:
   - File changes (diffs) — this is what changed in the code
   - Regressions — correlated with file diffs where applicable
   - Fixes
   - New failures, gone tasks, timing changes

8. Summarize: "2 files changed, 3 regressions (2 in changed files),
   1 fix, 0 removed tasks."

### Parameters

- `id` (required): Playbook ID or ARA URL for the "after" run
- `before` (optional): Playbook ID or ARA URL for the "before" run.
  If omitted, automatically find the previous run by path.
- `include_diffs` (optional, default true): Show unified diffs for changed
  files. If false, only list which files changed without fetching content.
- `timing_threshold` (optional, default 2.0): Factor of duration change
  to report as a timing anomaly (2.0 = report if 2x slower or faster)

### Notes

- **File comparison is the fast path.** In most cases, comparing SHA1 hashes
  across two runs will show zero changes and requires no content fetches at
  all. Only fetch file content when hashes differ. This makes the file
  comparison the cheapest step despite being the most informative.
- **File paths are the join key for files**, just as `(task_name, task_action,
  host_name)` is for results. A renamed file will appear as "removed" + "new"
  rather than "modified." This is uncommon but worth noting in output when
  a removed and a new file share the same basename.
- The join on `(task_name, task_action, host_name)` for results is imperfect.
  If someone renames a task between runs, it will show up as "gone" + "new"
  rather than a changed task. This is acceptable — the tool should note this
  caveat in its output when it detects tasks that are gone in one run and new
  in another with the same action and line number.
- **Correlating file diffs to regressions** is the highest-value output of
  this tool. When a task regresses and its source file changed, that's almost
  always the cause. Use the task's `file` and `lineno` fields to map results
  back to files.
- Limit the result fetching to a reasonable number (e.g., 500 per playbook).
  For very large playbooks, focus the comparison on failed and changed results
  first.
- The automatic "find previous run" feature should only match playbooks with
  the same path. It should not match by name alone since names are optional
  and not unique.


## compare_hosts

### Use case

A user has a playbook that targets multiple hosts and wants to know why some
hosts succeeded while others failed, or why one host took significantly
longer than the rest. In fleet deployments where you expect uniform outcomes,
this tool surfaces the outliers.

Also useful for drift detection: "are all my web servers still on the same
OS and kernel version?"

### Implementation

1. Fetch the playbook and its list of hosts (`list hosts, playbook=<id>`).

2. For each host, fetch host details including facts (`get hosts/<id>`).
   Build a profile for each host:
   - Result counts: ok, changed, failed, skipped, unreachable
   - Key facts: OS, version, kernel, architecture, Python, package manager,
     SELinux status

3. **Result consistency check**: Identify the "majority outcome" for each
   task across hosts. For example, if task "Install nginx" was ok on 9 hosts
   and failed on 1, that 1 host is the outlier.

   To do this: fetch results for the playbook, group by task (using task ID),
   and for each task check if any host's status differs from the majority.
   Report tasks where hosts diverged, showing which hosts had which status.

4. **Fact comparison**: Compare facts across all hosts. Flag differences:
   - Different OS or OS version
   - Different kernel
   - Different Python version
   - Different package manager
   - Different SELinux status
   - Different architecture

   Present this as: "All hosts run Ubuntu 24.04 except host-7 which runs
   22.04" rather than dumping a table of every host's facts.

5. **Timing outliers**: For each task, compare durations across hosts. Flag
   hosts where a task took significantly longer than the median for that task
   across all hosts (e.g., more than 3x the median). This can reveal network
   issues, disk problems or resource contention on specific hosts.

6. Report in order:
   - Hosts with failures (if any), with the specific tasks that failed
   - Fact divergences
   - Timing outliers
   - A summary line: "12 hosts, 11 consistent, 1 outlier (host-7)"

### Parameters

- `id` (required): Playbook ID or ARA URL
- `include_facts` (optional, default true): Compare host facts
- `timing_threshold` (optional, default 3.0): Multiplier of median duration
  to flag as an outlier

### Notes

- This tool will make many API calls (hosts * tasks in the worst case).
  Optimize by fetching results in bulk (`list results, playbook=<id>`) and
  grouping in Python rather than fetching per-host.
- For playbooks with a single host, the tool should say so and suggest
  using `troubleshoot_playbook` or `analyze_performance` instead.
- Facts may not be available if `gather_facts: false` was used. The tool
  should handle this gracefully and skip the fact comparison section.
- The task grouping should use task ID (stable within a playbook run) rather
  than task name (which could be duplicated across roles).


## summarize_changes

### Use case

A user has run a playbook and wants to know what it actually changed on the
target systems. They don't want to scroll through hundreds of "ok" results —
they want to see the tasks that modified state.

This is the "what did this playbook actually do?" question. It's especially
relevant after running a playbook for the first time on new infrastructure,
after a refactor, or when reviewing what a colleague's playbook touched.

### Implementation

1. Fetch the playbook metadata.

2. Fetch results with `changed=true` for the playbook
   (`list results, playbook=<id>, changed=true`). This is the starting set.

3. For each changed result, fetch the full result data and classify it into
   one of two categories:

   **Real changes** — the task demonstrably modified system state:
   - Modules with built-in idempotency that report `changed` meaningfully:
     `package`/`apt`/`dnf`/`yum` (package installed/removed),
     `service`/`systemd` (service state changed),
     `copy`/`template`/`file` (file created/modified/permissions changed),
     `user`/`group` (user/group created/modified),
     `lineinfile`/`blockinfile` (file content modified),
     `cron` (cron job added/modified),
     `sysctl`, `mount`, `firewalld`, `selinux`, etc.
   - Any module where `changed=true` and the result content includes a `diff`
     key (Ansible's `--diff` mode was used, confirming actual changes)

   **Possibly spurious changes** — the task returned `changed` but may not
   have actually modified anything:
   - `shell`, `command`, `raw`, `script` — these always return `changed=true`
     unless `changed_when` was explicitly set. Flag these with a note:
     "This task uses the `shell` module which reports changed by default.
     Without `changed_when`, this may not reflect an actual change."
   - `uri`/`win_uri` — HTTP calls that return changed by default

   The classification is based on the task's `action` (module name). Maintain
   a list of modules known to report spurious changes.

4. For each changed result, extract a brief description of what changed:
   - For `package` tasks: the package name and version from `content`
   - For `copy`/`template`: the destination path from `content`
   - For `service`/`systemd`: the service name and state change
   - For `command`/`shell`: the command that was run (from `content.cmd`)
   - For tasks with a `diff` in content: note that a diff is available
   - Fallback: the task name and module

5. Group the report by host, then by classification:

   ```
   Host: web-01
     Real changes (7):
       - Installed nginx 1.24.0 (apt)
       - Created /etc/nginx/sites-available/app.conf (template)
       - Enabled and started nginx (systemd)
       ...
     Possibly spurious (2):
       - "Run database migration" (shell) — changed_when not set
       - "Check connectivity" (command) — changed_when not set

   Host: web-02
     ...
   ```

6. End with a summary:
   - Total changed results
   - Breakdown: N real changes, M possibly spurious
   - If all changes are from `shell`/`command` without `changed_when`:
     suggest adding `changed_when` to improve idempotency reporting

### Parameters

- `id` (required): Playbook ID or ARA URL
- `include_spurious` (optional, default true): Include possibly spurious
  changes in the report. If false, only show changes from modules with
  meaningful changed reporting.
- `group_by` (optional, default "host"): Group results by "host" or "role"
  (role grouping uses the file path to infer which role a task belongs to)

### Notes

- The spurious change detection is heuristic. A `shell` task with a well-set
  `changed_when` will correctly report `changed=true` only when something
  changed — but we cannot tell from the recorded data whether `changed_when`
  was set and evaluated to true, or whether it was absent and the module
  defaulted to `changed`. ARA records the final `changed` boolean but not
  whether `changed_when` was involved.
  The safest heuristic: if the action is `shell`/`command`/`raw`/`script`,
  always flag it as "possibly spurious" with the advisory note. Users who
  know they have proper `changed_when` can ignore the caveat.
- The `diff` content (from `--diff` mode) can be large. Don't include the
  full diff in the tool output — just note that a diff is available and link
  to the result URL where the user can view it.
- For playbooks with many hosts doing the same thing, consider collapsing
  identical changes across hosts: "All 12 hosts: installed nginx 1.24.0"
  rather than listing each host separately.


## get_help

### Use case

A model or user wants to understand what the ARA MCP server can do, how the
tools work, and how to approach common tasks like troubleshooting or
performance analysis. Today this information lives in AGENTS.md on disk,
which is useful for humans and for agents that can read files, but is not
accessible through the MCP protocol itself.

A `get_help` tool makes this guidance available as a first-class tool call.
The model can ask for help when it's unsure how to proceed, and the user can
call it directly to learn what's available — without the content being
injected into every conversation unconditionally.

### Implementation

1. On startup (or on first call), read `AGENTS.md` from disk. Locate it
   relative to the script itself:

   ```python
   agents_md_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AGENTS.md")
   ```

2. Return the full content of AGENTS.md verbatim.

3. If `AGENTS.md` is not found on disk, return a short fallback message:

   ```
   AGENTS.md not found. For guidance on using the ARA MCP server, see:
   https://codeberg.org/ansible-community/ara/src/branch/master/contrib/mcp/AGENTS.md
   ```

### Parameters

None.

### Notes

- This tool reads from disk rather than embedding the content as a string
  constant. This means AGENTS.md is the single source of truth — edits to
  the file are immediately reflected without touching the Python code.
  The tradeoff is that `ara_mcp.py` is no longer fully self-contained,
  but anyone who obtained the script from the repository already has
  AGENTS.md next to it.
- Cache the file content after the first read. AGENTS.md is not expected to
  change while the MCP server is running.
- The tool description in the MCP tool catalog should be clear enough that
  models know to call it when they need orientation: something like
  "Learn what this MCP server can do, how the tools work, and how to
  approach common Ansible troubleshooting tasks."
