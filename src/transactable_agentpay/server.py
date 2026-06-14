'''
FastMCP server entry point.

Exposes four tools that mirror the four /agentpay/* endpoints on cw-backend.
All four go through the Alchemy AgentPay proxy, which collects payment first.

Server name: transactable_agentpay_mcp (matches {service}_mcp convention).

Tool naming uses the `transactable_` prefix so this server can coexist with
other MCPs in a host without name collisions.
'''
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from transactable_agentpay.config import Config, load_config
from transactable_agentpay.models import (
    LookupInput,
    RegisterInput,
    ResponseFormat,
    UploadUrlInput,
    VerifyHashInput,
)
from transactable_agentpay.x402_proxy import (
    AgentPayClient,
    PaymentNotConfiguredError,
    PaymentRequiredError,
    ProxyError,
)

# stdio servers must never log to stdout — that channel is the MCP wire.
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger('transactable_agentpay')

mcp = FastMCP('transactable_agentpay_mcp')

INLINE_LIMIT_BYTES = 8 * 1024 * 1024  # AgentPay backend's inline cap.
TERMINAL_STATUSES = {'completed', 'completed_no_nft', 'failed'}


# --- Singleton config + client ------------------------------------------


_CONFIG: Optional[Config] = None
_CLIENT: Optional[AgentPayClient] = None


def _client() -> AgentPayClient:
    global _CONFIG, _CLIENT
    if _CLIENT is None:
        _CONFIG = load_config()
        _CLIENT = AgentPayClient(_CONFIG)
    return _CLIENT


def _config() -> Config:
    _client()  # ensures load
    assert _CONFIG is not None
    return _CONFIG


# --- Shared formatting helpers -------------------------------------------


def _format_error(code: str, message: str, **extra: Any) -> str:
    '''Single error shape across tools. JSON so agents can branch on `error`.'''
    payload: dict[str, Any] = {'error': code, 'detail': message}
    payload.update({k: v for k, v in extra.items() if v is not None})
    return json.dumps(payload, indent=2)


def _format_response(body: dict[str, Any], fmt: ResponseFormat, markdown_renderer) -> str:
    if fmt is ResponseFormat.JSON:
        return json.dumps(body, indent=2, default=str)
    return markdown_renderer(body)


# --- File helpers --------------------------------------------------------


def _read_file_for_inline(path: str) -> tuple[str, str, str, int, str]:
    '''Read a local file and prep the fields needed for inline transport.

    Returns (filename, content_type, base64_payload, byte_size, sha256_hex).
    '''
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f'No such file: {path}')

    data = p.read_bytes()
    sha256_hex = hashlib.sha256(data).hexdigest()

    content_type, _ = mimetypes.guess_type(p.name)
    if not content_type:
        content_type = 'application/octet-stream'

    return (
        p.name,
        content_type,
        base64.b64encode(data).decode('ascii'),
        len(data),
        sha256_hex,
    )


def _sha256_of_file(path: str) -> str:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f'No such file: {path}')
    hasher = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


async def _upload_to_signed_url(
    *,
    upload_url: str,
    upload_fields: dict[str, str],
    file_path: str,
    content_type: str,
) -> None:
    '''Stream a file to a signed GCS POST policy URL.'''
    p = Path(file_path).expanduser().resolve()
    async with httpx.AsyncClient(timeout=300.0) as client:
        with p.open('rb') as fh:
            files = {'file': (p.name, fh, content_type)}
            resp = await client.post(upload_url, data=upload_fields, files=files)
            if resp.status_code >= 400:
                raise ProxyError(
                    f'GCS upload failed: {resp.status_code} {resp.text[:200]}',
                    status_code=resp.status_code,
                )


# --- Tool: get_upload_url ------------------------------------------------


