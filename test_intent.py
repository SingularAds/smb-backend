import asyncio
import os
from dotenv import load_dotenv

# Ensure we load the env file for OPENAI_API_KEY
load_dotenv(".env")

from app.services.intent_classifier import classify_intent, Intent

async def test():
    business_name = "SHERA CHAT"
    business_type = "restaurant"
    
    # Simulate a conversation history where the user is a friend
    history = [
        {"role": "user", "content": "What are you doing"},
        {"role": "assistant", "content": "I am working at the restaurant, what's up?"},
    ]
    
    # The tricky message
    message = "Can we have dinner today bro"
    
    print(f"Testing message: '{message}'")
    result = await classify_intent(message, business_name, business_type, history)
    print(f"Result Intent: {result.intent}")
    print(f"Result Score: {result.score}")
    print(f"Result Reason: {result.reason}")

    print("\n----------------\n")
    
    # Another tricky message
    message2 = "Can we have dinner today for 2 people?"
    print(f"Testing message: '{message2}'")
    result2 = await classify_intent(message2, business_name, business_type, history)
    print(f"Result Intent: {result2.intent}")
    print(f"Result Score: {result2.score}")
    print(f"Result Reason: {result2.reason}")

if __name__ == "__main__":
    asyncio.run(test())
