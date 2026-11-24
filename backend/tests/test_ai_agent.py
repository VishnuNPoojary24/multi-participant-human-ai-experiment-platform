import asyncio
from types import SimpleNamespace

from app.ai_agent import GroqParticipant


def test_groq_reply_streams_tokens_with_neutral_identity(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    agent = GroqParticipant()
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)

        async def chunks():
            for content in ("I favor Player A", " because they listened carefully."):
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content))])

        return chunks()

    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    sent = []

    async def on_token(token):
        sent.append(token)

    result = asyncio.run(agent.respond("B", 1, [{"participant_id": "A", "content": "I value careful listening."}], ["A", "C"], {"A"}, on_token))

    assert result == "I favor Player A because they listened carefully."
    assert "Player B" in captured["messages"][0]["content"]
    assert "never reveal" in captured["messages"][0]["content"].lower()
    assert captured["model"] == "openai/gpt-oss-120b"
    assert captured["reasoning_effort"] == "medium"
    assert captured["stream"] is True
    assert sent == ["I favor Player A", " because they listened carefully."]


def test_opening_prompt_does_not_praise_or_invent_human_input(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    agent = GroqParticipant()
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)

        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello, I’m Player B. What matters most in a Decision Maker?"))])

        return chunks()

    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def ignore_token(_token):
        pass

    asyncio.run(agent.respond("B", 1, [], ["A", "C"], {"A"}, ignore_token))
    opening = captured["messages"][-1]["content"].lower()
    assert "no human participant has spoken yet" in opening
    assert "do not rate, praise" in opening
    assert "do not tell a participant that they are good" in captured["messages"][0]["content"].lower()
