from sqlalchemy.orm import Session

from app.db.models import LlmCall
from app.llm.provider import GroundedResult, LLMCallResult


def record_llm_call(
    db: Session, result: LLMCallResult | GroundedResult, related_entity: str | None = None
) -> LlmCall:
    call = LlmCall(
        purpose=result.purpose,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        related_entity=related_entity,
    )
    db.add(call)
    db.commit()
    return call
