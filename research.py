"""
ATLAS Academic Research — parallel search across arXiv, Semantic Scholar, CrossRef.

Searches multiple sources simultaneously, deduplicates by title similarity,
ranks by citation count and relevance, then summarises with brain.ask().

Voice commands:
  "ATLAS research X"                     → full search + summary
  "ATLAS find papers on X"               → same
  "ATLAS what does the literature say about X" → same
  "ATLAS search for recent papers on X"  → filter last 3 years
  "ATLAS more papers on X"               → next page of results
  "ATLAS what is that paper about"       → detail last paper
  "ATLAS cite that paper"                → APA citation for last paper
  "ATLAS cite all papers"                → APA citations for last results
  "ATLAS save research on X"             → save to Obsidian vault
  "ATLAS summarise the abstracts"        → AI summary of last result set
  "ATLAS how many citations does that have" → citation count for last paper
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

_SUMMARY_PROMPT = """\
You are summarising academic literature on: {topic}

Abstracts from {n} papers:
{abstracts}

Write a 4-sentence synthesis covering:
1) What the consensus finding is
2) Key methods used
3) Remaining open questions
4) Most important practical implication

Plain prose, no markdown, no citations. For voice output."""

_DETAIL_PROMPT = """\
Explain this academic paper in plain language as if talking to an intelligent non-expert:

Title: {title}
Authors: {authors}
Abstract: {abstract}