@mcp.tool(
    name='transactable_get_upload_url',
    annotations={
        'title': 'Get a signed upload URL for large deposit files',
        'readOnlyHint': False,  # creates a signed URL on the backend
        'destructiveHint': False,
        'idempotentHint': False,
        'openWorldHint': True,
    },
)
async def transactable_get_upload_url(params: UploadUrlInput) -> str:
    '''Get a short-lived signed Google Cloud Storage URL for files >= 8 MB.

    Use this only when you need to register a file that exceeds the 8 MB inline limit
    AND you want to upload it yourself (e.g., streaming from a pipeline). For most
    cases, just pass `file_path` to `transactable_register_copyright` and the MCP
    will handle the upload-or-inline decision automatically.

    Workflow:
      1. Call this tool with the filename and MIME type.
      2. POST the file to the returned `upload_url` using the returned `upload_fields`.
      3. Pass the returned `upload_token` to `transactable_register_copyright`.

    Returns JSON with: `upload_url`, `upload_fields`, `object_key`, `upload_token`,
    `expires_at`. Or an error envelope on failure.
    '''
    try:
        body = await _client().request(
            'POST',
            '/uploadurl/',
            json_body={'filename': params.filename, 'content_type': params.content_type},
        )
    except PaymentNotConfiguredError as exc:
        return _format_error('payment_not_configured', str(exc))
    except PaymentRequiredError as exc:
        return _format_error('payment_failed', str(exc))
    except ProxyError as exc:
        return _format_error(
            'proxy_error',
            str(exc),
            status_code=exc.status_code,
            upstream=exc.body,
        )

    return json.dumps(body, indent=2)


# --- Tool: verify_hash ---------------------------------------------------


@mcp.tool(
    name='transactable_verify_hash',
    annotations={
        'title': 'Check whether a file is already registered',
        'readOnlyHint': True,
        'destructiveHint': False,
        'idempotentHint': True,
        'openWorldHint': True,
    },
)
async def transactable_verify_hash(params: VerifyHashInput) -> str:
    '''Check whether a given file has already been registered with Transactable.

    Accepts either a pre-computed `sha256` (64 hex chars) or a local `file_path`
    (the MCP will compute the hash). Returns a JSON envelope listing any matching
    registrations with their numbers, statuses, and timestamps, or an empty list
    if the hash has not been registered.

    Useful as a pre-flight check before paying for a `transactable_register_copyright`
    call — though note that THIS call is itself paid through AgentPay.

    Args:
        params.sha256: 64-char hex SHA-256 digest. Optional if file_path is set.
        params.file_path: Absolute path to a local file. Optional if sha256 is set.

    Returns:
        JSON string. Success shape:
          {
            "matches": [
              {"registration_number": "<uuid>", "created_at": "...", "status": "..."},
              ...
            ]
          }
        Error shape:
          {"error": "<code>", "detail": "<message>", ...}
    '''
    sha256 = params.sha256
    if sha256 is None:
        if not params.file_path:
            return _format_error(
                'validation_error',
                'Either sha256 or file_path must be provided.',
            )
        try:
            sha256 = _sha256_of_file(params.file_path)
        except FileNotFoundError as exc:
            return _format_error('file_not_found', str(exc))

    try:
        body = await _client().request(
            'POST', '/verifyhash/', json_body={'sha256': sha256}
        )
    except PaymentNotConfiguredError as exc:
        return _format_error('payment_not_configured', str(exc))
    except PaymentRequiredError as exc:
        return _format_error('payment_failed', str(exc))
    except ProxyError as exc:
        return _format_error(
            'proxy_error', str(exc), status_code=exc.status_code, upstream=exc.body
        )

    body['_sha256_checked'] = sha256

    def render_md(b: dict[str, Any]) -> str:
        matches = b.get('matches', []) or []
        lines = [f'# Hash check: `{sha256}`', '']
        if not matches:
            lines.append('No prior registrations found for this hash.')
        else:
            lines.append(f'Found {len(matches)} prior registration(s):')
            lines.append('')
            for m in matches:
                lines.append(
                    f'- `{m["registration_number"]}` — status `{m["status"]}` '
                    f'(created {m.get("created_at") or "unknown"})'
                )
        return '\n'.join(lines)

    return _format_response(body, params.response_format, render_md)


# --- Tool: register_copyright -------------------------------------------


