"""Extraction with one repair turn.

1. Ask the model for JSON matching the schema.
2. Validate it with Pydantic.
3. If it fails, send the model its own answer plus the exact errors and ask
   once more. The errors name the broken rule, which is what makes a second
   attempt useful. A third identical request would just fail the same way.
"""

from dataclasses import dataclass, field

from pydantic import ValidationError

from app.llm import strict_schema
from app.schema import JobPosting

SYSTEM_PROMPT = """\
You extract structured data from job postings and return only JSON matching the schema.

1. Most postings do not state pay. If there is no figure, or it says "competitive", \
"negotiable" or "as per company policy", salary.is_stated is false and every other \
salary field is null. That is a correct answer, not a failure.
2. Do not guess the pay period. Set salary.period only if the posting literally says \
it ("per month", "a year", "/hr"). A bare range like "PKR 30,000 to 50,000" leaves \
period null and period_stated false.
3. Take title and company from the posting text. If it contradicts a header \
("this role is with Aiden Risk and not Edge"), the text wins.

Return every key. Use null when unsure; title and company are never empty.
"""

USER_TEMPLATE = "Extract this job posting.\n\n<posting>\n{body}\n</posting>"

REPAIR_TEMPLATE = (
    "Your JSON did not validate:\n\n{errors}\n\n"
    "Return the corrected JSON for the same posting."
)

SCHEMA = strict_schema(JobPosting)
MAX_CALLS = 2  # the first answer plus one repair turn


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, completion) -> None:
        self.calls += 1
        self.input_tokens += completion.input_tokens
        self.output_tokens += completion.output_tokens


class ExtractionFailed(Exception):
    """The model's answer still failed validation after the repair turn."""

    def __init__(self, errors: str, usage: Usage):
        super().__init__(f"invalid after {usage.calls} attempts:\n{errors}")
        self.errors = errors
        self.usage = usage


@dataclass
class Extraction:
    posting: JobPosting
    usage: Usage = field(default_factory=Usage)


def format_errors(exc: ValidationError) -> str:
    return "\n".join(
        f"- {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
        for err in exc.errors()
    )


def extract(body: str, client, usage: Usage | None = None) -> Extraction:
    """Pass `usage` to keep counting tokens even if the call raises midway."""
    usage = usage if usage is not None else Usage()
    messages = [{"role": "user", "content": USER_TEMPLATE.format(body=body)}]
    errors = ""
    for _ in range(MAX_CALLS):
        completion = client.complete(SYSTEM_PROMPT, messages, SCHEMA)
        usage.add(completion)
        try:
            return Extraction(JobPosting.model_validate_json(completion.text), usage)
        except ValidationError as exc:
            errors = format_errors(exc)
            messages += [
                {"role": "assistant", "content": completion.text},
                {"role": "user", "content": REPAIR_TEMPLATE.format(errors=errors)},
            ]
    raise ExtractionFailed(errors, usage)
