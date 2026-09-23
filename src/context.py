"""Turns an inbound SIP call into context the model can act on.

Four jobs:
  1. Pull the caller's number out of the SIP `From` header.
  2. Render a matched customer as a labelled profile block.
  3. Produce each dynamic section of the system prompt: profile, language,
     delivery style, Arabic dialect, opening.
  4. Render the operator's prompt template with those sections substituted in.

The template itself — the role sentence, the rules and the guardrails — lives in
`prompts/system_template.txt`, is seeded into settings, and is editable in the
console. Nothing is appended to it in code: what the operator sees is what the
caller gets.
"""

import re

LANGUAGE_NAMES = {"ar": "Arabic", "en": "English"}

# Currency codes the demo can realistically carry, as singular/plural names.
# The model reads a bare three-letter code as an initialism — "Q A R" — so the
# code is expanded to a spoken name before it ever reaches the prompt.
# Anything not listed is left untouched rather than guessed at.
CURRENCY_NAMES = {
    "QAR": ("Qatari riyal", "Qatari riyals"),
    "USD": ("US dollar", "US dollars"),
    "EUR": ("euro", "euros"),
    "GBP": ("British pound", "British pounds"),
    "AED": ("UAE dirham", "UAE dirhams"),
    "SAR": ("Saudi riyal", "Saudi riyals"),
}

# "QAR 62,000" and the bare code on its own. The amount form is substituted
# first so the name lands *after* the number, the way it is spoken.
_CURRENCY_AMOUNT = re.compile(
    r"\b(" + "|".join(CURRENCY_NAMES) + r")\s+(\d[\d,]*(?:\.\d+)?)\b"
)
_CURRENCY_BARE = re.compile(r"\b(" + "|".join(CURRENCY_NAMES) + r")\b")


def expand_currency_codes(text):
    """Replaces currency codes with names the model will say out loud.

    Applied to the whole profile block rather than only the formatted amounts,
    because the seeded free-text fields embed the code too — "Auto loan, QAR
    62,000 outstanding". Word boundaries keep it off reference numbers.
    """
    if not text:
        return text

    text = _CURRENCY_AMOUNT.sub(
        lambda m: f"{m.group(2)} {CURRENCY_NAMES[m.group(1)][1]}", text
    )
    return _CURRENCY_BARE.sub(lambda m: CURRENCY_NAMES[m.group(1)][1], text)


# Said out loud, so it also covers currencies arriving from the knowledge base,
# which never pass through expand_currency_codes().
PRONUNCIATION_DIRECTIVE = (
    "Pronunciation: say currency names in full, never as letters. Read \"QAR\" as "
    "\"Qatari riyals\" in English and \"ريال قطري\" in Arabic, and treat any other "
    "three-letter currency code the same way — \"USD\" is \"US dollars\". Never spell "
    "a currency code out letter by letter."
)


# Arabic delivery styles. The prompt text for the non-default variants lives in
# settings so it can be edited in the console.
ARABIC_VARIANTS = {
    "default": "Model default",
    "faseeh": "Faseeh (Modern Standard Arabic)",
    "qatari": "Qatari dialect",
}

# How the agent should sound, independent of the language it is speaking.
# id -> (label shown in the console, paragraph substituted for {style}).
#
# Deliberately not editable in the console: these are short, load-bearing
# sentences, and an operator who wants different wording can edit the prompt
# template itself, which is where real customisation belongs.
VOICE_STYLES = {
    "professional": (
        "Professional",
        "Delivery style: professional. Sound polished, composed and efficient. Use courteous, "
        "businesslike phrasing, avoid slang and filler, and keep the call moving.",
    ),
    "friendly": (
        "Friendly",
        "Delivery style: friendly. Sound warm, relaxed and conversational, like a helpful "
        "colleague rather than a switchboard. Use the caller's name naturally, and keep any "
        "pleasantries short and sincere.",
    ),
    "empathetic": (
        "Empathetic",
        "Delivery style: empathetic. Sound patient and reassuring. Acknowledge how the caller "
        "feels before moving on to the solution, slow your pace, and check they have followed "
        "you before continuing.",
    ),
    "energetic": (
        "Energetic",
        "Delivery style: energetic. Sound upbeat, enthusiastic and positive, with lively pacing "
        "and expressive emphasis. Stay professional: enthusiasm must never sound insincere or "
        "hurry the caller.",
    ),
    "concise": (
        "Concise",
        "Delivery style: concise. Use the fewest words that fully answer the question. Short "
        "sentences, no preamble, no repetition. Still greet and close politely.",
    ),
    "formal": (
        "Formal",
        "Delivery style: formal. Sound reserved and highly deferential. Use complete sentences "
        "and formal forms of address, and avoid colloquialisms, humour and contractions.",
    ),
}