@mcp.tool(
    name='transactable_register_copyright',
    annotations={
        'title': 'Register a creative work on Transactable (paid)',
        'readOnlyHint': False,
        'destructiveHint': False,  # creates state but does not destroy anything
        'idempotentHint': False,   # each call creates a new registration row
        'openWorldHint': True,
    },
)
async def transactable_register_copyright(params: RegisterInput) -> str:
    '''Create a new provenance registration for a creative work.

    This is the primary tool. It pays AgentPay's per-call fee, then creates a
    registration on cw-backend covering hash + AI examination + certificate +
    optional NFT mint. The call returns immediately with a `registration_number`;
    use `transactable_lookup_registration` to poll for completion.

    File transport is automatic:
      - If `file_path` is provided and the file is < 8 MB, it is base64-inlined.
      - If `file_path` is provided and the file is >= 8 MB, the MCP first requests
        a signed upload URL, uploads the file directly to GCS, then registers
        with the resulting upload_token.
      - If you already uploaded the file yourself, pass `upload_token`.
      - You can also pass raw `file_base64` (<=8 MB) plus `file_name` and
        `file_content_type` for unusual setups.

    Wallet:
      - `wallet_address` is optional; if omitted, the address derived from the
        configured TRANSACTABLE_WALLET_PRIVATE_KEY is used. This is the standard
        case — the wallet paying for the call should be the wallet that owns the
        registration.

    Args:
        params.title: Title of the work, 1-200 chars.
        params.authors: At least one AuthorInput (name + optional email/country/year_born).
        params.claimants: Optional list of ClaimantInput. Defaults to first author.
        params.file_path | params.file_base64 (+ file_name + file_content_type) | params.upload_token.
        params.wallet_address: Optional override of the payer wallet.
        params.agent_name: Optional self-declared agent identifier.

    Returns:
        Success (JSON or Markdown depending on response_format):
          {
            "registration_number": "<uuid>",
            "status": "processing",
            "status_url": "https://.../agentpay/lookup/<uuid>",
            "estimated_completion_seconds": 90,
            "_sha256": "<computed when file_path/file_base64 used>"
          }
        Error envelope:
          {"error": "<code>", "detail": "<message>", ...}
    '''
    cfg = _config()
    client = _client()

    # 1. Resolve the file reference into the {transport: ...} dict the backend wants.
    file_ref: Optional[dict[str, Any]] = None
    computed_sha: Optional[str] = None

    try:
        file_ref, computed_sha = await _resolve_file_reference(params, client)
    except _UserError as exc:
        return _format_error(exc.code, exc.message)
    except ProxyError as exc:
        return _format_error(
            'proxy_error', str(exc), status_code=exc.status_code, upstream=exc.body
        )

    # 2. Resolve wallet address (param override > derived from key).
    wallet_address = params.wallet_address or client.configured_wallet_address
    if not wallet_address:
        return _format_error(
            'wallet_unresolved',
            'Could not determine a wallet_address. Either pass one explicitly or set '
            'TRANSACTABLE_WALLET_PRIVATE_KEY so the MCP can derive one.',
        )

    # 3. Build the payload mirroring agentpay/serializers.RegisterRequestSerializer.
    payload: dict[str, Any] = {
        'wallet_address': wallet_address,
        'title': params.title,
        'application_type': 'providence',
        'deposit_type': 'file',
        'authors': [a.model_dump(exclude_none=True) for a in params.authors],
        'file': file_ref,
    }
    if params.claimants:
        payload['claimants'] = [c.model_dump(exclude_none=True) for c in params.claimants]
    agent_name = params.agent_name or cfg.agent_name
    if agent_name:
        payload['agent_name'] = agent_name
    if cfg.model_info:
        payload['model_info'] = cfg.model_info

    # 4. Make the paid call.
    try:
        body = await client.request('POST', '/register/', json_body=payload)
    except PaymentNotConfiguredError as exc:
        return _format_error('payment_not_configured', str(exc))
    except PaymentRequiredError as exc:
        return _format_error('payment_failed', str(exc))
    except ProxyError as exc:
        return _format_error(
            'proxy_error', str(exc), status_code=exc.status_code, upstream=exc.body
        )

    if computed_sha:
        body['_sha256'] = computed_sha

    def render_md(b: dict[str, Any]) -> str:
        lines = [
            '# Registration submitted',
            '',
            f'- **Registration number**: `{b.get("registration_number")}`',
            f'- **Status**: `{b.get("status")}`',
            f'- **ETA**: ~{b.get("estimated_completion_seconds", "?")}s',
        ]
        if b.get('_sha256'):
            lines.append(f'- **SHA-256**: `{b["_sha256"]}`')
        if b.get('status_url'):
            lines.append('')
            lines.append(
                f'Poll completion: call `transactable_lookup_registration` with '
                f'`registration_number={b.get("registration_number")}` and `poll=true`.'
            )
        return '\n'.join(lines)

    return _format_response(body, params.response_format, render_md)


