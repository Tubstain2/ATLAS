"""
ATLAS Socratic Tutor — teaches any topic via questions, not explanations.

Session flow:
  1. "ATLAS teach me X" → assess prior knowledge
  2. Calibrate difficulty 1-5 from answer
  3. Ask Socratic questions cycling through stages:
     FOUNDATION → MECHANISM → IMPLICATIONS → EDGE_CASES → CONNECTIONS → APPLICATION
  4. Evaluate each answer with brain, adapt difficulty
  5. Summarise every 5 questions
  6. After 20 questions or "ATLAS stop teaching" → save notes to vault

Voice commands:
  "ATLAS teach me X"                    → start session
  "ATLAS I already know X, go deeper"   → start at difficulty 4
  "ATLAS explain that one"              → brief direct explanation
  "ATLAS simpler please"                → easier question
  "ATLAS harder please"                 → harder question
  "ATLAS give me a hint"                → small clue
  "ATLAS just tell me"                  → full direct answer
  "ATLAS quiz me on what we covered"    → test session
  "ATLAS save my notes"                 → manual vault save
  "ATLAS stop teaching"                 → end with summary
  "ATLAS continue where we left off on X" → reload from vault
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

_STAGES = ["FOUNDATION", "MECHANISM", "IMPLICATIONS", "EDGE_CASES",
           "CONNECTIONS", "APPLICATION"]

_QUESTION_PROMPT = """\
You are a Socratic tutor teaching: {topic}
Student knowledge level: {difficulty}/5
Current stage: {stage}

Ask ONE question that helps the student discover a core concept about {topic}.
Do NOT give explanations or answers — only ask a question.
Adapt complexity to the knowledge level.
Stage guidance:
- FOUNDATION: What is it? Why does it exist?
- MECHANISM: How does it work step by step?
- IMPLICATIONS: What happens because of it?
- EDGE_CASES: When does it break down or not apply?
- CONNECTIONS: How does it relate to other concepts?
- APPLICATION: How would you use this to solve a real problem?

Respond with ONLY the question. No preamble."""

_EVAL_PROMPT = """\
Student was asked: "{question}"
They answered: "{answer}"

Rate their understanding and respond with ONLY valid JSON (no markdown):
{{"score": <1-10>, "feedback": "<one sentence>", "correct": <true/false>, "next_difficulty": "<easier|same|harder>"}}"""

_SUMMARY_PROMPT = """\
You are summarising a Socratic tutoring session on: {topic}
Questions covered: {questions}
Key insights the student demonstrated: {insights}

Write a 3-sentence summary of what was learned. For voice output — no markdown, plain prose."""

_NOTES_PROMPT = """\
Write structured learning notes for a tutoring session on: {topic}
Student level: {difficulty}/5
Questions explored: {questions}
Key insights: {insights}

