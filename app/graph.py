import re
from typing import TypedDict, Optional
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from psycopg_pool import AsyncConnectionPool
from app.config import settings
from app.schema import get_live_schema

# 1. Structured Output Schema for deterministic SQL generation
class GeneratedSQL(BaseModel):
    sql_query: str = Field(description="Executable PostgreSQL query without markdown code fences.")
    is_mutating: bool = Field(
        description="True if the query modifies data or schema (INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE)."
    )
    reasoning: str = Field(description="Brief one-line explanation of how the query answers the user request.")

llm = ChatOpenAI(base_url="https://api.groq.com/openai/v1",model = settings.llm_model,temperature=0, api_key=settings.openai_api_key)
sql_generator_llm = llm.with_structured_output(GeneratedSQL)

# 2. Deterministic Regex Guardrail (Defense-in-Depth)
MUTATION_REGEX = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CREATE)\b",
    re.IGNORECASE,
)

# 3. Graph State Definition
class SentinalState(TypedDict):
    question: str
    sql_query: Optional[str]
    reasoning: Optional[str]
    is_mutating: Optional[bool]
    db_result: Optional[str]
    error: Optional[str]
    retries: int
    final_answer: Optional[str]

def build_sentinel_graph(db_pool: AsyncConnectionPool, checkpointer):
    """Builds and compiles the stateful LangGraph workflow."""
    async def generate_sql(state: SentinalState) -> dict:
        live_schema = await get_live_schema(db_pool)
        system_prompt = (
            "You are SentinelSQL, a senior PostgreSQL engineer.\n"
            f"Live Database Schema:\n{live_schema}\n\n"
            "Rules:\n"
            "1. Write valid PostgreSQL syntax.\n"
            "2. Never query internal checkpoint tables.\n"
            "3. Return ONLY the structured output."
        )
        user_prompt = f"User Question: {state['question']}"
        if state.get("error"):
            user_prompt += (
                f"\n\n[SELF-CORRECTION LOOP - Attempt {state.get('retries', 0) + 1}]\n"
                f"Your previous SQL failed:\n{state['sql_query']}\n"
                f"PostgreSQL Error:\n{state['error']}\n"
                "Analyze the error against the live schema and fix the query."
            )
        response: GeneratedSQL = await sql_generator_llm.ainvoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
        ])
        clean_sql = response.sql_query.strip().rstrip(";") + ";"
        regex_flagged = bool(MUTATION_REGEX.search(clean_sql))
        return {
            "sql_query": clean_sql,
            "reasoning": response.reasoning,
            "is_mutating": response.is_mutating or regex_flagged,
            "retries": state.get("retries", 0) + 1,
        }

    async def human_approval_gate(state: SentinalState) -> dict:
        """Pauses execution using LangGraph interrupt() if the SQL mutates state."""
        if not state.get("is_mutating"):
            return {}

        # Freeze execution & persist checkpoint to PostgreSQL
        human_decision = interrupt({
            "alert": "Destructive or State-Mutating SQL Intercepted by SentinelSQL",
            "proposed_sql": state["sql_query"],
            "reasoning": state.get("reasoning"),
            "allowed_actions": ["approve", "edit", "reject"],
        })

        action = human_decision.get("action", "reject").lower()
        if action == "approve":
            return {"error": None}
        elif action == "edit" and human_decision.get("modified_sql"):
            return {"sql_query": human_decision["modified_sql"].strip(), "error": None}
        else:
            return {
                "error": "Query rejected by administrator.",
                "final_answer": "🛡️ **SentinelSQL Guardrail:** Execution was cancelled by the administrator.",
            }

    async def execute_sql(state: SentinalState) -> dict:
        """Executes the validated SQL query against PostgreSQL."""
        if state.get("final_answer"):
            return {}

        query = state["sql_query"]
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query)
                    if cur.description:
                        rows = await cur.fetchall()
                        return {"db_result": str(rows), "error": None}
                    return {
                        "db_result": f"Statement executed successfully. Rows affected: {cur.rowcount}",
                        "error": None,
                    }
        except Exception as exc:
            return {"error": str(exc), "db_result": None}

    async def summarize_answer(state: SentinalState) -> dict:
        if state.get("final_answer"):
            return {}

        if state.get("error"):
            return {
                "final_answer": (
                    f"❌ SentinelSQL could not resolve the query after {state['retries']} attempts.\n"
                    f"**Last Database Error:** `{state['error']}`"
                )
            }

        prompt = (
            f"User Question: {state['question']}\n"
            f"Executed SQL: {state['sql_query']}\n"
            f"Query Result: {state['db_result']}\n\n"
            "Format a concise, helpful response. Format all currency values in Indian Rupees (₹)."
        )
        res = await llm.ainvoke(prompt)
        return {"final_answer": res.content}

    def should_retry(state: SentinalState) -> str:
        if state.get("final_answer"):
            return "summarize_answer"
        if state.get("error") and state.get("retries", 0) < settings.max_retries:
            return "generate_sql"
        return "summarize_answer"

    # Build StateGraph
    builder = StateGraph(SentinalState)
    builder.add_node("generate_sql", generate_sql)
    builder.add_node("human_approval_gate", human_approval_gate)
    builder.add_node("execute_sql", execute_sql)
    builder.add_node("summarize_answer", summarize_answer)
    builder.add_edge(START, "generate_sql")
    builder.add_edge("generate_sql", "human_approval_gate")
    builder.add_edge("human_approval_gate", "execute_sql")
    builder.add_conditional_edges(
        "execute_sql",
        should_retry,
        {
            "generate_sql": "generate_sql",
            "summarize_answer": "summarize_answer",
        },
    )
    builder.add_edge("summarize_answer", END)
    return builder.compile(checkpointer=checkpointer)