# --- Tool: lookup_registration ------------------------------------------


@mcp.tool(
    name='transactable_lookup_registration',
    annotations={
        'title': 'Get registration status and result (poll-capable)',
        'readOnlyHint': True,
        'destructiveHint': False,
        'idempotentHint': True,
        'openWorldHint': True,
    },
)
async def transactable_lookup_registration(params: LookupInput) -> str:
    '''Fetch the current state of a registration, optionally polling until terminal.

    The backend's terminal statuses are `completed`, `completed_no_nft`, and `failed`.
    With `poll=true`, this tool calls `/lookup/{id}` on `poll_interval_seconds`
    until it sees a terminal status or `poll_timeout_seconds` elapses.

    NOTE: Each lookup is a paid AgentPay call. Use `poll=true` judiciously — it
    can rack up calls for slow registrations. Default interval is 10s.

    Args:
        params.registration_number: UUID returned by transactable_register_copyright.
        params.poll: Poll until terminal if true (default false).
        params.poll_interval_seconds: 2.0 - 60.0 (default 10.0).
        params.poll_timeout_seconds: 10.0 - 600.0 (default 180.0).
        params.response_format: 'markdown' (default) or 'json'.

    Returns:
        JSON or Markdown rendering of the registration projection:
          {
            "registration_number": "<uuid>",
            "status": "...",
            "deposit_hash": "<sha256>",
            "deposit_size": <int>,
            "created_at": "...",
            "completed_at": "..." | null,
            "certificate_url": "..." | null,
            "nft_transaction_hash": "..." | null,
            "nft_token_id": <int|null>,
            "examination_summary": {"copyrightability": bool, "ai_flag": bool} | null
          }
    '''
    client = _client()
    path = f'/lookup/{params.registration_number}'

    start = time.monotonic()
    last_body: dict[str, Any] = {}
    polls = 0

    while True:
        try:
            last_body = await client.request('GET', path)
            polls += 1
        except PaymentNotConfiguredError as exc:
            return _format_error('payment_not_configured', str(exc))
        except PaymentRequiredError as exc:
            return _format_error('payment_failed', str(exc))
        except ProxyError as exc:
            return _format_error(
                'proxy_error',
                str(exc),
                status_code=exc.status_code,
                upstream=exc.body,
            )

        status = last_body.get('status')
        if not params.poll or status in TERMINAL_STATUSES:
            break
        if time.monotonic() - start >= params.poll_timeout_seconds:
            last_body['_poll_timed_out'] = True
            break
        await asyncio.sleep(params.poll_interval_seconds)

    last_body['_polls'] = polls

    def render_md(b: dict[str, Any]) -> str:
        lines = [
            f'# Registration `{b.get("registration_number")}`',
            '',
            f'- **Status**: `{b.get("status")}`',
            f'- **Created**: {b.get("created_at") or "unknown"}',
        ]
        if b.get('completed_at'):
            lines.append(f'- **Completed**: {b["completed_at"]}')
        if b.get('deposit_hash'):
            lines.append(f'- **SHA-256**: `{b["deposit_hash"]}`')
        if b.get('deposit_size'):
            lines.append(f'- **Size**: {b["deposit_size"]} bytes')
        if b.get('certificate_url'):
            lines.append(f'- **Certificate**: {b["certificate_url"]}')
        if b.get('nft_transaction_hash'):
            lines.append(
                f'- **NFT tx**: `{b["nft_transaction_hash"]}` (token id {b.get("nft_token_id")})'
            )
        summary = b.get('examination_summary')
        if summary:
            lines += [
                '',
                '## AI examination',
                f'- Copyrightability: {summary.get("copyrightability")}',
                f'- AI-generated flag: {summary.get("ai_flag")}',
            ]
        if b.get('_poll_timed_out'):
            lines += ['', '_Polling timed out before reaching a terminal status._']
        return '\n'.join(lines)

    return _format_response(last_body, params.response_format, render_md)


