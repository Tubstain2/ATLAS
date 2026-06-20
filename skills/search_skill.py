"""Search skill — DuckDuckGo web search (no API key required)."""


def skill_info():
    return {
        "name": "search",
        "triggers": ["atlas search for", "atlas look up", "atlas find out",
                     "atlas google", "atlas web search", "search the web for",
                     "atlas search", "look up"],
        "description": "Web search via DuckDuckGo",
    }


def execute(query: str, context: dict) -> str:
    lower = query.lower().strip()

    # Extract search terms
    search_term = query
    for trigger in ("atlas search for", "atlas look up", "atlas find out about",
                    "atlas google", "atlas web search", "search the web for",
                    "atlas search", "look up"):
        if trigger in lower:
            search_term = query[lower.index(trigger) + len(trigger):].strip()
            break

    if not search_term:
        return "What would you like me to search for, Boss?"

    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(search_term, max_results=3):
                results.append(r)

        if not results:
            return f"I couldn't find anything for {search_term}."

        # Try brain for synthesis if available
        brain = context.get("brain")
        if brain:
            snippets = "\n".join(
                f"- {r.get('title', '')}: {r.get('body', '')[:150]}"
                for r in results
            )
            prompt = (
                f"Answer this query based on these search results. "
                f"Be concise — 2 sentences max.\n"
                f"Query: {search_term}\n\nResults:\n{snippets}"
            )
            try:
                return brain.ask(prompt)
            except Exception:
                pass

        # Fallback: return top result
        top = results[0]
        return f"{top.get('title', 'Result')}: {top.get('body', '')[:200]}"

    except ImportError:
        return "The duckduckgo-search package is not installed."
    except Exception as exc:
        return f"Search failed: {exc}"
