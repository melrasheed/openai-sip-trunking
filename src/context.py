"""Turns an inbound SIP call into context the model can act on.

Three jobs:
  1. Pull the caller's number out of the SIP `From` header.
  2. Render a matched customer as a labelled profile block.
  3. Assemble the accept-time instructions and the opening greeting.
"""

import re

LANGUAGE_NAMES = {"ar": "Arabic", "en": "English"}

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
    if amount is None:
        return None
    return f"{currency or ''} {amount:,.2f}".strip()


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

    return "\n".join(lines)


def language_directive(customer):
    if customer and customer.get("preferred_language") == "ar":
        return (
            "The caller's preferred language is Arabic. Greet them and hold the entire "
            "conversation in Arabic, using Gulf dialect where it sounds natural. Switch "
            "language only if the caller switches first."
        )
    return (
        "Hold the conversation in English. Switch language only if the caller asks or "
        "starts speaking another language."
    )


GUARDRAILS = (
    "Rules you must follow:\n"
    "- Use only the facts in the customer profile above. Never invent balances, "
    "transactions, dates, or case references.\n"
    "- If you are asked something the profile does not cover, say you will check with "
    "a colleague and offer to follow up, rather than guessing.\n"
    "- Never read out the full account balance until the caller has asked for it.\n"
    "- Keep replies short and natural: this is a phone call, not a written chat."
)


def build_instructions(base_instructions, customer):
    """Assembles the accept-time `instructions` string."""
    sections = [base_instructions.strip()]

    if customer:
        sections.append(build_profile(customer))
        sections.append(
            f"You already know who is calling because the call came from their registered "
            f"mobile number. Greet {customer.get('full_name_en')} by name. Do not ask them "
            f"to identify themselves again."
        )
    else:
        sections.append(
            "The caller's number does not match any customer record, so you do not know "
            "who they are. Greet them politely as an unrecognised caller, and offer to help "
            "with general enquiries. Do not claim to see any account details."
        )

    sections.append(language_directive(customer))
    sections.append(GUARDRAILS)
    return "\n\n".join(section for section in sections if section)


def build_greeting(customer, fallback_greeting):
    """The line the agent opens with, passed via `response.create`."""
    if not customer:
        return fallback_greeting

    if customer.get("preferred_language") == "ar":
        name = customer.get("full_name_ar") or customer.get("full_name_en")
        return (
            f"Greet the caller in Arabic by name. Say something close to: "
            f"أهلاً {name}، معك المساعد الذكي لبنك قطر الوطني. كيف أقدر أساعدك اليوم؟"
        )

    name = customer.get("full_name_en")
    return (
        f"Greet the caller in English by name. Say something close to: "
        f"Hello {name}, thank you for calling. How can I help you today?"
    )