# --- File reference resolver (private) ----------------------------------


class _UserError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def _resolve_file_reference(
    params: RegisterInput, client: AgentPayClient
) -> tuple[dict[str, Any], Optional[str]]:
    '''Turn the user's file_* params into the {transport: ...} body the API wants.

    Returns (file_ref_dict, computed_sha256_or_None).
    '''
    supplied = sum(
        1 for v in (params.file_path, params.file_base64, params.upload_token) if v
    )
    if supplied != 1:
        raise _UserError(
            'validation_error',
            'Exactly one of file_path, file_base64, or upload_token must be provided.',
        )

    if params.upload_token:
        return {'transport': 'upload_token', 'upload_token': params.upload_token}, None

    if params.file_base64:
        if not params.file_name or not params.file_content_type:
            raise _UserError(
                'validation_error',
                'file_base64 requires both file_name and file_content_type.',
            )
        # Hash for traceability.
        try:
            raw = base64.b64decode(params.file_base64, validate=True)
        except Exception:
            raise _UserError('validation_error', 'file_base64 is not valid base64.')
        if len(raw) > INLINE_LIMIT_BYTES:
            raise _UserError(
                'file_too_large',
                f'file_base64 exceeds {INLINE_LIMIT_BYTES} bytes. Use file_path so the MCP '
                'can switch to upload-token transport, or call get_upload_url yourself.',
            )
        sha = hashlib.sha256(raw).hexdigest()
        return (
            {
                'transport': 'inline',
                'filename': params.file_name,
                'content_type': params.file_content_type,
                'base64': params.file_base64,
            },
            sha,
        )

    # file_path branch.
    try:
        filename, content_type, b64, size, sha = _read_file_for_inline(params.file_path)  # type: ignore[arg-type]
    except FileNotFoundError as exc:
        raise _UserError('file_not_found', str(exc))

    if size <= INLINE_LIMIT_BYTES:
        # Honor user-supplied overrides for filename / content_type if they want them.
        return (
            {
                'transport': 'inline',
                'filename': params.file_name or filename,
                'content_type': params.file_content_type or content_type,
                'base64': b64,
            },
            sha,
        )

    # File is too big to inline — get a signed upload URL and stream it.
    upload_resp = await client.request(
        'POST',
        '/uploadurl/',
        json_body={
            'filename': params.file_name or filename,
            'content_type': params.file_content_type or content_type,
        },
    )
    await _upload_to_signed_url(
        upload_url=upload_resp['upload_url'],
        upload_fields=upload_resp['upload_fields'],
        file_path=params.file_path,  # type: ignore[arg-type]
        content_type=params.file_content_type or content_type,
    )
    return {'transport': 'upload_token', 'upload_token': upload_resp['upload_token']}, sha


# --- Entrypoint ---------------------------------------------------------


def main() -> None:
    '''Launch the server over stdio. Override transport via env if needed later.'''
    transport = os.environ.get('TRANSACTABLE_MCP_TRANSPORT', 'stdio').lower()
    if transport == 'streamable_http':
        port = int(os.environ.get('TRANSACTABLE_MCP_PORT', '8765'))
        mcp.run(transport='streamable_http', port=port)
    else:
        mcp.run()


if __name__ == '__main__':
    main()
