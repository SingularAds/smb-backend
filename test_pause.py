import asyncio
import os
import json
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv(".env")

from app.services.customer_ai_service import CUSTOMER_TOOLS, _build_system_prompt

async def test_auto_pause():
    business = {
        "name": "SHERA CHAT",
        "businessType": "restaurant",
        "businessPhone": "919905252720"
    }
    
    system_prompt = _build_system_prompt(business)
    
    # Let's test the frustrated user edge case
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "What the f*** is this"}
    ]
    
    client = AsyncOpenAI()
    
    print("Testing angry message: 'What the f*** is this'")
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        tools=[{"type": "function", "function": t} for t in CUSTOMER_TOOLS],
        tool_choice="auto",
        temperature=0.0
    )
    
    msg = response.choices[0].message
    if msg.tool_calls:
        print(f"PASS: AI outputted tool calls: {[t.function.name for t in msg.tool_calls]}")
    else:
        print(f"FAIL: AI replied directly: {msg.content}")

    print("\n----------------\n")
    
    # Let's test the sensitive complaint edge case
    messages2 = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Hey why is my food smelling bad, I bought it 2 hours ago from your restaurant"}
    ]
    
    print("Testing complaint: 'Hey why is my food smelling bad...'")
    response2 = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages2,
        tools=[{"type": "function", "function": t} for t in CUSTOMER_TOOLS],
        tool_choice="auto",
        temperature=0.0
    )
    
    msg2 = response2.choices[0].message
    if msg2.tool_calls:
        print(f"PASS: AI outputted tool calls: {[t.function.name for t in msg2.tool_calls]}")
    else:
        print(f"FAIL: AI replied directly: {msg2.content}")

if __name__ == "__main__":
    asyncio.run(test_auto_pause())
