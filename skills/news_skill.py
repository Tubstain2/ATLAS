"""News skill — DuckDuckGo (no API key required)."""


def skill_info():
    return {
        "name": "news",
        "triggers": ["atlas news", "latest news", "what is in the news",
                     "what's happening", "top headlines", "news today",
                     "what happened today", "current events"],
        "description": "Fetch top headlines via DuckDuckGo news search",
    }


def execute(query: str, context: dict) -> str:
    config = context.get("config", {})
    topics = config.get("news_topics", ["technology", "world"])

    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for topic in topics[:2]:
                for r in ddgs.news(topic, max_results=2):
                    results.append(r)

        if not results:
            return "I couldn't find any headlines right now, Boss."

        headlines = []
        for r in results[:3]:
            title = r.get("title", "")
            body  = r.get("body", "")
            if title:
                summary = f"{title}. {body[:80]}..." if body else title
                headlines.append(summary)

        return "Here are the top headlines. " + " Next: ".join(headlines[:3])

    except ImportError:
        return "The duckduckgo-search package is not installed."
    except Exception as exc:
        return f"I couldn't fetch the news: {exc}"
