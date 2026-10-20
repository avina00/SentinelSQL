from contextlib import asynccontextmanager
from typing import Optional, Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from app.config import settings
from app.schema import seed_database, get_live_schema
from app.graph import build_sentinel_graph

@asynccontextmanager
async def lifespan(app: FastAPI):
    # autocommit=True and dict_row are required by AsyncPostgresSaver
    conn_kwargs = {"autocommit": True, "row_factory": dict_row}
    async with AsyncConnectionPool(
        conninfo=settings.postgres_uri,
        max_size=20,
        kwargs=conn_kwargs,
    ) as pool:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        await seed_database(pool)

        app.state.pool = pool
        app.state.agent = build_sentinel_graph(pool, checkpointer)
        yield

app = FastAPI(title="SentinelSQL Core Engine", version="1.0.0", lifespan=lifespan)

class QueryRequest(BaseModel):
    thread_id: str
    question: str

class ApprovalRequest(BaseModel):
    thread_id: str
    action: Literal["approve", "edit", "reject"]
    modified_sql: Optional[str] = None

async def serialize_graph_output(agent, config: dict, result: dict) -> dict:
    snapshot = await agent.aget_state(config)
    if snapshot.next:
        interrupt_payload = None
        for task in snapshot.tasks:
            if task.interrupts:
                interrupt_payload = task.interrupts[0].value
                break
        return {
            "status": "awaiting_human_approval",
            "thread_id": config["configurable"]["thread_id"],
            "next_node": list(snapshot.next),
            "interrupt_details": interrupt_payload,
        }

    return {
        "status": "completed",
        "thread_id": config["configurable"]["thread_id"],
        "sql_executed": result.get("sql_query"),
        "reasoning": result.get("reasoning"),
        "retries_used": result.get("retries"),
        "answer": result.get("final_answer"),
    }

@app.post("/api/v1/query")
async def execute_query(req: QueryRequest):
    agent = app.state.agent
    config = {"configurable": {"thread_id": req.thread_id}}
    initial_state = {
        "question": req.question,
        "retries": 0,
        "error": None,
        "final_answer": None,
    }
    result = await agent.ainvoke(initial_state, config=config)
    return await serialize_graph_output(agent, config, result)

@app.post("/api/v1/approve")
async def resolve_interrupt(req: ApprovalRequest):
    agent = app.state.agent
    config = {"configurable": {"thread_id": req.thread_id}}

    snapshot = await agent.aget_state(config)
    if not snapshot.next:
        raise HTTPException(status_code=400, detail="No paused execution found for this thread_id.")

    resume_payload = {"action": req.action, "modified_sql": req.modified_sql}
    result = await agent.ainvoke(Command(resume=resume_payload), config=config)
    return await serialize_graph_output(agent, config, result)

@app.get("/api/v1/schema")
async def fetch_schema():
    schema_str = await get_live_schema(app.state.pool)
    return {"schema": schema_str}

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "SentinelSQL"}