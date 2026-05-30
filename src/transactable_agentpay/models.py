'''
Pydantic input models for the four MCP tools.

Schemas mirror agentpay/serializers.py in the cw-backend. Any change there must
be reflected here (and ideally caught by an integration test against a staging
AgentPay project).
'''
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResponseFormat(str, Enum):
    '''Output format for tool responses.'''
    MARKDOWN = 'markdown'
    JSON = 'json'


# --- /register inputs ----------------------------------------------------


class AuthorInput(BaseModel):
    '''One author of the work being registered.'''
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    name: str = Field(
        ...,
        description="Full name of the author (e.g., 'Ada Lovelace'). For AI-generated work, "
                    "use the operator's name or the model identifier.",
        min_length=1,
        max_length=200,
    )
    email: Optional[str] = Field(
        default=None,
        description='Author email. Optional, used only for record-keeping.',
        max_length=254,
    )
    country: str = Field(
        default='US',
        description="ISO-3166 alpha-2 country code (e.g., 'US', 'GB'). Defaults to 'US'.",
        min_length=2,
        max_length=2,
    )
    year_born: Optional[int] = Field(
        default=None,
        description='Birth year. Optional. Omit for non-human authors.',
        ge=1800,
        le=2100,
    )


class ClaimantInput(BaseModel):
    '''Party that holds rights in the work. Defaults to first author if omitted.'''
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    name: str = Field(..., min_length=1, max_length=200)
    email: Optional[str] = Field(default=None, max_length=254)
    country: str = Field(default='US', min_length=2, max_length=2)


class RegisterInput(BaseModel):
    '''Input for transactable_register_copyright.'''
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    title: str = Field(
        ...,
        description="Title of the work (e.g., 'Untitled image, May 2026'). Max 200 chars.",
        min_length=1,
        max_length=200,
    )
    authors: list[AuthorInput] = Field(
        ...,
        description='At least one author. For AI-generated work, declare the model operator '
                    'as the author with the model identifier in agent_name.',
        min_length=1,
    )
    claimants: Optional[list[ClaimantInput]] = Field(
        default=None,
        description='Rights holders. Defaults to the first author if omitted.',
    )
    wallet_address: Optional[str] = Field(
        default=None,
        description='Self-declared EVM or Solana wallet address that owns this registration. '
                    'If omitted, the wallet derived from the configured private key is used.',
        max_length=64,
    )
    agent_name: Optional[str] = Field(
        default=None,
        description='Optional human-readable identifier for the agent making the call. '
                    "Falls back to the TRANSACTABLE_AGENT_NAME env var.",
        max_length=255,
    )
    # File reference — exactly one of file_path / file_base64 / upload_token must be set.
    file_path: Optional[str] = Field(
        default=None,
        description="Absolute path to a local file. The MCP will read, hash, and either inline "
                    "(<8 MB) or upload to a signed URL (>=8 MB) automatically.",
    )
    file_base64: Optional[str] = Field(
        default=None,
        description='Base64-encoded file payload (use only if you cannot supply file_path). '
                    'Must be <=8 MB decoded. Requires file_name and file_content_type.',
    )
    file_name: Optional[str] = Field(
        default=None,
        description="Filename including extension (e.g., 'output.png'). Required when "
                    "file_base64 is used; auto-derived from file_path otherwise.",
        max_length=255,
    )
    file_content_type: Optional[str] = Field(
        default=None,
        description="MIME type (e.g., 'image/png'). Required when file_base64 is used; "
                    "auto-derived from file extension otherwise.",
        max_length=128,
    )
    upload_token: Optional[str] = Field(
        default=None,
        description='Token returned by transactable_get_upload_url after you uploaded the file '
                    'to GCS yourself. Use this path for very large files you already have in cloud storage.',
        max_length=64,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @field_validator('wallet_address')
    @classmethod
    def _strip_addr(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


# --- /lookup inputs ------------------------------------------------------


class LookupInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    registration_number: str = Field(
        ...,
        description='UUID returned by transactable_register_copyright.',
        min_length=36,
        max_length=36,
    )
    poll: bool = Field(
        default=False,
        description='If true, poll until status is terminal (completed / completed_no_nft / failed) '
                    'or poll_timeout_seconds elapses. If false, return the current snapshot once.',
    )
    poll_interval_seconds: float = Field(
        default=10.0,
        description='Seconds to wait between lookups when poll=true. Min 2.',
        ge=2.0,
        le=60.0,
    )
    poll_timeout_seconds: float = Field(
        default=180.0,
        description='Give up polling after this many seconds.',
        ge=10.0,
        le=600.0,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


# --- /verify-hash inputs -------------------------------------------------


class VerifyHashInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    sha256: Optional[str] = Field(
        default=None,
        description='SHA-256 hash of the file, 64 hex chars. Either sha256 or file_path is required.',
        min_length=64,
        max_length=64,
    )
    file_path: Optional[str] = Field(
        default=None,
        description='Absolute path to a local file. The MCP will hash it for you.',
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @field_validator('sha256')
    @classmethod
    def _normalize_sha(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.lower().strip()
        if v and not all(c in '0123456789abcdef' for c in v):
            raise ValueError('sha256 must be 64 hex characters')
        return v or None


# --- /upload-url inputs --------------------------------------------------


class UploadUrlInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

    filename: str = Field(
        ...,
        description="Filename you intend to upload (e.g., 'master.wav'). Must include an extension.",
        min_length=3,
        max_length=255,
    )
    content_type: str = Field(
        ...,
        description="MIME type for the file (e.g., 'audio/wav', 'video/mp4').",
        min_length=3,
        max_length=128,
    )

    @field_validator('filename')
    @classmethod
    def _validate_filename(cls, v: str) -> str:
        v = v.strip()
        if '.' not in v:
            raise ValueError('filename must include an extension')
        if '/' in v or '\\' in v:
            raise ValueError('filename must not contain path separators')
        return v
