"""Cloudflare R2 client (S3-compatible via boto3, wrapped for async use)."""
from __future__ import annotations

import asyncio
from functools import lru_cache

import boto3
from botocore.config import Config

from app.config import get_settings


class R2ObjectMissing(Exception):
    """The object does not exist in the bucket — a permanent failure."""


@lru_cache
def _client():
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=f"https://{s.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=s.r2_access_key_id,
        aws_secret_access_key=s.r2_secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


async def get_object(r2_key: str) -> bytes:
    def _get() -> bytes:
        client = _client()
        try:
            return client.get_object(
                Bucket=get_settings().r2_bucket_name, Key=r2_key
            )["Body"].read()
        except client.exceptions.NoSuchKey as exc:
            raise R2ObjectMissing(r2_key) from exc

    return await asyncio.to_thread(_get)


async def delete_object(r2_key: str) -> None:
    def _delete() -> None:
        _client().delete_object(Bucket=get_settings().r2_bucket_name, Key=r2_key)

    await asyncio.to_thread(_delete)