DEFAULT_VOICE_STYLE = "professional"

# Sections the prompt template can substitute. The descriptions are shown as the
# legend beside the template editor in the console.
PLACEHOLDERS = {
    "bank": "The bank's name, in the language the caller will be spoken to.",
    "profile": "The caller's profile, or a note that their number matched no record.",
    "language": "Which language to hold the conversation in.",
    "style": "The delivery style chosen in Agent settings.",
    "dialect": "The Arabic style set on the caller's profile. Empty for English callers.",
    "knowledge": "When to consult the knowledge base. Empty when none is configured.",
    "pronunciation": "How to say currency codes out loud, so \"QAR\" is not spelled out.",
    "opening": "How to open the call, including how to greet the caller by name.",
}

# Without these the agent either loses the caller's context or never opens the
# call, so removing one is worth warning about.
REQUIRED_PLACEHOLDERS = ("profile", "opening")

# Matches the user part of a SIP/TEL URI, e.g.
#   sip:+97455512345@sip.example.com
#   "Ahmed" <sip:97455512345@host>;tag=abc
#   tel:+97455512345
_URI_USER = re.compile(r"(?:sips?|tel):\+?([0-9\-\.\(\) ]+?)(?:[@;>]|$)", re.IGNORECASE)


def get_sip_header(sip_headers, name):
    """Reads one header by name, case-insensitively.

    `sip_headers` is the list of {"name": ..., "value": ...} dicts carried on
    the `realtime.call.incoming` webhook.
    """
    for header in sip_headers or []:
        if isinstance(header, dict) and (header.get("name") or "").lower() == name.lower():
            return header.get("value")
    return None


def caller_number(sip_headers):
    """Extracts the calling number from the SIP `From` header."""
    from_header = get_sip_header(sip_headers, "From")
    if not from_header:
        return None

    match = _URI_USER.search(from_header)
    if not match:
        return None

    number = re.sub(r"[^\d]", "", match.group(1))
    if not number:
        return None

    # Anonymous/withheld callers arrive with a non-numeric user part, which the
    # digit strip above turns into an empty string, so anything left is usable.
    return "+" + number if from_header.find("+") != -1 else number


def _money(amount, currency):
    """Formats an amount the way it should be spoken, not written.

    "QAR 4,820.00" is read back as "Q A R"; "4,820.00 Qatari riyals" is not.
    Unknown codes keep the written form rather than being mangled.
    """
    if amount is None:
        return None

    figure = f"{amount:,.2f}"
    names = CURRENCY_NAMES.get((currency or "").strip().upper())
    if not names:
        return f"{currency or ''} {figure}".strip()

    singular, plural = names
    return f"{figure} {singular if abs(amount) == 1 else plural}"


