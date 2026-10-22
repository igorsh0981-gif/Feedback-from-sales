from types import SimpleNamespace

from app.pipeline.classifier import ClaudeClassifier, EpisodeInput, EpisodeMessage, extract_analysis

GOOD = {"kind": "issue", "type": "Баг", "section": "Переводы", "priority": "Высокий", "title": "Перевод завис",
        "relation": "none", "confidence": 0.9, "reason": "Сбой"}


def ep():
    return EpisodeInput(episode_id="E-1", chat_title="Чат", chat_platform="", messages=[
        EpisodeMessage(author="Участник 1", sent_at="2026-10-22T14:00:00+05:00", text="Перевод завис")])


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=self.responses.pop(0))


def make(responses):
    c = ClaudeClassifier.__new__(ClaudeClassifier)
    c.model, c.max_images = "m", 4
    c.client = SimpleNamespace(messages=FakeMessages(responses))
    return c


async def test_tool_use_with_auto_choice():
    c = make([[SimpleNamespace(type="tool_use", name="submit_analysis", input=GOOD)]])
    a = await c.classify(ep())
    assert a.kind == "issue" and a.priority == "Высокий"
    assert c.client.messages.calls[0]["tool_choice"] == {"type": "auto"}


async def test_json_in_text_fallback():
    c = make([[SimpleNamespace(type="text", text='Вот разбор: {"kind": "noise", "confidence": 0.9, "reason": "привет"}')]])
    a = await c.classify(ep())
    assert a.kind == "noise"


async def test_retry_when_no_tool_and_no_json():
    c = make([[SimpleNamespace(type="text", text="Думаю...")],
              [SimpleNamespace(type="tool_use", name="submit_analysis", input=GOOD)]])
    a = await c.classify(ep())
    assert a.title == "Перевод завис" and len(c.client.messages.calls) == 2


def test_extract_ignores_thinking_blocks():
    blocks = [SimpleNamespace(type="thinking", thinking="{...}"),
              SimpleNamespace(type="tool_use", name="submit_analysis", input=GOOD)]
    assert extract_analysis(blocks)["kind"] == "issue"