3 sentences max. What problem it solves, what they found, why it matters."""


@dataclass
class Paper:
    title: str
    authors: List[str]
    year: Optional[int]
    abstract: str
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    semantic_id: Optional[str] = None
    citations: int = 0
    url: Optional[str] = None
    source: str = "unknown"
    venue: str = ""

    def apa(self) -> str:
        """Return a minimal APA-ish citation string."""
        authors_str = (
            ", ".join(f"{a}" for a in self.authors[:3])
            + (" et al." if len(self.authors) > 3 else "")
        )
        year_str = f"({self.year}). " if self.year else ""
        venue_str = f" {self.venue}." if self.venue else "."
        doi_str = f" https://doi.org/{self.doi}" if self.doi else ""
        return f"{authors_str} {year_str}{self.title}{venue_str}{doi_str}".strip()


class ATLASResearch:
    """Academic research aggregator with voice interface."""

    _ARXIV_BASE   = "https://export.arxiv.org/api/query"
    _S2_BASE      = "https://api.semanticscholar.org/graph/v1/paper/search"
    _CROSSREF_BASE = "https://api.crossref.org/works"
    _TIMEOUT      = 10

    def __init__(self, config: dict, speak_cb: Callable,
                 brain, vault_brain=None, smart_card_mgr=None):
        self._config         = config
        self._speak          = speak_cb
        self._brain          = brain
        self._vault_brain    = vault_brain
        self._smart_card_mgr = smart_card_mgr
        self._enabled        = config.get("research_enabled", True)
        self._max_results    = int(config.get("research_max_results", 10))
        self._last_results: List[Paper] = []
        self._last_paper: Optional[Paper] = None
        self._last_topic: str = ""

        log.info("ATLASResearch: ready (enabled=%s).", self._enabled)

    # ── Voice router ──────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        if not self._enabled:
            return None
        lower = text.lower().strip()
        lower_clean = re.sub(r"^atlas\s+", "", lower)

        # Research queries
        m = (re.search(r"research (.+?)$", lower_clean) or
             re.search(r"find papers on (.+?)$", lower_clean) or
             re.search(r"what does the literature say about (.+?)$", lower_clean) or
             re.search(r"search for (?:recent )?papers on (.+?)$", lower_clean))
        if m:
            topic = m.group(1).strip()
            recent = "recent" in lower_clean
            return self._start_search(topic, recent=recent)

        # More papers
        m = re.search(r"more papers on (.+?)$", lower_clean)
        if m:
            return self._start_search(m.group(1).strip(), page=2)

        # Detail / citation / save (need existing results)
        if any(p in lower_clean for p in ("what is that paper about",
                                           "explain that paper")):
            return self._detail_last_paper()

        if "cite that paper" in lower_clean and "all" not in lower_clean:
            return self._cite_last_paper()

        if "cite all papers" in lower_clean or "cite all" in lower_clean:
            return self._cite_all()

        if any(p in lower_clean for p in ("how many citations does that have",
                                           "citations for that paper")):
            return self._citations_count()

        if "summarise the abstracts" in lower_clean or "summarize the abstracts" in lower_clean:
            if self._last_results:
                return self._summarise_results(self._last_topic, self._last_results)

        if "save research" in lower_clean:
            m = re.search(r"save research on (.+?)$", lower_clean)
            topic = m.group(1).strip() if m else self._last_topic
            return self._manual_save(topic)

        return None

    # ── Search ────────────────────────────────────────────────────────────────

    def _start_search(self, topic: str, recent: bool = False, page: int = 1) -> str:
        self._last_topic = topic
        threading.Thread(
            target=self._search_thread, args=(topic, recent, page),
            daemon=True, name="atlas-research").start()
        return f"Searching the literature on {topic}, Boss. One moment."

    def _search_thread(self, topic: str, recent: bool, page: int) -> None:
        try:
            cutoff_year = (datetime.now().year - 3) if recent else None
            results = self._parallel_search(topic, page, cutoff_year)
            if not results:
                self._speak(f"I could not find papers on {topic}, Boss. "
                             "Try a different search term.")
                return
            self._last_results = results
            if results:
                self._last_paper = results[0]
            summary = self._summarise_results(topic, results)
            self._speak(summary)
            if self._smart_card_mgr:
                card_text = self._format_card_text(topic, results)
                try:
                    self._smart_card_mgr.on_response(f"research: {topic}", card_text)
                except Exception:
                    pass
            if self._config.get("research_auto_save_obsidian", False):
                self._save_to_vault(topic, results, summary)
        except Exception as exc:
            log.error("Research thread error: %s", exc)
            self._speak(f"Research search failed for {topic}, Boss.")

    def _parallel_search(self, topic: str, page: int,
                          cutoff_year: Optional[int]) -> List[Paper]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futures = {
                ex.submit(self._search_arxiv, topic, page): "arxiv",
                ex.submit(self._search_semantic_scholar, topic, page): "s2",
                ex.submit(self._search_crossref, topic, page): "crossref",
            }
            all_papers: List[Paper] = []
            for future in concurrent.futures.as_completed(futures, timeout=15):
                source = futures[future]
                try:
                    papers = future.result()
                    all_papers.extend(papers)
                except Exception as exc:
                    log.debug("Research: %s failed: %s", source, exc)

        if cutoff_year:
            all_papers = [p for p in all_papers
                          if p.year is None or p.year >= cutoff_year]

        deduped = self._deduplicate(all_papers)
        deduped.sort(key=lambda p: p.citations, reverse=True)
        return deduped[:self._max_results]

    # ── arXiv ─────────────────────────────────────────────────────────────────

    def _search_arxiv(self, topic: str, page: int = 1) -> List[Paper]:
        start = (page - 1) * 5
        params = urllib.parse.urlencode({
            "search_query": f"all:{topic}",
            "start": start,
            "max_results": 5,
            "sortBy": "relevance",
        })
        url = f"{self._ARXIV_BASE}?{params}"
        raw = self._fetch(url)
        if not raw:
            return []

        papers: List[Paper] = []
        ns = {"atom": "http://www.w3.org/2005/Atom",
               "arxiv": "http://arxiv.org/schemas/atom"}
        try:
            root = ET.fromstring(raw)
            for entry in root.findall("atom:entry", ns):
                title = (entry.findtext("atom:title", "", ns) or "").strip()
                abstract = (entry.findtext("atom:summary", "", ns) or "").strip()
                authors = [
                    a.findtext("atom:name", "", ns)
                    for a in entry.findall("atom:author", ns)
                ]
                published = entry.findtext("atom:published", "", ns)
                year = int(published[:4]) if published and len(published) >= 4 else None
                arxiv_id = ""
                link_el = entry.find("atom:id", ns)
                if link_el is not None and link_el.text:
                    arxiv_id = link_el.text.split("/abs/")[-1].strip()
                doi_el = entry.find("arxiv:doi", ns)
                doi = doi_el.text.strip() if doi_el is not None and doi_el.text else None

                if title:
                    papers.append(Paper(
                        title=title, authors=authors, year=year,
                        abstract=abstract, doi=doi, arxiv_id=arxiv_id,
                        url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None,
                        source="arxiv",
                    ))
        except ET.ParseError as exc:
            log.debug("arXiv XML parse error: %s", exc)
        return papers

    # ── Semantic Scholar ──────────────────────────────────────────────────────

    def _search_semantic_scholar(self, topic: str, page: int = 1) -> List[Paper]:
        offset = (page - 1) * 5
        params = urllib.parse.urlencode({
            "query": topic,
            "limit": 5,
            "offset": offset,
            "fields": "title,authors,year,abstract,citationCount,externalIds,venue",
        })
        url = f"{self._S2_BASE}?{params}"
        raw = self._fetch(url)
        if not raw:
            return []

        papers: List[Paper] = []
        try:
            data = json.loads(raw)
            for item in data.get("data", []):
                title = item.get("title", "") or ""
                abstract = item.get("abstract", "") or ""
                authors = [a.get("name", "") for a in item.get("authors", [])]
                year = item.get("year")
                citations = item.get("citationCount", 0) or 0
                ext = item.get("externalIds", {}) or {}
                doi = ext.get("DOI")
                arxiv_id = ext.get("ArXiv")
                venue = item.get("venue", "") or ""
                s2id = item.get("paperId", "")

                if title:
                    papers.append(Paper(
                        title=title, authors=authors, year=year,
                        abstract=abstract, doi=doi, arxiv_id=arxiv_id,
                        semantic_id=s2id, citations=citations, venue=venue,
                        url=(f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id
                             else f"https://semanticscholar.org/paper/{s2id}" if s2id
                             else None),
                        source="semantic_scholar",
                    ))
        except Exception as exc:
            log.debug("S2 parse error: %s", exc)
        return papers

    # ── CrossRef ──────────────────────────────────────────────────────────────

    def _search_crossref(self, topic: str, page: int = 1) -> List[Paper]:
        offset = (page - 1) * 5
        params = urllib.parse.urlencode({
            "query": topic,
            "rows": 5,
            "offset": offset,
            "select": "title,author,published,abstract,DOI,is-referenced-by-count,container-title",
        })
        url = f"{self._CROSSREF_BASE}?{params}"
        raw = self._fetch(url)
        if not raw:
            return []

        papers: List[Paper] = []
        try:
            data = json.loads(raw)
            for item in data.get("message", {}).get("items", []):
                titles = item.get("title", [])
                title = titles[0] if titles else ""
                abstract = item.get("abstract", "") or ""
                # Strip HTML tags from CrossRef abstracts
                abstract = re.sub(r"<[^>]+>", "", abstract).strip()
                auth_list = item.get("author", [])
                authors = [
                    f"{a.get('given', '')} {a.get('family', '')}".strip()
                    for a in auth_list
                ]
                pub = item.get("published", {}) or {}
                dp = pub.get("date-parts", [[None]])
                year = dp[0][0] if dp and dp[0] else None
                doi = item.get("DOI")
                citations = item.get("is-referenced-by-count", 0) or 0
                containers = item.get("container-title", [])
                venue = containers[0] if containers else ""

                if title:
                    papers.append(Paper(
                        title=title, authors=authors, year=year,
                        abstract=abstract, doi=doi, citations=citations, venue=venue,
                        url=f"https://doi.org/{doi}" if doi else None,
                        source="crossref",
                    ))
        except Exception as exc:
            log.debug("CrossRef parse error: %s", exc)
        return papers

    # ── Deduplication ─────────────────────────────────────────────────────────

    def _deduplicate(self, papers: List[Paper]) -> List[Paper]:
        seen: Dict[str, Paper] = {}
        for p in papers:
            key = self._title_key(p.title)
            if key in seen:
                # Prefer the one with more info
                existing = seen[key]
                if p.citations > existing.citations:
                    seen[key] = p
                elif not existing.doi and p.doi:
                    seen[key] = p
            else:
                seen[key] = p
        return list(seen.values())

    @staticmethod
    def _title_key(title: str) -> str:
        t = re.sub(r"[^\w\s]", "", title.lower())
        return " ".join(t.split()[:6])

    # ── Summarisation ─────────────────────────────────────────────────────────

    def _summarise_results(self, topic: str, papers: List[Paper]) -> str:
        if not papers:
            return f"No papers found on {topic}, Boss."
        abstracts_block = "\n---\n".join(
            f"Title: {p.title}\nAbstract: {p.abstract[:400]}"
            for p in papers[:6] if p.abstract
        )
        if not abstracts_block:
            # Fallback: just name the papers
            titles = "; ".join(p.title for p in papers[:5])
            return (f"Found {len(papers)} papers on {topic}, Boss. "
                    f"Top results: {titles}.")
        prompt = _SUMMARY_PROMPT.format(
            topic=topic, n=len(papers), abstracts=abstracts_block)
        try:
            summary = self._brain.ask(prompt)
            top = papers[0]
            return (f"Found {len(papers)} papers on {topic}, Boss. "
                    f"Most cited: '{top.title}' ({top.citations} citations). "
                    f"{summary}")
        except Exception as exc:
            log.error("Research: summary failed: %s", exc)
            titles = "; ".join(p.title for p in papers[:3])
            return f"Found {len(papers)} papers on {topic}, Boss. Top results: {titles}."

    # ── Detail and citation helpers ───────────────────────────────────────────

    def _detail_last_paper(self) -> str:
        if not self._last_paper:
            return "No paper in context, Boss. Search for something first."
        p = self._last_paper
        prompt = _DETAIL_PROMPT.format(
            title=p.title,
            authors=", ".join(p.authors[:3]) or "Unknown",
            abstract=p.abstract[:800] or "Abstract not available.",
        )
        try:
            return self._brain.ask(prompt)
        except Exception:
            return f"'{p.title}' — {p.abstract[:200]}" if p.abstract else f"Paper: {p.title}"

    def _cite_last_paper(self) -> str:
        if not self._last_paper:
            return "No paper in context, Boss."
        return self._last_paper.apa()

    def _cite_all(self) -> str:
        if not self._last_results:
            return "No papers in context, Boss."
        lines = [f"{i+1}. {p.apa()}" for i, p in enumerate(self._last_results[:10])]
        # Speak first 3, show all on card
        spoken = "; ".join(f"{i+1}. {p.title}" for i, p in enumerate(self._last_results[:3]))
        result = f"Citations, Boss:\n" + "\n".join(lines)
        if self._smart_card_mgr:
            try:
                self._smart_card_mgr.on_response("citations", result)
            except Exception:
                pass
        return f"Here are the citations, Boss: {spoken}... Full list on your smart card."

    def _citations_count(self) -> str:
        if not self._last_paper:
            return "No paper in context, Boss."
        p = self._last_paper
        return (f"'{p.title}' has {p.citations} citations, Boss." if p.citations
                else f"Citation count not available for '{p.title}', Boss.")

    # ── Vault and card ────────────────────────────────────────────────────────

    def _manual_save(self, topic: str) -> str:
        if not self._last_results:
            return "No research results to save, Boss."
        summary = self._summarise_results(topic, self._last_results)
        path = self._save_to_vault(topic, self._last_results, summary)
        return (f"Research on {topic} saved to your Obsidian vault, Boss."
                if path else "Vault save failed, Boss.")

    def _save_to_vault(self, topic: str, papers: List[Paper], summary: str) -> Optional[str]:
        if not self._vault_brain:
            return None
        try:
            folder = self._vault_brain.atlas / "Research" / "Academic"
            folder.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^\w\s-]", "", topic.lower()).replace(" ", "-")[:40]
            fname = f"{date.today()}-{slug}.md"
            citations_md = "\n".join(f"- {p.apa()}" for p in papers)
            papers_md = "\n\n".join(
                (f"### {p.title}\n"
                 f"**Authors:** {', '.join(p.authors[:3])}{' et al.' if len(p.authors) > 3 else ''}  \n"
                 f"**Year:** {p.year or 'N/A'}  |  **Citations:** {p.citations}  "
                 f"|  **Source:** {p.source}  \n"
                 f"{p.abstract[:500] if p.abstract else 'Abstract not available.'}")
                for p in papers[:10]
            )
            (folder / fname).write_text(
                f"---\ntags: [research, academic, {slug}]\n"
                f"date: {date.today()}\ntopic: {topic}\n---\n\n"
                f"# Research: {topic}\n\n"
                f"## Summary\n{summary}\n\n"
                f"## Papers\n{papers_md}\n\n"
                f"## Citations\n{citations_md}\n",
                encoding="utf-8")
            return str(folder / fname)
        except Exception as exc:
            log.error("Research: vault save failed: %s", exc)
            return None

    def _format_card_text(self, topic: str, papers: List[Paper]) -> str:
        lines = [f"Research: {topic}\n"]
        for i, p in enumerate(papers[:6]):
            yr = f" ({p.year})" if p.year else ""
            cit = f" — {p.citations} citations" if p.citations else ""
            lines.append(f"{i+1}. {p.title}{yr}{cit}")
        return "\n".join(lines)

    # ── HTTP helper ───────────────────────────────────────────────────────────

    def _fetch(self, url: str) -> Optional[str]:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "ATLAS-Research/1.0 (atlas@atlas.ai)"})
            with urllib.request.urlopen(req, timeout=self._TIMEOUT) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset("utf-8") or "utf-8"
                return raw.decode(charset, errors="replace")
        except Exception as exc:
            log.debug("Research fetch error for %s: %s", url[:80], exc)
            return None