def build_profile(customer):
    """Renders the customer as a labelled block.

    One label per line reads reliably for the model and stays greppable in the
    logs. Empty fields are omitted rather than shown as "None", which would
    invite the model to talk about them.
    """
    if not customer:
        return None

    language = LANGUAGE_NAMES.get(customer.get("preferred_language"), "English")
    currency = customer.get("currency")

    lines = ["CUSTOMER PROFILE"]

    def add(label, value):
        if value not in (None, "", []):
            lines.append(f"{label}: {value}")

    add("Name (English)", customer.get("full_name_en"))
    add("Name (Arabic)", customer.get("full_name_ar"))
    add("Preferred language", language)
    if customer.get("preferred_language") == "ar":
        variant = customer.get("arabic_variant") or "default"
        if variant != "default":
            add("Arabic style", ARABIC_VARIANTS.get(variant, variant))
    add("Mobile", customer.get("mobile_e164"))
    add("Segment", customer.get("segment"))
    add("Home branch", customer.get("branch"))
    add("Account type", customer.get("account_type"))
    add("Balance", _money(customer.get("balance"), currency))

    if customer.get("last_txn_amount") is not None:
        parts = [_money(customer["last_txn_amount"], currency)]
        if customer.get("last_txn_merchant"):
            parts.append(f"at {customer['last_txn_merchant']}")
        if customer.get("last_txn_date"):
            parts.append(f"on {customer['last_txn_date']}")
        add("Last transaction", " ".join(p for p in parts if p))

    add("Card status", customer.get("card_status"))

    kyc = customer.get("kyc_status")
    if kyc:
        add("KYC", f"{kyc} (expires {customer['kyc_expiry']})" if customer.get("kyc_expiry") else kyc)

    add("Open case", customer.get("open_case"))
    add("Loan", customer.get("loan_summary"))
    add("Relationship manager", customer.get("relationship_manager"))

    # Catches the codes embedded in free-text fields, which never went through
    # _money(). Amounts formatted above are already expanded, so this is a
    # no-op for them.
    return expand_currency_codes("\n".join(lines))


def language_directive(customer):
    if customer and customer.get("preferred_language") == "ar":
        return (
            "The caller's preferred language is Arabic. Greet them and hold the entire "
            "conversation in Arabic. Switch language only if the caller switches first."
        )
    return (
        "Hold the conversation in English. Switch language only if the caller asks or "
        "starts speaking another language."
    )


def dialect_prompt(customer, prompts):
    """The Arabic style prompt for this customer, if any.

    `prompts` maps variant name to prompt text, loaded from settings so the
    wording can be edited in the console. Only applies to Arabic speakers.
    """
    if not customer or customer.get("preferred_language") != "ar":
        return None

    variant = (customer.get("arabic_variant") or "default").strip()
    if variant in ("", "default"):
        return None

    text = (prompts or {}).get(variant)
    return text.strip() if text and text.strip() else None


DEFAULT_BANK_EN = "Commercial Bank of Qatar"
DEFAULT_BANK_AR = "البنك التجاري"


def opening_directive(customer, bank_en, bank_ar):
    """Tells the agent how to open the call.

    Deliberately describes *intent* rather than supplying a sentence to imitate.
    A hardcoded example would be written in one Arabic register and would then
    fight whichever style prompt is active — a Gulf-colloquial example pulls a
    Faseeh session off-register on its very first word.
    """
    arabic = customer and customer.get("preferred_language") == "ar"
    bank = bank_ar if arabic else bank_en

    if not customer:
        return (
            f"Open the call by greeting the caller warmly on behalf of {bank}, then ask how "
            f"you can help. Keep it to one short sentence."
        )

    name = (
        (customer.get("full_name_ar") or customer.get("full_name_en"))
        if arabic
        else customer.get("full_name_en")
    )
    return (
        f"Open the call yourself, before the caller speaks: greet {name} by name on behalf of "
        f"{bank}, then ask how you can help. Keep it to one short, natural sentence, and follow "
        f"the language and style rules above."
    )


def build_hold_instruction(customer, prompts=None, nth=1):
    """A one-line brief for a holding phrase while a lookup is running.

    Sent as per-response `instructions`, which *replace* the session
    instructions for that response — so the language and dialect rules have to
    be repeated here or an Arabic caller is answered in English.

    `nth` is which holding phrase this is in the current wait, so the second
    one can be told not to echo the first.
    """
    parts = [language_directive(customer)]

    dialect = dialect_prompt(customer, prompts)
    if dialect:
        parts.append(dialect)

    brief = (
        "You are still waiting for a knowledge base lookup to come back. Say one short, "
        "natural sentence to let the caller know you are still looking, so the line is not "
        "silent. Do not attempt to answer the question, do not ask a new question, and do "
        "not invent any facts. Keep it under about eight words."
    )
    if nth > 1:
        brief += " You have already said you are checking, so word this differently."

    parts.append(brief)
    return "\n\n".join(parts)


