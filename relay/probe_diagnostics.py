"""Emit only fixed codes and small metadata from explicitly enabled synthetic probes."""
import logging
import re


log = logging.getLogger("relay.probe")
CODES = frozenset("""
    WORKER_CONNECT WORKER_REPLY_INVALID WORKER_UNAVAILABLE SANDBOX_PREPARE_FAILED
    BINARY_START_FAILED BWRAP_START_FAILED VERSION_CHECK VERSION_FAILED VERSION_MISMATCH
    ACCOUNT_CHECK ACCOUNT_AUTH_FAILED CLI_CONFIG_INCOMPATIBLE CLI_START CLI_EXIT
    INFERENCE_TRANSPORT_FAILED CLI_FAILED CLI_EXITED_WITHOUT_MCP_COMPLETION TIMEOUT
    MCP_AUTH_ACCEPTED MCP_AUTH_REJECTED MCP_HTTP MCP_INITIALIZE MCP_TOOLS_LIST MCP_RPC_ERROR
    MCP_CLIENT_INIT_FAILED MCP_CLIENT_AUTH_FAILED MCP_CLIENT_LIST_FAILED
    MCP_GET_PROJECT_CONTEXT MCP_HEARTBEAT_JOB MCP_OTHER_CALL MCP_TOOL_FAILED
    MCP_TOOLS_LIST_RESULT MCP_RESPONSE_UNCLASSIFIED
    CLI_MCP_CALL_STARTED CLI_MCP_CALL_COMPLETED CLI_MCP_CALL_FAILED CLI_MCP_APPROVAL_REQUIRED
    CLI_FINAL_RESPONSE_PRESENT CLI_FINAL_RESPONSE_ABSENT CLI_TURN_COMPLETED CLI_TURN_FAILED
    CLI_JSON_UNREADABLE CLI_JSON_TRUNCATED CODE_MODE_HOST_UNAVAILABLE
    PROBE_COMPLETED
""".split())


def emit(code, *, enabled, job_id=None, provider=None, role=None, status=None, exit_code=None,
         tool_count=None, tool=None):
    if not enabled or code not in CODES:
        return
    job = job_id if isinstance(job_id, str) and re.fullmatch(r"[0-9a-f]{32}", job_id) else "-"
    name = provider if provider in {"codex", "claude_code", "antigravity"} else "-"
    kind = role if role in {"management", "audit"} else "-"
    http = int(status) if isinstance(status, int) and 100 <= status <= 599 else "-"
    rc = exit_code if type(exit_code) is int and -255 <= exit_code <= 255 else "-"
    count = tool_count if type(tool_count) is int and 0 <= tool_count <= 1000 else "-"
    safe_tool = tool if tool in ("get_project_context", "heartbeat_job") else "-"
    log.warning("probe code=%s job=%s provider=%s role=%s http=%s exit=%s tools=%s tool=%s",
                code, job, name, kind, http, rc, count, safe_tool)
