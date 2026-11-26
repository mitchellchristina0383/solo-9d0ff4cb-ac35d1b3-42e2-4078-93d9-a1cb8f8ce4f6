"""FastAPI application for shared Kubernetes Deployment claim rehearsals."""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Path, Query
from fastapi.responses import JSONResponse, RedirectResponse

from .db import init_db
from .ownership import OwnershipConflictError
from .schemas import ClaimRequest, ClaimResponse, ConflictResponse, StateResponse
from .service import (
    conflict_payload,
    get_state,
    reset_state,
    submit_claim,
)

DB_PATH = os.getenv("DB_PATH", "rehearsal.db")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db(DB_PATH)
    yield


app = FastAPI(
    title="Shared Deployment Release Rehearsal",
    description=(
        "接收管理方声明，只协调 `replicas` 与 `images.<容器名>`。\n\n"
        "- 同值声明自动共管；\n"
        "- 新声明省略本方管理的字段即退出，最后一方退出才删除字段；\n"
        "- 非接管改他方管理的值会返回 409，并列出冲突字段和管理方，整份不生效；\n"
        "- `takeover=true` 显式接管后，冲突字段只归提交方；\n"
        "- SQLite `BEGIN IMMEDIATE` 事务按最新已生效状态判断冲突，并一起提交配置与归属。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_db() -> sqlite3.Connection:
    # The generator dependency is provided explicitly so DB_PATH is read at
    # request time/import time consistently for local and Docker runs.
    from .db import connection_dependency

    yield from connection_dependency(DB_PATH)


Database = Annotated[sqlite3.Connection, Depends(get_db)]
DeploymentPath = Annotated[
    str,
    Path(
        ...,
        min_length=1,
        max_length=64,
        description="演练 Deployment 标识",
    ),
]
DeploymentQuery = Annotated[
    str,
    Query(
        min_length=1,
        max_length=64,
        description="演练 Deployment 标识",
    ),
]


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


def _submit(
    conn: sqlite3.Connection,
    deployment: str,
    payload: ClaimRequest,
):
    try:
        return submit_claim(
            conn,
            deployment=deployment,
            manager=payload.manager,
            declared_values=payload.declared_values(),
            takeover=payload.takeover,
        )
    except OwnershipConflictError as error:
        return JSONResponse(
            status_code=409,
            content=conflict_payload(error, deployment),
        )


@app.post(
    "/claims",
    response_model=ClaimResponse,
    responses={409: {"model": ConflictResponse}},
    tags=["claims"],
    summary="提交管理方声明（请求体可指定 deployment）",
)
@app.post(
    "/claim",
    response_model=ClaimResponse,
    responses={409: {"model": ConflictResponse}},
    include_in_schema=False,
)
@app.post(
    "/api/claims",
    response_model=ClaimResponse,
    responses={409: {"model": ConflictResponse}},
    include_in_schema=False,
)
@app.post(
    "/api/v1/claims",
    response_model=ClaimResponse,
    responses={409: {"model": ConflictResponse}},
    include_in_schema=False,
)
def submit_claim_default(payload: ClaimRequest, conn: Database):
    return _submit(conn, payload.deployment, payload)


@app.post(
    "/deployments/{deployment}/claims",
    response_model=ClaimResponse,
    responses={409: {"model": ConflictResponse}},
    tags=["claims"],
    summary="向指定 Deployment 提交声明",
)
@app.post(
    "/api/v1/deployments/{deployment}/claims",
    response_model=ClaimResponse,
    responses={409: {"model": ConflictResponse}},
    include_in_schema=False,
)
def submit_claim_for_deployment(
    deployment: DeploymentPath,
    payload: ClaimRequest,
    conn: Database,
):
    return _submit(conn, deployment, payload)


@app.get(
    "/state",
    response_model=StateResponse,
    tags=["state"],
    summary="查看合并配置和归属",
)
@app.get("/api/state", response_model=StateResponse, include_in_schema=False)
@app.get("/api/v1/state", response_model=StateResponse, include_in_schema=False)
def read_state(conn: Database, deployment: DeploymentQuery = "default"):
    return get_state(conn, deployment)


@app.get(
    "/deployments/{deployment}/state",
    response_model=StateResponse,
    tags=["state"],
    summary="按路径查看合并配置和归属",
)
@app.get(
    "/api/v1/deployments/{deployment}/state",
    response_model=StateResponse,
    include_in_schema=False,
)
def read_deployment_state(deployment: DeploymentPath, conn: Database):
    return get_state(conn, deployment)


@app.delete(
    "/state",
    tags=["state"],
    summary="清空某个演练 Deployment（不接触真实 Kubernetes）",
)
def delete_state(conn: Database, deployment: DeploymentQuery = "default"):
    return reset_state(conn, deployment)


@app.delete(
    "/deployments/{deployment}",
    tags=["state"],
    summary="按路径清空演练 Deployment",
)
def delete_deployment_state(deployment: DeploymentPath, conn: Database):
    return reset_state(conn, deployment)