def style_prompt(style):
    """The delivery-style paragraph for a style id, or the default's."""
    entry = VOICE_STYLES.get(style) or VOICE_STYLES[DEFAULT_VOICE_STYLE]
    return entry[1]


def profile_section(customer):
    """What the agent is told about who it is speaking to.

    Covers both cases in one section, so a template that keeps only
    `{profile}` still behaves correctly for an unrecognised caller.
    """
    if not customer:
        return (
            "The caller's number does not match any customer record, so you do not know "
            "who they are. Greet them politely as an unrecognised caller, and offer to help "
            "with general enquiries. Do not claim to see any account details."
        )

    return (
        build_profile(customer)
        + "\n\nYou already know who is calling because the call came from their registered "
        "mobile number. Do not ask them to identify themselves again."
    )


_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def render(template, sections):
    """Substitutes `{name}` placeholders, leaving unknown ones untouched.

    Deliberately not `str.format`: the template is operator-editable free text
    and the Arabic prompts contain braces, so a stray `{` must never raise
    while a call is waiting to be accepted. Sections that resolve to nothing
    leave a blank run behind, which is collapsed so the prompt stays tidy.
    """
    text = _PLACEHOLDER.sub(
        lambda match: sections[match.group(1)] or ""
        if match.group(1) in sections
        else match.group(0),
        template or "",
    )
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def unknown_placeholders(template):
    """Placeholder names in the template that nothing will ever fill."""
    return sorted({name for name in _PLACEHOLDER.findall(template or "") if name not in PLACEHOLDERS})


def missing_placeholders(template):
    """Load-bearing placeholders the template has dropped."""
    present = set(_PLACEHOLDER.findall(template or ""))
    return [name for name in REQUIRED_PLACEHOLDERS if name not in present]


def build_instructions(
    template,
    customer,
    prompts=None,
    bank_en=DEFAULT_BANK_EN,
    bank_ar=DEFAULT_BANK_AR,
    voice_style=DEFAULT_VOICE_STYLE,
    knowledge_directive="",
):
    """Renders the accept-time `instructions` string from the stored template.

    The opening directive is part of this string rather than a per-response
    override because `response.create` instructions *replace* the session
    instructions for that response — which previously stripped the dialect,
    profile and guardrails from the agent's very first sentence.

    The shipped template puts `{style}` before `{dialect}` on purpose: the
    Arabic dialect prompts carry strict register rules, and whichever section
    comes last tends to win, so the dialect must have the final word on how the
    agent sounds.
    """
    arabic = bool(customer and customer.get("preferred_language") == "ar")
    directive = (knowledge_directive or "").strip()

    instructions = render(
        template,
        {
            "bank": bank_ar if arabic else bank_en,
            "profile": profile_section(customer),
            "language": language_directive(customer),
            "style": style_prompt(voice_style),
            "dialect": dialect_prompt(customer, prompts) or "",
            "knowledge": directive,
            "pronunciation": PRONUNCIATION_DIRECTIVE,
            "opening": opening_directive(customer, bank_en, bank_ar),
        },
    )

    # A template edited before these placeholders existed has nowhere to put
    # them, and dropping them would leave the agent holding a search tool it
    # was never told to use, or spelling currency codes out. Append instead of
    # silently losing them; appending never disturbs an operator's own edits.
    extras = [
        text
        for placeholder, text in (
            ("{knowledge}", directive),
            ("{pronunciation}", PRONUNCIATION_DIRECTIVE),
        )
        if text and placeholder not in (template or "")
    ]
    if extras:
        instructions = instructions.rstrip() + "\n\n" + "\n\n".join(extras)

    return instructions
