import re
from difflib import SequenceMatcher


PROFILE_QUESTION_ALIASES = {
    "phone": (
        "phone", "telephone", "mobile", "contact number", "cell number",
    ),
    "email": ("email", "e-mail"),
    "visa_sponsorship": (
        "visa sponsorship", "require sponsorship", "need sponsorship",
        "sponsor now or in the future",
    ),
    "work_authorization": (
        "authorized to work", "authorised to work", "work authorization",
        "work authorisation", "legally eligible to work",
    ),
    "referred": ("referred", "referral status", "employee referral"),
    "referral_details": (
        "who referred", "referrer", "referral contact", "referral name",
    ),
    "linkedin": ("linkedin", "linked in"),
    "github": ("github", "git hub"),
    "experience": (
        "years of experience", "experience in years", "professional experience",
        "customer facing technical delivery experience",
    ),
    "location": ("current location", "based in", "where are you located"),
    "availability": ("notice period", "availability", "when can you start", "start date"),
    "relocation": ("relocation", "willing to relocate", "move to"),
}

SENSITIVE_PROFILE_KEYS = {
    "phone",
    "email",
    "visa_sponsorship",
    "work_authorization",
    "referred",
    "referral_details",
    "experience",
    "availability",
    "work_authorization",
}


def normalize_question(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", value.casefold()).split())


def match_profile_answer(question: str, profile_answers: dict[str, str]) -> tuple[str, str]:
    normalized = normalize_question(question)
    if not normalized:
        return "", ""

    if "sponsor" in normalized or "visa" in normalized:
        answer = str(profile_answers.get("visa_sponsorship", "") or "").strip()
        if answer:
            return "visa_sponsorship", answer
    if ("work" in normalized or "employment" in normalized) and any(
        word in normalized
        for word in ("authoriz", "authoris", "eligible", "permit", "legally")
    ):
        answer = str(profile_answers.get("work_authorization", "") or "").strip()
        if answer:
            return "work_authorization", answer
    if "experience" in normalized and (
        "year" in normalized or "customer facing" in normalized
    ):
        answer = str(profile_answers.get("experience", "") or "").strip()
        if answer:
            return "experience", answer
    if "refer" in normalized and any(
        word in normalized for word in ("who", "name", "contact", "person")
    ):
        answer = str(profile_answers.get("referral_details", "") or "").strip()
        return "referral_details", answer
    if "refer" in normalized:
        answer = str(profile_answers.get("referred", "") or "").strip()
        if answer:
            return "referred", answer
    if any(word in normalized for word in ("phone", "mobile", "telephone", "contact number", "cell number")):
        answer = str(profile_answers.get("phone", "") or "").strip()
        if answer:
            return "phone", answer

    # Check more specific topics before broad terms like "referral" or "experience".
    for key in (
        "referral_details", "visa_sponsorship", "work_authorization",
        "availability", "experience", "location", "linkedin", "github",
        "relocation", "referred", "phone", "email",
    ):
        answer = str(profile_answers.get(key, "") or "").strip()
        if not answer:
            continue
        aliases = PROFILE_QUESTION_ALIASES[key]
        if any(normalize_question(alias) in normalized for alias in aliases):
            return key, answer

    return "", ""


def match_saved_question(
    question: str,
    question_answers: dict[str, dict[str, str]],
    *,
    minimum_similarity: float = 0.82,
) -> tuple[str, str]:
    normalized = normalize_question(question)
    if not normalized:
        return "", ""
    stop_words = {"a", "an", "are", "can", "could", "do", "does", "for", "how", "is", "me", "of", "please", "share", "the", "to", "what", "when", "your"}
    question_words = set(normalized.split()) - stop_words
    for stored_question, entry in question_answers.items():
        if not isinstance(entry, dict):
            continue
        answer = str(entry.get("answer", "") or "").strip()
        saved_text = normalize_question(str(entry.get("question", stored_question)))
        if not answer or not saved_text:
            continue
        if normalized == saved_text or normalized in saved_text or saved_text in normalized:
            return str(entry.get("question", stored_question)), answer
        saved_words = set(saved_text.split()) - stop_words
        token_similarity = len(question_words & saved_words) / max(
            1, len(question_words | saved_words)
        )
        if (
            SequenceMatcher(None, normalized, saved_text).ratio() >= minimum_similarity
            or token_similarity >= 0.6
        ):
            return str(entry.get("question", stored_question)), answer
    return "", ""


def is_sensitive_question(question: str) -> bool:
    normalized = normalize_question(question)
    return any(
        normalize_question(alias) in normalized
        for key in SENSITIVE_PROFILE_KEYS
        for alias in PROFILE_QUESTION_ALIASES[key]
    ) or bool(re.search(
        r"\b(salary|compensation|work permit|citizenship|nationality|immigration|"
        r"sponsorship|visa|authorized|authorised|phone|mobile|telephone|"
        r"email|e mail|years of experience|notice period|availability)\b",
        normalized,
    )) or bool(re.search(
        r"\b(years?.{0,50}experience|experience.{0,50}years?|"
        r"worked with.{0,40}(?:llm|generative ai|rest api)|"
        r"(?:llm|generative ai).{0,60}(?:production|experience)|"
        r"hands on.{0,40}(?:rest api|webhook|data mapping))\b",
        normalized,
    ))