Format as:
# Learning Session: {topic}
## Key Concepts Covered
(3-5 bullet points)
## Questions That Revealed Gaps
(2-3 questions that the student found hardest)
## Student's Own Understanding
(what they explained well in their own words)
## Next Session Focus
(what to tackle next)"""


@dataclass
class TutorSession:
    topic: str
    subject_type: str        # coding | market | science | general
    difficulty: int = 2      # 1-5
    questions_asked: List[str] = field(default_factory=list)
    answers_given: List[str] = field(default_factory=list)
    correct_answers: int = 0
    key_insights: List[str] = field(default_factory=list)
    current_question: str = ""
    stage_index: int = 0
    awaiting_answer: bool = False
    awaiting_knowledge: bool = True


class ATLASTutor:
    """Socratic teaching mode — questions, not explanations."""

    def __init__(self, config: dict, speak_cb: Callable,
                 brain, vault_brain=None):
        self._config      = config
        self._speak       = speak_cb
        self._brain       = brain
        self._vault_brain = vault_brain
        self._enabled     = config.get("tutor_enabled", True)
        self._max_q       = int(config.get("tutor_questions_per_session", 20))
        self._summary_n   = int(config.get("tutor_summary_interval", 5))
        self._session: Optional[TutorSession] = None

        log.info("ATLASTutor: ready.")

    # ── Voice router ──────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        if not self._enabled:
            return None
        lower = text.lower().strip()
        lower_clean = re.sub(r"^atlas\s+", "", lower)

        # ── Active session — absorb voice input as student answer ──────────────
        if self._session and self._session.awaiting_answer:
            # Check for session control commands first
            if "stop teaching" in lower_clean:
                return self._end_session()
            if "simpler please" in lower_clean or "make it simpler" in lower_clean:
                return self._adjust_difficulty(-1)
            if "harder please" in lower_clean or "make it harder" in lower_clean:
                return self._adjust_difficulty(1)
            if "give me a hint" in lower_clean:
                return self._give_hint()
            if "just tell me" in lower_clean or "explain that one" in lower_clean:
                return self._direct_answer()
            if "save my notes" in lower_clean:
                return self._save_notes()
            return self._on_answer(text)

        # ── Knowledge assessment ───────────────────────────────────────────────
        if self._session and self._session.awaiting_knowledge:
            return self._on_knowledge_assessment(text)

        # ── Start session commands ─────────────────────────────────────────────
        m = re.search(r"teach me (.+?)$", lower_clean)
        if m:
            topic = m.group(1).strip()
            return self._start_session(topic, 2)

        m = re.search(r"i already know (.+?),?\s*go deeper", lower_clean)
        if m:
            topic = m.group(1).strip()
            return self._start_session(topic, 4)

        if "stop teaching" in lower_clean:
            if self._session:
                return self._end_session()
            return None

        m = re.search(r"continue where we left off on (.+?)$", lower_clean)
        if m:
            topic = m.group(1).strip()
            return self._resume_from_vault(topic)

        if "quiz me on what we covered" in lower_clean and self._session:
            return self._quiz_mode()

        if "save my notes" in lower_clean and self._session:
            return self._save_notes()

        return None

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def _start_session(self, topic: str, difficulty: int = 2) -> str:
        subject_type = self._detect_subject(topic)
        self._session = TutorSession(
            topic=topic, subject_type=subject_type,
            difficulty=difficulty, awaiting_knowledge=True,
        )
        if difficulty >= 4:
            self._session.awaiting_knowledge = False
            return self._ask_question()
        return (f"Before we start, Boss — what do you already know about {topic}? "
                f"Even a rough sense is fine.")

    def _on_knowledge_assessment(self, answer: str) -> str:
        s = self._session
        s.awaiting_knowledge = False
        lower = answer.lower()

        if any(w in lower for w in ("nothing", "never", "no idea", "don't know",
                                     "dont know", "beginner", "new to")):
            s.difficulty = 1
        elif any(w in lower for w in ("little", "basics", "heard of", "a bit",
                                       "some", "not much")):
            s.difficulty = 2
        elif any(w in lower for w in ("familiar", "some experience", "decent",
                                       "moderate", "intermediate")):
            s.difficulty = 3
        elif any(w in lower for w in ("good", "comfortable", "experienced",
                                       "solid", "well")):
            s.difficulty = 4
        elif any(w in lower for w in ("expert", "advanced", "professional",
                                       "master", "years")):
            s.difficulty = 5

        level_desc = {1: "beginner", 2: "basic", 3: "intermediate",
                      4: "advanced", 5: "expert"}[s.difficulty]
        preface = (f"Got it — starting at {level_desc} level. "
                   f"Let's explore {s.topic} through questions. ")
        return preface + self._ask_question()

    def _ask_question(self) -> str:
        s = self._session
        stage = _STAGES[s.stage_index % len(_STAGES)]
        prompt = _QUESTION_PROMPT.format(
            topic=s.topic, difficulty=s.difficulty, stage=stage)
        try:
            question = self._brain.ask(prompt).strip()
        except Exception:
            question = f"What is the most fundamental thing about {s.topic}?"

        s.current_question = question
        s.awaiting_answer = True
        return question

    def _on_answer(self, answer: str) -> str:
        s = self._session
        s.answers_given.append(answer)
        s.questions_asked.append(s.current_question)
        s.awaiting_answer = False

        eval_prompt = _EVAL_PROMPT.format(
            question=s.current_question, answer=answer)
        try:
            raw = self._brain.ask(eval_prompt).strip()
            # Strip potential markdown fences
            raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
            evaluation = json.loads(raw)
            score = int(evaluation.get("score", 5))
            feedback = evaluation.get("feedback", "")
            next_diff = evaluation.get("next_difficulty", "same")
        except Exception:
            score = 5
            feedback = "Good effort."
            next_diff = "same"

        # Adjust difficulty
        if next_diff == "harder" and s.difficulty < 5:
            s.difficulty = min(5, s.difficulty + 1)
        elif next_diff == "easier" and s.difficulty > 1:
            s.difficulty = max(1, s.difficulty - 1)

        # Track insights from good answers
        if score >= 7:
            s.correct_answers += 1
            s.key_insights.append(answer[:100])

        # Advance stage
        s.stage_index += 1

        # Check end condition
        total_q = len(s.questions_asked)
        if total_q >= self._max_q:
            return self._end_session()

        # Periodic summary
        if total_q > 0 and total_q % self._summary_n == 0:
            summary = self._generate_summary()
            next_q = self._ask_question()
            return f"{self._response_prefix(score, feedback)} {summary} {next_q}"

        return f"{self._response_prefix(score, feedback)} {self._ask_question()}"

    def _response_prefix(self, score: int, feedback: str) -> str:
        if score >= 7:
            return f"You are on the right track. {feedback}"
        elif score >= 4:
            return f"Good thinking. {feedback} Let me ask it differently."
        else:
            return f"Interesting perspective. {feedback} What do you think about this:"

    def _generate_summary(self) -> str:
        s = self._session
        prompt = _SUMMARY_PROMPT.format(
            topic=s.topic,
            questions="; ".join(s.questions_asked[-5:]),
            insights="; ".join(s.key_insights[-5:]) or "still building",
        )
        try:
            return self._brain.ask(prompt)
        except Exception:
            return (f"So far on {s.topic}, we have covered "
                    f"{len(s.questions_asked)} concepts.")

    def _end_session(self) -> str:
        s = self._session
        if not s:
            return "No active tutoring session, Boss."

        self._session = None
        summary = self._generate_summary_for_session(s)

        if self._config.get("tutor_save_notes", True):
            threading.Thread(target=self._save_notes_async, args=(s,),
                             daemon=True).start()

        return (f"Tutoring session on {s.topic} complete, Boss. "
                f"You answered {s.correct_answers} of {len(s.questions_asked)} "
                f"questions well. {summary}")

    def _generate_summary_for_session(self, s: TutorSession) -> str:
        if not s.questions_asked:
            return ""
        prompt = _SUMMARY_PROMPT.format(
            topic=s.topic,
            questions="; ".join(s.questions_asked[-10:]),
            insights="; ".join(s.key_insights[-5:]) or "exploration phase",
        )
        try:
            return self._brain.ask(prompt)
        except Exception:
            return f"Great work exploring {s.topic} today."

    def _save_notes_async(self, s: TutorSession) -> None:
        if not self._vault_brain:
            return
        try:
            prompt = _NOTES_PROMPT.format(
                topic=s.topic, difficulty=s.difficulty,
                questions="\n".join(f"- {q}" for q in s.questions_asked[-15:]),
                insights="\n".join(f"- {i}" for i in s.key_insights[-10:]) or "- still developing",
            )
            notes = self._brain.ask(prompt)
            folder = self._vault_brain.atlas / "Coaching" / "Learning"
            folder.mkdir(parents=True, exist_ok=True)
            fname = f"{s.topic.replace(' ', '-')}-{date.today()}.md"
            (folder / fname).write_text(
                f"---\ntags: [tutor, learning, {s.subject_type}]\n"
                f"date: {date.today()}\ntopic: {s.topic}\n---\n\n{notes}\n",
                encoding="utf-8")
        except Exception as exc:
            log.error("Tutor: notes save failed: %s", exc)

    def _save_notes(self) -> str:
        if not self._session:
            return "No active tutoring session, Boss."
        threading.Thread(target=self._save_notes_async, args=(self._session,),
                         daemon=True).start()
        return "Saving your tutoring notes to Obsidian, Boss."

    # ── Session controls ───────────────────────────────────────────────────────

    def _adjust_difficulty(self, delta: int) -> Optional[str]:
        if not self._session:
            return None
        self._session.difficulty = max(1, min(5, self._session.difficulty + delta))
        self._session.awaiting_answer = False
        direction = "simpler" if delta < 0 else "more advanced"
        return f"Got it — {direction} question coming. " + self._ask_question()

    def _give_hint(self) -> Optional[str]:
        if not self._session or not self._session.current_question:
            return "No question in progress to hint at, Boss."
        s = self._session
        prompt = (f"For this Socratic question about {s.topic}: '{s.current_question}' "
                  f"Give a small hint that points the student in the right direction "
                  f"without giving away the answer. One sentence only.")
        try:
            hint = self._brain.ask(prompt)
            return f"Here is a hint, Boss: {hint} Now, {s.current_question}"
        except Exception:
            return f"Think about the fundamentals of {s.topic}. {s.current_question}"

    def _direct_answer(self) -> Optional[str]:
        if not self._session or not self._session.current_question:
            return "No question in progress to answer directly, Boss."
        s = self._session
        s.awaiting_answer = False
        prompt = (f"Give a direct, clear answer to this question about {s.topic}: "
                  f"'{s.current_question}'. Max 3 sentences, plain prose.")
        try:
            answer = self._brain.ask(prompt)
            next_q = self._ask_question()
            return f"{answer} Now let us continue. {next_q}"
        except Exception:
            return f"The answer involves the core principles of {s.topic}. Let us try another angle."

    def _quiz_mode(self) -> str:
        if not self._session or not self._session.questions_asked:
            return "Nothing covered yet in this session, Boss."
        s = self._session
        prompt = (f"Create 3 quick quiz questions based on what was covered in a "
                  f"tutoring session on {s.topic}. Topics touched: "
                  f"{'; '.join(s.questions_asked[-5:])}. "
                  f"Make them multiple choice or short answer. Max 100 words.")
        try:
            quiz = self._brain.ask(prompt)
            return f"Quick quiz on what we covered, Boss: {quiz}"
        except Exception:
            return f"Let us review {s.topic}. {s.questions_asked[-1] if s.questions_asked else ''}"

    def _resume_from_vault(self, topic: str) -> str:
        if not self._vault_brain:
            return f"Starting fresh on {topic}, Boss — no vault connected."
        try:
            folder = self._vault_brain.atlas / "Coaching" / "Learning"
            matches = list(folder.glob(f"{topic.replace(' ', '-')}*.md"))
            if not matches:
                return self._start_session(topic, 2)
            latest = max(matches, key=lambda p: p.stat().st_mtime)
            content = latest.read_text(encoding="utf-8")
            m = re.search(r"## Next Session Focus\n(.+?)(?:\n#|$)", content, re.DOTALL)
            focus = m.group(1).strip() if m else "where we left off"
            self._session = TutorSession(
                topic=topic, subject_type=self._detect_subject(topic),
                difficulty=3, awaiting_knowledge=False,
            )
            return (f"Resuming {topic}, Boss. Last focus: {focus[:100]}. "
                    + self._ask_question())
        except Exception as exc:
            log.error("Tutor: resume failed: %s", exc)
            return self._start_session(topic, 2)

    def _detect_subject(self, topic: str) -> str:
        lower = topic.lower()
        if any(w in lower for w in ("code", "python", "javascript", "algorithm",
                                     "function", "class", "api", "sql", "git",
                                     "programming", "software")):
            return "coding"
        if any(w in lower for w in ("stock", "market", "invest", "trading",
                                     "finance", "option", "crypto", "portfolio")):
            return "market"
        if any(w in lower for w in ("physics", "chemistry", "biology", "math",
                                     "maths", "calculus", "statistics", "science")):
            return "science"
        return "general"
