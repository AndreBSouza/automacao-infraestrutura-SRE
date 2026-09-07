"""Chat endpoints (SPEC.md sections 2, 6) — conversations + streamed
message responses via the LLM orchestrator.
"""
from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sai.api.deps import get_approval_engine, get_registry
from sai.api.routers.auth import get_current_user
from sai.approval.engine import ApprovalEngine
from sai.config import Settings, get_settings
from sai.connectors.registry import ToolRegistry
from sai.db.models import Conversation, Message, User
from sai.db.session import get_db_session
from sai.llm.orchestrator import Orchestrator
from sai.rag.retrieve import retrieve_context

router = APIRouter(prefix="/conversations", tags=["chat"])


class CreateConversationRequest(BaseModel):
    title: str | None = None


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str | None

    class Config:
        from_attributes = True


class SendMessageRequest(BaseModel):
    content: str
    # Escalonamento pontual para o modelo mais capaz
    # (ANTHROPIC_ESCALATION_MODEL), quando a resposta do modelo padrão não
    # convence. Vale só para esta mensagem — a conversa volta ao padrão na
    # próxima, para que o custo extra nunca fique ligado por esquecimento.
    escalate: bool = False


@router.post("", response_model=ConversationOut)
async def create_conversation(
    body: CreateConversationRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Conversation:
    conversation = Conversation(user_id=user.id, title=body.title)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation


@router.get("")
async def list_conversations(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ConversationOut]:
    result = await session.execute(select(Conversation).where(Conversation.user_id == user.id).order_by(Conversation.created_at.desc()))
    return list(result.scalars().all())


@router.get("/{conversation_id}/messages")
async def get_history(
    conversation_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[dict]:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")

    result = await session.execute(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at))
    return [{"id": str(m.id), "role": m.role, "content": m.content, "created_at": m.created_at.isoformat()} for m in result.scalars().all()]


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: SendMessageRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    registry: Annotated[ToolRegistry, Depends(get_registry)],
    approval_engine: Annotated[ApprovalEngine, Depends(get_approval_engine)],
) -> StreamingResponse:
    """Streams the orchestrator's response as newline-delimited JSON events
    (SPEC 11: chat latency targets favor incremental delivery over a single
    blocking response). Persists both the user message and the final
    assistant message to `messages` once the turn completes.
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found")

    session.add(Message(conversation_id=conversation_id, role="user", content={"text": body.content}))
    await session.commit()

    history_result = await session.execute(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at))
    anthropic_messages: list[dict] = []
    for m in history_result.scalars().all():
        if m.role == "user" and "text" in m.content:
            anthropic_messages.append({"role": "user", "content": m.content["text"]})
        elif m.role == "assistant" and "blocks" in m.content:
            anthropic_messages.append({"role": "assistant", "content": m.content["blocks"]})
    # Drop the just-persisted user message from history since run_turn appends it itself.
    if anthropic_messages and anthropic_messages[-1]["role"] == "user":
        anthropic_messages.pop()

    # Escalonamento é por mensagem: quem pediu recebe o modelo mais capaz
    # apenas nesta resposta. Note que trocar de modelo troca de cache — este
    # turno paga o prompt cheio de novo. É o preço consciente de escalar.
    model = settings.anthropic_escalation_model if body.escalate else settings.anthropic_model
    orchestrator = Orchestrator(
        settings=settings,
        registry=registry,
        approval_engine=approval_engine,
        retrieve_context_fn=retrieve_context,
        model=model,
    )

    async def event_stream():
        final_text = ""
        # Diz de saída qual modelo está respondendo, para a UI poder marcar a
        # resposta — sem isso não há como saber depois se um diagnóstico veio
        # do modelo padrão ou do escalonado.
        yield json.dumps({"type": "model", "model": model, "escalated": body.escalate}) + "\n"
        async for event in orchestrator.run_turn(
            anthropic_messages, body.content, actor=f"user:{user.id}", conversation_id=str(conversation_id)
        ):
            if event["type"] == "final_text":
                final_text = event["text"]
            yield json.dumps(event) + "\n"

        # Persist the assistant's final content blocks (last assistant message in the updated list).
        assistant_blocks = next(
            (m["content"] for m in reversed(anthropic_messages) if m["role"] == "assistant"), None
        )
        async with session.begin():
            session.add(
                Message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content={
                        "text": final_text,
                        "blocks": assistant_blocks,
                        # Registrado no histórico: ao reler um diagnóstico
                        # antigo, é preciso saber se ele veio do modelo padrão
                        # ou do escalonado para julgar quanto peso dar a ele.
                        "model": model,
                        "escalated": body.escalate,
                    },
                )
            )

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")
