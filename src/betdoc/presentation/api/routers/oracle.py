import asyncio
import os
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/oracle", tags=["oracle"])

class OracleChatRequest(BaseModel):
    message: str
    context_market_id: str | None = None

async def _stream_llm(prompt: str):
    api_key = os.getenv("OPENAI_API_KEY", "sk-fakekey")
    if api_key == "sk-fakekey":
        words = prompt.split(" ")
        for i, word in enumerate(words):
            yield word + (" " if i < len(words) - 1 else "")
            await asyncio.sleep(0.05)
        return
        
    try:
        from openai import AsyncOpenAI
        
        if api_key.startswith("AIza"):
            client = AsyncOpenAI(
                api_key=api_key,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
            )
            model_name = "gemini-1.5-pro"
        else:
            omniroute_url = os.getenv("OMNIROUTE_URL", "http://host.docker.internal:20128/v1")
            client = AsyncOpenAI(
                api_key="omniroute-local",
                base_url=omniroute_url
            )
            model_name = "auto" # OmniRoute automatically selects the best available free model

        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are the BetDoc Scout Oracle, a quantitative trading assistant. Keep answers brief and analytical."},
                {"role": "user", "content": prompt}
            ],
            stream=True
        )
        async for chunk in response:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    except Exception as e:
        yield f"\n[LLM Error: {str(e)}]"

@router.post("/chat")
async def oracle_chat(body: OracleChatRequest):
    edge_str = "2.45%"
    wallet_balance_str = "₹1,04,200.00"
    
    if body.context_market_id:
        text = f"User asked: '{body.message}'. Analyze market {body.context_market_id} with a live edge of {edge_str} and current wallet balance of {wallet_balance_str}."
    else:
        text = f"User asked: '{body.message}'. Current wallet is {wallet_balance_str}."

    return StreamingResponse(_stream_llm(text), media_type="text/plain")
