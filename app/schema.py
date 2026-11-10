"""What the model must return for each posting.

Field descriptions are sent to the model as part of the JSON schema, so they
double as instructions. The validators are the rules the repair turn feeds
back to the model when it gets something wrong.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Period = Literal["hourly", "monthly", "yearly"]
Seniority = Literal["intern", "junior", "mid", "senior", "lead", "principal"]
WorkMode = Literal["onsite", "hybrid", "remote"]


class Salary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_stated: bool = Field(
        description="true only when the posting gives an actual pay figure. "
        "'Competitive', 'negotiable', 'as per company policy' and silence are all false."
    )
    min_amount: int | None = Field(
        description="Lower bound as a plain number. For a single figure, use it for "
        "both min and max. null when is_stated is false."
    )
    max_amount: int | None = Field(description="Upper bound. null when is_stated is false.")
    currency: str | None = Field(
        description="ISO code as written: PKR for Rs/PKR, USD for $. null if not written."
    )
    period: Period | None = Field(
        description="Only if the posting literally states it ('per month', '/hr'). "
        "A bare range leaves this null."
    )
    period_stated: bool = Field(description="true exactly when period is set.")

    @model_validator(mode="after")
    def _consistent(self) -> "Salary":
        if not self.is_stated:
            set_fields = [
                f for f in ("min_amount", "max_amount", "currency", "period")
                if getattr(self, f) is not None
            ]
            if set_fields:
                raise ValueError(
                    f"is_stated is false but {', '.join(set_fields)} is set; "
                    "either pay is stated or every field is null."
                )
        elif self.min_amount is None and self.max_amount is None:
            raise ValueError("is_stated is true but no amount was given.")

        for f in ("min_amount", "max_amount"):
            value = getattr(self, f)
            if value is not None and value <= 0:
                raise ValueError(f"{f} must be positive, got {value}.")
        if self.min_amount and self.max_amount and self.min_amount > self.max_amount:
            raise ValueError(
                f"min_amount ({self.min_amount}) is greater than max_amount ({self.max_amount})."
            )
        if self.period_stated != (self.period is not None):
            raise ValueError("period_stated must be true exactly when period is set.")
        return self


class JobPosting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Role title from the posting body.")
    company: str = Field(
        description="Hiring company from the body. If the body says 'this role is "
        "with X and not Y', the answer is X."
    )
    seniority: Seniority | None = Field(
        description="Only when the posting states it. Do not default to 'mid'."
    )
    skills: list[str] = Field(description="Technical skills and tools named, verbatim.")
    salary: Salary
    remote: WorkMode | None = Field(
        description="Only when the posting says where the work happens."
    )
    confidence: float = Field(description="0.0-1.0: how well the text supported this.")

    @model_validator(mode="after")
    def _non_empty(self) -> "JobPosting":
        for f in ("title", "company"):
            if not getattr(self, f).strip():
                raise ValueError(f"{f} must not be empty.")
        if any(not s.strip() for s in self.skills):
            raise ValueError("skills must not contain empty strings.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be between 0 and 1, got {self.confidence}.")
        return self
